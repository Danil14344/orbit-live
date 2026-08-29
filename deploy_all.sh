#!/usr/bin/env bash
# One-shot bot deploy on a fresh Ubuntu 24.04 VPS (run as root from /root/orbit).
# Sets everything up, restores .env, installs the systemd service — but does NOT
# start live trading. Ends with a READ-ONLY balance check for review.
set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"

echo "==== [1/5] system packages ===="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3-venv python3-pip git curl

echo "==== [2/5] python venv + deps ===="
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt cryptography

echo "==== [3/5] restore .env from secrets.enc ===="
if [ ! -s .env ]; then
  read -s -p "Paste PASSPHRASE then Enter: " SECRETS_PASS; echo
  SECRETS_PASS="$SECRETS_PASS" .venv/bin/python unmake_secrets.py
else
  echo ".env already present, skipping"
fi
echo "-- mode flags --"
grep -E "^(MODE|HEDGE_DRY_RUN|REBALANCE_DRY_RUN)=" .env || true

echo "==== [4/5] install systemd service (enabled, NOT started) ===="
SVC=/etc/systemd/system/orbit.service
sed -e "s|^User=.*|User=root|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$ROOT|" \
    -e "s|^ExecStart=.*|ExecStart=$ROOT/.venv/bin/python $ROOT/watchdog.py|" \
    orbit.service > "$SVC"
systemctl daemon-reload
systemctl enable orbit >/dev/null 2>&1 || true

echo "==== [5/5] READ-ONLY balance / key check (no trading) ===="
.venv/bin/python test_keys.py || true

echo ""
echo "############################################################"
echo "# DONE. Bot is installed + enabled but NOT started yet.    #"
echo "# Review balances above, then we set live flags & start.   #"
echo "############################################################"
