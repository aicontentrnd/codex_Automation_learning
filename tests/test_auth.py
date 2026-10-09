import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from auth import Directory, Settings, SCOPES
from common import Problem
from server import create_app


class OAuthTests(unittest.TestCase):
    def test_metadata_jwt_validation_tenant_mapping_and_revocation(self):
        with tempfile.TemporaryDirectory() as d:
            acl = Path(d) / 'subjects.json'
            acl.write_text(json.dumps({'user-1': {'tenant': 'tenant-1', 'enabled': True}}))
            settings = Settings(database=d+'/jobs.db',image_dir=d+'/images',public_url='https://images.example',
                encryption_key=Fernet.generate_key().decode(),auth_mode='oauth',issuer='https://identity.example',
                jwks_url='https://identity.example/keys',subjects_file=str(acl))
            directory = Directory(settings)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            claims = {'sub':'user-1','iss':settings.issuer,'aud':settings.public_url+'/mcp',
                      'iat':int(time.time()),'exp':int(time.time())+300,'scope':' '.join(SCOPES)}
            def token(values): return 'Bearer '+jwt.encode(values,key,algorithm='RS256',headers={'kid':'test-key'})
            with patch.object(directory.jwks,'get_signing_key_from_jwt',return_value=SimpleNamespace(key=key.public_key())):
                self.assertEqual(directory.authenticate(token(claims)).tenant,'tenant-1')
                for change in [{'aud':'wrong'}, {'iss':'https://wrong.example'}, {'exp':1}, {'scope':'images:read'}]:
                    with self.assertRaises(Problem): directory.authenticate(token({**claims,**change}))
                acl.write_text(json.dumps({'user-1':{'tenant':'tenant-1','enabled':False}}))
                self.assertFalse(directory.active('user-1','tenant-1'))
                with self.assertRaises(Problem): directory.authenticate(token(claims))
            with TestClient(create_app(settings,run_worker=False),base_url=settings.public_url) as client:
                metadata = client.get('/.well-known/oauth-protected-resource/mcp').json()
                self.assertEqual(metadata['resource'], settings.public_url+'/mcp')
                self.assertEqual(metadata['authorization_servers'], [settings.issuer])
                response = client.post('/mcp',json={})
                self.assertEqual(response.status_code,401)
                self.assertIn('resource_metadata',response.headers['www-authenticate'])

    def test_public_static_mode_rejected(self):
        with self.assertRaises(ValueError):
            Settings(public_url='https://images.example',encryption_key=Fernet.generate_key().decode()).validate()
