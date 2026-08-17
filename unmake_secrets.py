"""On the VPS: decrypt secrets.enc back into .env files.
Usage: SECRETS_PASS='<passphrase>' python3 unmake_secrets.py
Writes ./.env (orbit) and ./eyecrypt/backend/.env"""
import os, json, base64, sys
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

HERE = os.path.dirname(os.path.abspath(__file__))
blob = json.load(open(os.path.join(HERE, "secrets.enc")))
passphrase = os.environ.get("SECRETS_PASS", "").strip()
if not passphrase:
    print("SECRETS_PASS env var is empty"); sys.exit(1)

salt  = base64.b64decode(blob["salt"])
nonce = base64.b64decode(blob["nonce"])
ct    = base64.b64decode(blob["ct"])
key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000).derive(passphrase.encode())
try:
    payload = json.loads(AESGCM(key).decrypt(nonce, ct, None))
except Exception:
    print("DECRYPT FAILED — wrong passphrase?"); sys.exit(2)

orbit_path   = os.path.join(HERE, ".env")
backend_path = os.path.join(HERE, "eyecrypt", "backend", ".env")
os.makedirs(os.path.dirname(backend_path), exist_ok=True)
open(orbit_path,   "w", encoding="utf-8").write(payload["orbit_env"])
open(backend_path, "w", encoding="utf-8").write(payload["backend_env"])
os.chmod(orbit_path, 0o600); os.chmod(backend_path, 0o600)
print("restored:", orbit_path, "and", backend_path)
