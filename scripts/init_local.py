"""Create local-only credentials without printing values or replacing user files."""
import json
import os
import secrets
from pathlib import Path
from cryptography.fernet import Fernet

root = Path(__file__).resolve().parents[1]
env_file = root / '.env'
token_file = root / 'data' / 'local-tokens.json'
if env_file.exists() or token_file.exists():
    raise SystemExit('Local configuration already exists; preserving it.')
token_file.parent.mkdir(exist_ok=True, mode=0o700)
token = secrets.token_urlsafe(32)
for path, content in [
    (token_file, json.dumps({token: 'local-demo'}) + '\n'),
    (env_file, 'AUTH_MODE=static\nPUBLIC_BASE_URL=http://127.0.0.1:8000\nAUTH_TOKENS_FILE=./data/local-tokens.json\nPUBLIC_DEMO_MODE=true\n'
     + 'SUBSCRIPTION_ENCRYPTION_KEY=' + Fernet.generate_key().decode() + '\n')]:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
        f.write(content)
print('Created .env and data/local-tokens.json. Keep both private.')
