"""One-shot: bundle orbit/.env + backend/.env, encrypt with a random passphrase.
Run locally to produce secrets.enc (safe to push). Decrypt on the VPS with unmake_secrets.py.
Prints the passphrase ONCE — save it, you'll paste it into the server console."""
import os, json, base64, secrets, sys
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

HERE = os.path.dirname(os.path.abspath(__file__))
ORBIT_ENV   = os.path.join(HERE, ".env")
BACKEND_ENV = os.path.join(HERE, "eyecrypt", "backend", ".env")
OUT         = os.path.join(HERE, "secrets.enc")

for p in (ORBIT_ENV, BACKEND_ENV):
    if not os.path.isfile(p):
        print("MISSING:", p); sys.exit(1)

payload = json.dumps({
    "orbit_env":   open(ORBIT_ENV,   "r", encoding="utf-8").read(),
    "backend_env": open(BACKEND_ENV, "r", encoding="utf-8").read(),
}).encode()

passphrase = base64.urlsafe_b64encode(secrets.token_bytes(24)).decode().rstrip("=")
salt  = secrets.token_bytes(16)
nonce = secrets.token_bytes(12)
key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000).derive(passphrase.encode())
ct = AESGCM(key).encrypt(nonce, payload, None)

blob = {"v": 1, "salt": base64.b64encode(salt).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "ct": base64.b64encode(ct).decode()}
open(OUT, "w").write(json.dumps(blob))
print("wrote", OUT, f"({os.path.getsize(OUT)} bytes)")
print("\n=========================================")
print("  PASSPHRASE (save it, paste on server):")
print(" ", passphrase)
print("=========================================")
