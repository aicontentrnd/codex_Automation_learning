"""Static local tokens and OAuth resource-server validation with a live ACL."""
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import jwt
from jwt import PyJWKClient

from common import Problem

SCOPES = ['images:read', 'images:write', 'events:subscribe']


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant: str


@dataclass(frozen=True)
class Settings:
    database: str = './data/jobs.sqlite3'
    image_dir: str = './data/images'
    public_url: str = 'http://127.0.0.1:8000'
    encryption_key: str = ''
    auth_mode: str = 'static'
    tokens_json: str = '{}'
    tokens_file: str = ''
    subjects_file: str = ''
    issuer: str = ''
    jwks_url: str = ''
    callback_hosts: tuple = ()
    demo_mode: bool = False

    @classmethod
    def from_env(cls):
        return cls(database=os.getenv('DB_PATH', './data/jobs.sqlite3'), image_dir=os.getenv('IMAGE_DIR', './data/images'),
                   public_url=os.getenv('PUBLIC_BASE_URL', 'http://127.0.0.1:8000').rstrip('/'),
                   encryption_key=os.getenv('SUBSCRIPTION_ENCRYPTION_KEY', ''), auth_mode=os.getenv('AUTH_MODE', 'static'),
                   tokens_json=os.getenv('APP_TOKENS_JSON', '{}'), tokens_file=os.getenv('AUTH_TOKENS_FILE', ''),
                   subjects_file=os.getenv('AUTH_SUBJECTS_FILE', ''), issuer=os.getenv('AUTH_ISSUER_URL', ''),
                   jwks_url=os.getenv('AUTH_JWKS_URL', ''),
                   callback_hosts=tuple(x.strip() for x in os.getenv('CALLBACK_ALLOWED_HOSTS', '').split(',') if x.strip()),
                   demo_mode=os.getenv('PUBLIC_DEMO_MODE', '').lower() == 'true')

    def validate(self):
        parts = urlsplit(self.public_url)
        if not parts.hostname or parts.query or parts.fragment or parts.username or parts.path not in ('', '/'):
            raise ValueError('PUBLIC_BASE_URL must be an origin without a path or credentials')
        local = parts.scheme == 'http' and parts.hostname in ('localhost', '127.0.0.1', '::1')
        if not local and parts.scheme != 'https':
            raise ValueError('Public deployment requires HTTPS')
        if self.auth_mode not in ('static', 'oauth'):
            raise ValueError('AUTH_MODE must be static or oauth')
        if not local and self.auth_mode != 'oauth':
            raise ValueError('Public deployment requires AUTH_MODE=oauth')
        if self.auth_mode == 'oauth':
            for url in (self.issuer, self.jwks_url):
                parts = urlsplit(url)
                if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.fragment:
                    raise ValueError('OAuth issuer and JWKS URLs must be trusted HTTPS URLs')
            if not self.subjects_file:
                raise ValueError('AUTH_SUBJECTS_FILE is required for tenant authorization')
        if not self.encryption_key:
            raise ValueError('SUBSCRIPTION_ENCRYPTION_KEY is required; generate and persist a Fernet key')


class Directory:
    def __init__(self, settings):
        self.settings = settings
        self.jwks = PyJWKClient(settings.jwks_url, cache_keys=False, lifespan=300, timeout=10) if settings.auth_mode == 'oauth' else None
        self._entries()  # fail startup on malformed/missing account configuration

    def _entries(self):
        try:
            if self.settings.auth_mode == 'oauth':
                raw = Path(self.settings.subjects_file).read_text()
            elif self.settings.tokens_file:
                raw = Path(self.settings.tokens_file).read_text()
            else:
                raw = self.settings.tokens_json
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            for key, account in value.items():
                if not key or (self.settings.auth_mode == 'static' and len(key) < 32):
                    raise ValueError()
                if isinstance(account, str):
                    if not account:
                        raise ValueError()
                elif not isinstance(account, dict) or not isinstance(account.get('tenant'), str) or not account['tenant'] or type(account.get('enabled', True)) is not bool:
                    raise ValueError()
            return value
        except (OSError, ValueError) as exc:
            raise Problem(503, 'Account directory unavailable or invalid') from exc

    def _principal(self, key, value):
        if isinstance(value, str):
            tenant, enabled = value, True
        else:
            tenant, enabled = value['tenant'], value.get('enabled', True)
        if not enabled:
            return None
        subject = key if self.settings.auth_mode == 'oauth' else 'tenant:' + tenant
        return Principal(subject, tenant)

    def demo_tenant(self):
        accounts = (self._principal(key, value) for key, value in self._entries().items())
        tenants = {account.tenant for account in accounts if account}
        if len(tenants) != 1:
            raise Problem(503, 'Demo mode requires exactly one active tenant')
        return tenants.pop()

    def active(self, subject, tenant):
        for key, value in self._entries().items():
            account = self._principal(key, value)
            if account and account.subject == subject and account.tenant == tenant:
                return True
        return False

    def authenticate(self, authorization):
        if not authorization or not authorization.startswith('Bearer ') or len(authorization) > 16384:
            raise Problem(401, 'Bearer token required')
        token = authorization[7:]
        entries = self._entries()
        if self.settings.auth_mode == 'static':
            digest = hashlib.sha256(token.encode()).digest()
            for key, value in entries.items():
                if hmac.compare_digest(digest, hashlib.sha256(key.encode()).digest()):
                    account = self._principal(key, value)
                    if account:
                        return account
            raise Problem(401, 'Invalid bearer token')
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=['RS256', 'ES256'], issuer=self.settings.issuer,
                                audience=self.settings.public_url + '/mcp', options={'require': ['exp', 'iat', 'sub', 'iss', 'aud']})
        except jwt.PyJWKClientConnectionError as exc:
            raise Problem(503, 'Authorization signing keys temporarily unavailable') from exc
        except jwt.PyJWTError as exc:
            raise Problem(401, 'Invalid or expired access token') from exc
        scopes = claims.get('scope', '')
        if not isinstance(scopes, str) or not set(SCOPES).issubset(scopes.split()):
            raise Problem(403, 'Required OAuth scopes are missing')
        subject = claims.get('sub')
        account = self._principal(subject, entries[subject]) if subject in entries else None
        if not account:
            raise Problem(403, 'Account access revoked or not provisioned')
        return account
