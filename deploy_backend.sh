#!/usr/bin/env bash
# Deploy the eyecrypt backend on the VPS: Postgres + FastAPI (uvicorn) service.
# Backend .env is reused from the earlier orbit deploy (/root/orbit/eyecrypt/backend/.env).
set -e
PGPASS="eyecpg2026x"
SRC=/root/eyecrypt-src

echo "==== [1/6] packages (postgres, python) ===="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y postgresql python3-venv python3-pip git curl
systemctl enable --now postgresql

echo "==== [2/6] postgres role + database ===="
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='eyecrypt'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE USER eyecrypt WITH PASSWORD '$PGPASS'"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='eyecrypt'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE DATABASE eyecrypt OWNER eyecrypt"

echo "==== [3/6] clone backend code ===="
cd /root
rm -rf "$SRC"
git clone https://github.com/Danil14344/eyecrypt.git "$SRC"

echo "==== [4/6] place .env + point DB at local postgres ===="
if [ ! -s /root/orbit/eyecrypt/backend/.env ]; then
  echo "!! backend .env not found at /root/orbit/eyecrypt/backend/.env — run the orbit bootstrap first"; exit 1
fi
cp /root/orbit/eyecrypt/backend/.env "$SRC/backend/.env"
cd "$SRC/backend"
sed -i 's/\r$//' .env
sed -i "s#^DATABASE_URL=.*#DATABASE_URL=postgresql+psycopg2://eyecrypt:$PGPASS@localhost:5432/eyecrypt#" .env
grep -q "^CORS_ORIGINS=" .env || echo "CORS_ORIGINS=https://eyecryptbot.com,https://www.eyecryptbot.com" >> .env

echo "==== [5/6] venv + deps ===="
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "==== [6/6] systemd service (uvicorn on 127.0.0.1:8001) ===="
cat > /etc/systemd/system/eyecrypt-api.service <<EOF
[Unit]
Description=Eye Crypt backend API
After=network-online.target postgresql.service
Wants=network-online.target
[Service]
WorkingDirectory=$SRC/backend
ExecStart=$SRC/backend/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8001
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable eyecrypt-api >/dev/null 2>&1 || true
systemctl restart eyecrypt-api
sleep 4

echo ""
echo "==== health check ===="
curl -s http://127.0.0.1:8001/health || echo "(no response)"
echo ""
systemctl --no-pager -l status eyecrypt-api | head -12 || true
echo ""
echo "############################################################"
echo "# Backend deployed on 127.0.0.1:8001.                      #"
echo "# Next: point DNS api.eyecryptbot.com -> this IP, then Caddy.#"
echo "############################################################"
