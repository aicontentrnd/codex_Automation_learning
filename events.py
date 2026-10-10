"""OpenAI's documented MCP Events webhook lifecycle and durable delivery worker."""
import base64
import hashlib
import hmac
import json
import logging
import random
import secrets
import socket
import ssl
import threading
import time
import uuid
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from mcp.shared.exceptions import MCPError
from standardwebhooks import Webhook

from common import canonical, iso
from secure_http import DestinationError, SafeHTTPS, callback_url

log = logging.getLogger(__name__)
EVENT_NAME = 'image.requested'
FILTER_SCHEMA = {'type': 'object', 'properties': {}, 'additionalProperties': False}
EVENT = {'name': EVENT_NAME,
         'description': 'Triggered when an authorized application user creates a new AI image-generation request.',
         'delivery': ['webhook'], 'inputSchema': FILTER_SCHEMA,
         'payloadSchema': {'type': 'object', 'properties': {
             'job_id': {'type': 'string'}, 'prompt_preview': {'type': 'string', 'maxLength': 100},
             'aspect_ratio': {'type': ['string', 'null']}, 'created_at': {'type': 'string', 'format': 'date-time'},
             'attempt': {'type': 'integer', 'minimum': 1}},
             'required': ['job_id', 'prompt_preview', 'aspect_ratio', 'created_at', 'attempt'],
             'additionalProperties': False}}


def validate_secret(secret):
    try:
        if not isinstance(secret, str) or not secret.startswith('whsec_') or len(secret) > 100:
            raise ValueError()
        raw = base64.b64decode(secret[6:] + '=' * (-len(secret[6:]) % 4), validate=True)
        if not 24 <= len(raw) <= 64:
            raise ValueError()
    except ValueError as exc:
        raise MCPError(-32602, 'Invalid webhook signing secret') from exc
    return secret


def signed_headers(sid, event_id, body, keys, timestamp):
    instant = datetime.fromtimestamp(timestamp, timezone.utc)
    return {'Content-Type': 'application/json', 'webhook-id': event_id,
            'webhook-timestamp': str(int(timestamp)), 'X-MCP-Subscription-Id': sid,
            'webhook-signature': ' '.join(Webhook(key).sign(event_id, instant, body.decode()) for key in keys)}


class Events:
    def __init__(self, store, encryption_key, directory, transport=None, clock=time.time):
        self.store, self.directory, self.clock = store, directory, clock
        self.cipher = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
        self.transport = transport or SafeHTTPS()
        self.lock = threading.RLock()
        # One delivery worker per process; sqlite leases also prevent cross-process claims.
        self.worker_lock = threading.Lock()

    def _identity(self, principal, params):
        name, arguments = params.get('name'), params.get('arguments', {})
        if name != EVENT_NAME:
            raise MCPError(-32011, 'Unknown event', {'kind': 'event'})
        if arguments != {}:
            raise MCPError(-32602, 'This event does not accept subscription arguments')
        delivery = params.get('delivery', {})
        if not isinstance(delivery, dict) or delivery.get('mode') != 'webhook':
            raise MCPError(-32014, 'Only webhook delivery is supported', {'feature': 'deliveryMode'})
        try:
            url = callback_url(delivery.get('url'))
        except DestinationError as exc:
            raise MCPError(-32602, str(exc)) from exc
        sid = 'sub_' + hashlib.sha256(canonical([principal.subject, principal.tenant, url, name, arguments]).encode()).hexdigest()
        return sid, url, arguments

    def subscribe(self, principal, params):
        sid, url, arguments = self._identity(principal, params)
        secret = validate_secret(params['delivery'].get('secret'))
        cursor = params.get('cursor')
        if cursor is not None:
            raise MCPError(-32014, 'Event replay is not supported', {'feature': 'cursor'})
        ttl = params.get('ttlMs', 86400000)
        # Null requests infinite TTL; this server explicitly grants a finite day.
        if ttl is None:
            ttl = 86400000
        if type(ttl) is not int or ttl <= 0:
            raise MCPError(-32602, 'ttlMs must be a positive integer or null')
        duration = min(ttl / 1000, 86400)
        with self.lock:
            if not self.directory.active(principal.subject, principal.tenant):
                raise MCPError(-32012, 'Account access revoked')
            stamp = self.clock()
            with self.store.connect() as db:
                count = db.execute('SELECT COUNT(*) FROM subscriptions WHERE principal=? AND active=1 AND expires_at>? AND id!=?', (principal.subject, stamp, sid)).fetchone()[0]
                if count >= 100:
                    raise MCPError(-32013, 'Subscription limit reached', {'limit': 'subscriptions', 'max': 100})
                key_hash = hashlib.sha256(secret.encode()).hexdigest()
                cached = db.execute('SELECT 1 FROM callback_verifications WHERE principal=? AND url=? AND key_hash=? AND verified_until>?', (principal.subject, url, key_hash, stamp)).fetchone()
            if not cached:
                challenge = secrets.token_urlsafe(32)
                body = canonical({'type': 'verification', 'challenge': challenge}).encode()
                event_id = 'verification_' + uuid.uuid4().hex
                try:
                    status, response = self.transport.post(url, body, signed_headers(sid, event_id, body, [secret], stamp))
                    echoed = json.loads(response).get('challenge') if 200 <= status < 300 else None
                    if not isinstance(echoed, str) or not hmac.compare_digest(echoed.encode(), challenge.encode()) or self.clock() - stamp > 10:
                        raise MCPError(-32015, 'Callback verification failed', {'reason': 'challenge_failed'})
                except MCPError:
                    raise
                except (TimeoutError, socket.timeout) as exc:
                    raise MCPError(-32015, 'Callback verification timed out', {'reason': 'timeout'}) from exc
                except ssl.SSLError as exc:
                    raise MCPError(-32015, 'Callback TLS verification failed', {'reason': 'tls_error'}) from exc
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    raise MCPError(-32015, 'Callback verification failed', {'reason': 'challenge_failed'}) from exc
                with self.store.connect() as db:
                    db.execute('INSERT OR REPLACE INTO callback_verifications VALUES (?,?,?,?)', (principal.subject, url, key_hash, self.clock() + 300))
            stamp = self.clock()
            if not self.directory.active(principal.subject, principal.tenant):
                raise MCPError(-32012, 'Account access revoked')
            expires = stamp + duration
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                old = db.execute('SELECT * FROM subscriptions WHERE id=?', (sid,)).fetchone()
                old_secret, old_until = None, None
                encrypted = self.cipher.encrypt(secret.encode()).decode()
                if old:
                    if self.cipher.decrypt(old['secret'].encode()).decode() != secret:
                        old_secret, old_until = old['secret'], stamp + 300
                    elif old['old_until'] and old['old_until'] > stamp:
                        old_secret, old_until = old['old_secret'], old['old_until']
                    # An expired subscription's pending deliveries must not turn into replay.
                    if old['expires_at'] <= stamp:
                        db.execute("UPDATE deliveries SET state='canceled', last_error='expired' WHERE subscription_id=? AND state IN ('queued','sending')", (sid,))
                db.execute('''INSERT INTO subscriptions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET secret=excluded.secret, old_secret=excluded.old_secret,
                    old_until=excluded.old_until, expires_at=excluded.expires_at, active=1''',
                    (sid, principal.subject, principal.tenant, EVENT_NAME, canonical(arguments), url,
                     encrypted, old_secret, old_until, expires, 1, stamp))
            return {'id': sid, 'refreshBefore': iso(expires), 'cursor': None, 'truncated': False}

    def unsubscribe(self, principal, params):
        sid, _, _ = self._identity(principal, params)
        with self.lock, self.store.connect() as db:
            db.execute('UPDATE subscriptions SET active=0 WHERE id=? AND principal=?', (sid, principal.subject))
            db.execute("UPDATE deliveries SET state='canceled', last_error='unsubscribed' WHERE subscription_id=? AND state IN ('queued','sending')", (sid,))
        return {}

    def tick(self):
        """Attempt one due delivery; persisted leases recover a crash mid-request."""
        with self.worker_lock, self.lock:
            stamp = self.clock()
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                item = db.execute("""SELECT d.*, s.principal, s.tenant, s.url, s.secret, s.old_secret,
                    s.old_until, s.expires_at, s.active, e.job_id, e.data, e.created_at, e.name
                    FROM deliveries d JOIN subscriptions s ON s.id=d.subscription_id
                    JOIN event_outbox e ON e.event_id=d.event_id
                    WHERE (d.state='queued' AND d.available_at<=?) OR (d.state='sending' AND d.lease_until<=?)
                    ORDER BY d.available_at LIMIT 1""", (stamp, stamp)).fetchone()
                if not item:
                    return False
                item = dict(item)
                ids = (item['event_id'], item['subscription_id'])
                if not item['active'] or item['expires_at'] <= stamp or not self.directory.active(item['principal'], item['tenant']):
                    db.execute("UPDATE deliveries SET state='canceled', last_error='expired_or_revoked' WHERE event_id=? AND subscription_id=?", ids)
                    return True
                db.execute("UPDATE deliveries SET state='sending', attempts=attempts+1, lease_until=? WHERE event_id=? AND subscription_id=?", (stamp + 60, *ids))
            event = {'eventId': item['event_id'], 'name': item['name'], 'timestamp': item['created_at'], 'data': json.loads(item['data']), 'cursor': None}
            body = canonical(event).encode()
            status, reason = None, None
            try:
                if len(body) > 262144:
                    raise DestinationError('Event exceeds 256 KiB')
                keys = [self.cipher.decrypt(item['secret'].encode()).decode()]
                if item['old_secret'] and item['old_until'] > stamp:
                    keys.append(self.cipher.decrypt(item['old_secret'].encode()).decode())
                status, _ = self.transport.post(item['url'], body, signed_headers(item['subscription_id'], item['event_id'], body, keys, stamp))
            except DestinationError:
                reason = 'unsafe_destination_or_payload'
            except (OSError, TimeoutError):
                reason = 'network_error'
            except Exception:
                # Keep the worker alive while surfacing configuration corruption.
                log.error(canonical({'event': 'delivery_error', 'event_id': item['event_id'], 'reason': 'internal_error'}))
                reason = 'internal_error'
            attempts = item['attempts'] + 1
            accepted = status is not None and 200 <= status < 300
            transient = (status is None and reason == 'network_error') or status in (408, 425, 429) or (status is not None and 500 <= status < 600)
            state = 'delivered' if accepted else ('queued' if transient and attempts < 8 else 'dead')
            delay = min(300, 5 * 2 ** (attempts - 1)) * random.uniform(0.8, 1.2)
            with self.store.connect() as db:
                db.execute('UPDATE deliveries SET state=?, available_at=?, lease_until=NULL, last_error=?, last_status=? WHERE event_id=? AND subscription_id=?',
                           (state, self.clock() + delay, reason or (None if accepted else f'http_{status}'), status, *ids))
                if accepted:
                    db.execute("UPDATE jobs SET status='event_delivered', updated_at=? WHERE id=? AND status='pending' AND attempt=?",
                               (iso(self.clock()), item['job_id'], event['data'].get('attempt', 1)))
                if status == 410:
                    db.execute('UPDATE subscriptions SET active=0 WHERE id=?', (item['subscription_id'],))
            log.info(canonical({'event': 'webhook_delivery', 'event_id': item['event_id'], 'subscription_id': item['subscription_id'], 'state': state, 'attempt': attempts, 'http_status': status}))
            return True
