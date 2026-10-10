"""Create local backend configuration without replacing an existing file."""
import os
from pathlib import Path
from cryptography.fernet import Fernet

root = Path(__file__).resolve().parents[1]
env_file = root / '.env'
if env_file.exists():
    raise SystemExit('Local configuration already exists; preserving it.')
content = ('PUBLIC_BASE_URL=http://127.0.0.1:8000\nAPP_WORKSPACE_ID=local-demo\n'
           + 'SUBSCRIPTION_ENCRYPTION_KEY=' + Fernet.generate_key().decode() + '\n')
with os.fdopen(os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
    f.write(content)
print('Created .env. Keep the subscription encryption key private and stable.')
