#!/usr/bin/env bash
# Re-pull encrypted secrets, rewrite backend .env, restart the API.
# Needs SECRETS_PASS in env. Run from /root/orbit.
set -e
cd /root/orbit
git pull
sed -i 's/\r$//' unmake_secrets.py 2>/dev/null || true
.venv/bin/python unmake_secrets.py            # writes /root/orbit/.env and /root/orbit/eyecrypt/backend/.env

cp /root/orbit/eyecrypt/backend/.env /root/eyecrypt-src/backend/.env
cd /root/eyecrypt-src/backend
sed -i 's/\r$//' .env
sed -i "s#^DATABASE_URL=.*#DATABASE_URL=postgresql+psycopg2://eyecrypt:eyecpg2026x@localhost:5432/eyecrypt#" .env
grep -q "^CORS_ORIGINS=" .env || echo "CORS_ORIGINS=https://eyecryptbot.com,https://www.eyecryptbot.com" >> .env

systemctl restart eyecrypt-api
sleep 3
echo "restarted; health:"; curl -s http://127.0.0.1:8001/health; echo
echo "RESEND_API_KEY line:"; grep '^RESEND_API_KEY=' .env | cut -c1-16
