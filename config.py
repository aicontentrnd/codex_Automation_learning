"""Backend settings for a shared workspace without user authentication."""
import os
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Workspace:
    tenant: str

    @property
    def subject(self):
        # Keep the existing subscription/database identity format.
        return 'tenant:' + self.tenant


@dataclass(frozen=True)
class Settings:
    database: str = './data/jobs.sqlite3'
    image_dir: str = './data/images'
    public_url: str = 'http://127.0.0.1:8000'
    encryption_key: str = ''
    callback_hosts: tuple = ()
    workspace_id: str = 'local-demo'

    @classmethod
    def from_env(cls):
        return cls(database=os.getenv('DB_PATH', './data/jobs.sqlite3'),
                   image_dir=os.getenv('IMAGE_DIR', './data/images'),
                   public_url=os.getenv('PUBLIC_BASE_URL', 'http://127.0.0.1:8000').rstrip('/'),
                   encryption_key=os.getenv('SUBSCRIPTION_ENCRYPTION_KEY', ''),
                   callback_hosts=tuple(x.strip() for x in os.getenv('CALLBACK_ALLOWED_HOSTS', '').split(',') if x.strip()),
                   workspace_id=os.getenv('APP_WORKSPACE_ID', 'local-demo'))

    def validate(self):
        parts = urlsplit(self.public_url)
        if not parts.hostname or parts.query or parts.fragment or parts.username or parts.path not in ('', '/'):
            raise ValueError('PUBLIC_BASE_URL must be an origin without a path or credentials')
        local = parts.scheme == 'http' and parts.hostname in ('localhost', '127.0.0.1', '::1')
        if not local and parts.scheme != 'https':
            raise ValueError('Public deployment requires HTTPS')
        if not isinstance(self.workspace_id, str) or not 1 <= len(self.workspace_id) <= 128:
            raise ValueError('APP_WORKSPACE_ID must contain 1–128 characters')
        if not self.encryption_key:
            raise ValueError('SUBSCRIPTION_ENCRYPTION_KEY is required; generate and persist a Fernet key')
