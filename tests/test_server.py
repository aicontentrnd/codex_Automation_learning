import base64
import hashlib
import hmac
import io
import json
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from PIL import Image
from starlette.testclient import TestClient
from standardwebhooks import Webhook

from auth import Settings
from server import create_app, VERSION
from secure_http import SafeHTTPS, DestinationError, callback_url, public_addresses

TOKEN_A, TOKEN_B = 'a' * 40, 'b' * 40
SECRET = 'whsec_' + base64.b64encode(b's' * 32).decode()
NEW_SECRET = 'whsec_' + base64.b64encode(b'n' * 32).decode()
buffer = io.BytesIO()
Image.new('RGB', (2, 2), 'red').save(buffer, format='PNG')
PNG = base64.b64encode(buffer.getvalue()).decode()


class Clock:
    def __init__(self): self.value = time.time()
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


class Receiver:
    def __init__(self, clock):
        self.clock, self.secret = clock, SECRET
        self.records, self.statuses = [], []
        self.bad_challenge = False

    def post(self, url, body, headers):
        self.records.append((url, body, headers.copy()))
        signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
        expected = base64.b64encode(hmac.new(base64.b64decode(self.secret[6:]), signed, hashlib.sha256).digest()).decode()
        if 'v1,' + expected not in headers['webhook-signature'].split() or abs(self.clock() - int(headers['webhook-timestamp'])) > 300:
            return 401, b'{}'
        data = json.loads(body)
        if data.get('type') == 'verification':
            return 200, json.dumps({'challenge': 'wrong' if self.bad_challenge else data['challenge']}).encode()
        status = self.statuses.pop(0) if self.statuses else 204
        if isinstance(status, Exception): raise status
        return status, b''

    def deliveries(self):
        return [r for r in self.records if 'eventId' in json.loads(r[1])]


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.receiver = Receiver(self.clock)
        self.tokens = Path(self.tmp.name) / 'tokens.json'
        self.tokens.write_text(json.dumps({TOKEN_A: 'a', TOKEN_B: 'b'}))
        self.settings = Settings(database=self.tmp.name + '/db/jobs.sqlite3', image_dir=self.tmp.name + '/images',
                                 encryption_key=Fernet.generate_key().decode(), tokens_file=str(self.tokens))
        self.app = create_app(self.settings, transport=self.receiver, run_worker=False, clock=self.clock)
        self.client = TestClient(self.app, base_url=self.settings.public_url).__enter__()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.client.__exit__, None, None, None)

    def rpc(self, method, params=None, token=TOKEN_A, client=None):
        params = dict(params or {})
        params['_meta'] = {'io.modelcontextprotocol/protocolVersion': VERSION, 'io.modelcontextprotocol/clientCapabilities': {}}
        headers = {'Authorization': f'Bearer {token}', 'MCP-Protocol-Version': VERSION, 'MCP-Method': method, 'Accept': 'application/json, text/event-stream'}
        if 'name' in params: headers['MCP-Name'] = params['name']
        return (client or self.client).post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})

    def tool(self, name, args, token=TOKEN_A):
        return self.rpc('tools/call', {'name': name, 'arguments': args}, token).json()

    def subscription(self, arguments=None, secret=SECRET, **extra):
        return {'name': 'image.requested', 'arguments': arguments or {}, 'delivery': {'mode': 'webhook', 'url': 'https://receiver.example/callback', 'secret': secret}, **extra}

    def subscribe(self, **extra):
        response = self.rpc('events/subscribe', self.subscription(**extra)).json()
        self.assertNotIn('error', response, response)
        return response['result']

    def create(self, token=TOKEN_A, key=None, **extra):
        response = self.client.post('/jobs', headers={'Authorization': f'Bearer {token}', 'Idempotency-Key': key or str(uuid.uuid4())}, json={'prompt': 'A mountain house', **extra})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['job_id']

    def claim(self, jid, claim_id='execution-123'):
        result = self.tool('get_image_request', {'job_id': jid, 'claim_id': claim_id})
        self.assertNotIn('error', result, result)
        self.assertFalse(result['result'].get('isError', False), result)
        return result['result']['structuredContent']

    def submission(self, job):
        return {'job_id': job['job_id'], 'attempt': job['attempt'], 'claim_token': job['claim_token'], 'generation_status': 'completed', 'media_type': 'image/png', 'image_base64': PNG}

    def test_discovery_and_event_schemas(self):
        body = self.rpc('server/discover').json()['result']
        self.assertEqual(body['supportedVersions'], [VERSION])
        self.assertEqual(body['capabilities'], {'tools': {}, 'events': {}})
        self.assertEqual(len(self.rpc('tools/list').json()['result']['tools']), 3)
        event = self.rpc('events/list').json()['result']['events'][0]
        self.assertEqual(event['delivery'], ['webhook'])
        self.assertEqual(event['inputSchema']['properties'], {})
        self.assertEqual(self.rpc('events/list', {'cursor': 'bad'}).json()['error']['code'], -32602)

    def test_demo_page_creates_event_without_browser_token_but_mcp_stays_protected(self):
        self.tokens.write_text(json.dumps({TOKEN_A: 'a'}))
        demo_settings = replace(self.settings, demo_mode=True)
        demo_app = create_app(demo_settings, transport=self.receiver, run_worker=False, clock=self.clock)
        self.subscribe()
        with TestClient(demo_app, base_url=demo_settings.public_url) as browser:
            self.assertEqual(browser.post('/mcp', json={}).status_code, 401)
            response = browser.post('/jobs', headers={'Idempotency-Key': 'demo-request-123'},
                                    json={'prompt': 'A mountain house'})
            self.assertEqual(response.status_code, 201, response.text)
            jid = response.json()['job_id']
            self.assertEqual(browser.get(f'/jobs/{jid}').status_code, 200)
            self.assertTrue(demo_app.state.events.tick())
            self.assertEqual(json.loads(self.receiver.deliveries()[0][1])['data']['job_id'], jid)

    def test_subscribe_refresh_encryption_and_signed_challenge(self):
        first = self.subscribe()
        second = self.subscribe()
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(self.receiver.records), 1)
        _, body, headers = self.receiver.records[0]
        Webhook(SECRET).verify(body, headers)
        with self.app.state.store.connect() as db:
            row = db.execute('SELECT * FROM subscriptions').fetchone()
        self.assertNotIn(SECRET, row['secret'])
        self.assertEqual(first['cursor'], None)

    def test_bad_callback_and_invalid_subscription(self):
        self.receiver.bad_challenge = True
        self.assertEqual(self.rpc('events/subscribe', self.subscription()).json()['error']['code'], -32015)
        self.receiver.bad_challenge = False
        self.receiver.secret = NEW_SECRET
        self.assertEqual(self.rpc('events/subscribe', self.subscription()).json()['error']['code'], -32015)
        for params in [self.subscription(secret='whsec_short'), self.subscription(arguments={'bad': 'x'}), self.subscription(ttlMs=0), self.subscription(cursor='old')]:
            self.assertIn('error', self.rpc('events/subscribe', params).json())
        params = self.subscription(); params['delivery']['url'] = 'http://127.0.0.1/'
        self.assertEqual(self.rpc('events/subscribe', params).json()['error']['code'], -32602)

    def test_end_to_end_and_duplicate_result(self):
        self.subscribe()
        jid = self.create()
        self.assertTrue(self.app.state.events.tick())
        event = json.loads(self.receiver.deliveries()[0][1])
        self.assertEqual(event['data']['job_id'], jid)
        self.assertIsNone(event['cursor'])
        self.assertEqual(self.app.state.store.status('a', jid)['status'], 'event_delivered')
        job = self.claim(jid)
        result = self.tool('submit_generated_image', self.submission(job))['result']
        self.assertEqual(result['structuredContent']['status'], 'completed')
        repeat = self.tool('submit_generated_image', self.submission(job))['result']
        self.assertEqual(repeat['structuredContent'], result['structuredContent'])
        image = self.client.get(f'/jobs/{jid}/image', headers={'Authorization': f'Bearer {TOKEN_A}'})
        self.assertEqual(image.status_code, 200)
        Image.open(io.BytesIO(image.content)).verify()
        self.assertEqual(self.client.get(f'/jobs/{jid}/image', headers={'Authorization': f'Bearer {TOKEN_B}'}).status_code, 404)

    def test_tenant_isolation_and_unsubscribe(self):
        self.subscribe()
        self.create(token=TOKEN_B)
        self.assertFalse(self.app.state.events.tick())
        jid = self.create()
        self.assertTrue(self.tool('get_image_request', {'job_id': jid}, TOKEN_B)['result']['isError'])
        params = self.subscription()
        self.rpc('events/unsubscribe', params, TOKEN_B)
        self.assertTrue(self.app.state.events.tick())
        self.assertEqual(len(self.receiver.deliveries()), 1)
        self.rpc('events/unsubscribe', params)
        self.rpc('events/unsubscribe', params)
        self.create()
        self.assertFalse(self.app.state.events.tick())

    def test_expiration_restart_and_revocation(self):
        self.subscribe(ttlMs=1000)
        self.create()
        self.clock.advance(2)
        restored = create_app(self.settings, transport=self.receiver, run_worker=False, clock=self.clock)
        self.assertTrue(restored.state.events.tick())
        self.assertEqual(len(self.receiver.deliveries()), 0)
        self.subscribe()
        self.create()
        self.tokens.write_text(json.dumps({TOKEN_B: 'b'}))
        self.app.state.events.tick()
        self.assertEqual(len(self.receiver.deliveries()), 0)
        self.assertEqual(self.rpc('events/list').status_code, 401)

    def test_rotation_signs_both_keys(self):
        first = self.subscribe()
        self.receiver.secret = NEW_SECRET
        self.assertEqual(first['id'], self.subscribe(secret=NEW_SECRET)['id'])
        self.create(); self.app.state.events.tick()
        _, body, headers = self.receiver.deliveries()[0]
        self.assertEqual(len(headers['webhook-signature'].split()), 2)
        Webhook(SECRET).verify(body, headers)
        Webhook(NEW_SECRET).verify(body, headers)

    def test_network_retries_preserve_event_and_refresh_signature(self):
        self.subscribe(); jid = self.create()
        self.receiver.statuses = [TimeoutError(), 500, 204]
        for _ in range(3):
            self.app.state.events.tick(); self.clock.advance(30)
        requests = self.receiver.deliveries()
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[0][1], requests[2][1])
        self.assertNotEqual(requests[0][2]['webhook-signature'], requests[2][2]['webhook-signature'])
        self.assertEqual(self.app.state.store.status('a', jid)['status'], 'event_delivered')

    def test_permanent_statuses_and_bounded_attempts(self):
        self.subscribe()
        for status in (400, 401, 410, 413, 302):
            self.subscribe(); jid = self.create(); self.receiver.statuses = [status]
            self.app.state.events.tick(); self.clock.advance(400)
            self.assertFalse(self.app.state.events.tick())
            self.assertEqual(self.app.state.store.status('a', jid)['deliveries'], {'dead': 1})
        self.subscribe(); jid = self.create(); self.receiver.statuses = [503] * 8
        for _ in range(8):
            self.app.state.events.tick(); self.clock.advance(400)
        self.assertFalse(self.app.state.events.tick())
        self.assertEqual(self.app.state.store.status('a', jid)['deliveries'], {'dead': 1})

    def test_creation_idempotency_claims_failure_retry_and_stale_results(self):
        key = 'unique-key-123'
        jid = self.create(key=key)
        self.assertEqual(self.create(key=key), jid)
        first = self.claim(jid)
        self.assertEqual(self.claim(jid)['claim_token'], first['claim_token'])
        self.assertTrue(self.tool('get_image_request', {'job_id': jid, 'claim_id': 'competing-task'})['result']['isError'])
        failure = {k: first[k] for k in ('job_id', 'attempt', 'claim_token')}
        failure.update(generation_status='failed', error_details='No compatible image generation tool')
        self.assertEqual(self.tool('submit_generated_image', failure)['result']['structuredContent']['status'], 'failed')
        headers = {'Authorization': f'Bearer {TOKEN_A}', 'Idempotency-Key': 'retry-key-123'}
        retry = self.client.post(f'/jobs/{jid}/retry', headers=headers)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()['attempt'], 2)
        self.assertEqual(self.client.post(f'/jobs/{jid}/retry', headers=headers).json(), retry.json())
        self.assertTrue(self.tool('submit_generated_image', failure)['result']['isError'])

    def test_invalid_image_and_protocol_security(self):
        jid = self.create(); job = self.claim(jid)
        args = self.submission(job); args['image_base64'] = 'AAAA'
        self.assertTrue(self.tool('submit_generated_image', args)['result']['isError'])
        self.assertEqual(self.rpc('events/list', token='wrong').status_code, 401)
        self.assertEqual(self.client.get('/', headers={'Origin': 'https://attacker.example'}).status_code, 403)
        self.assertIn('blob:', self.client.get('/').headers['content-security-policy'])
        self.assertEqual(self.client.get('/app.js').status_code, 200)
        self.assertEqual(self.client.get('/style.css').status_code, 200)


class NetworkTests(unittest.TestCase):
    def test_ssrf_rejected(self):
        for url in ['http://example.com', 'https://user:pass@example.com', 'https://example.com:8443', 'https://example.com/#fragment']:
            with self.assertRaises(DestinationError): callback_url(url)
        for ip in ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', '::ffff:127.0.0.1', '224.0.0.1']:
            with patch('secure_http.socket.getaddrinfo', return_value=[(2, 1, 6, '', (ip, 443))]):
                with self.assertRaises(DestinationError): public_addresses('receiver.example')
        with patch('secure_http.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443)), (2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(DestinationError): public_addresses('receiver.example')

    def test_standard_webhook_rejects_tampering(self):
        from datetime import datetime, timezone
        body = '{"eventId":"event-one"}'
        stamp = datetime.now(timezone.utc)
        signature = Webhook(SECRET).sign('event-one', stamp, body)
        headers = {'webhook-id': 'event-one', 'webhook-timestamp': str(int(stamp.timestamp())), 'webhook-signature': signature}
        self.assertEqual(Webhook(SECRET).verify(body, headers)['eventId'], 'event-one')
        with self.assertRaises(Exception): Webhook(SECRET).verify(body + ' ', headers)
        headers['webhook-timestamp'] = '1'
        with self.assertRaises(Exception): Webhook(SECRET).verify(body, headers)


if __name__ == '__main__': unittest.main()
