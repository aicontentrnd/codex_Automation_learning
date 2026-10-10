import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from config import Settings
from server import create_app
from starlette.testclient import TestClient


class ConfigTests(unittest.TestCase):
    def test_public_https_starts_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(public_url='https://images.example',
                                database=directory + '/jobs.db', image_dir=directory + '/images',
                                encryption_key=Fernet.generate_key().decode())
            with TestClient(create_app(settings, run_worker=False), base_url=settings.public_url) as client:
                response = client.get('/health')
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('www-authenticate', response.headers)

    def test_legacy_auth_configuration_is_ignored(self):
        with patch.dict('os.environ', {'AUTH_MODE': 'oauth', 'AUTH_SUBJECTS_FILE': '/missing',
                                     'AUTH_TOKENS_FILE': '/missing', 'PUBLIC_DEMO_MODE': 'true',
                                     'APP_WORKSPACE_ID': 'existing-tenant'}, clear=True):
            settings = Settings.from_env()
            self.assertEqual(settings.workspace_id, 'existing-tenant')
            self.assertFalse(hasattr(settings, 'auth_mode'))

    def test_transport_and_storage_configuration_validation(self):
        key = Fernet.generate_key().decode()
        for url in ('http://images.example', 'https://images.example/path', 'https://user@images.example'):
            with self.assertRaises(ValueError):
                Settings(public_url=url, encryption_key=key).validate()
        with self.assertRaises(ValueError):
            Settings(encryption_key=key, workspace_id='').validate()
        with self.assertRaises(ValueError):
            Settings().validate()
