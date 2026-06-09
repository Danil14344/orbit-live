#!/usr/bin/env bash
# One-shot VPS setup for the orbit arb bot (Ubuntu 22.04/24.04).
# Run from inside the orbit/ folder:  bash setup_vps.sh
set -e

echo "== updating system =="
sudo apt update && sudo apt -y upgrade

echo "== installing python, git, tmux, firewall =="
sudo apt -y install python3 python3-pip python3-venv git tmux ufw curl

echo "== python venv + deps =="
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "== firewall: allow SSH only (dashboard stays private) =="
sudo ufw allow OpenSSH || sudo ufw allow 22/tcp
sudo ufw --force enable

echo "== latency test to exchanges (lower = better; want < 100ms) =="
.venv/bin/python - <<'PY'
import asyncio, time
import ccxt.async_support as ccxt
async def p(n):
    e=getattr(ccxt,n)({"enableRateLimit":True,"timeout":15000})
    try:
        t=time.time(); await e.fetch_ticker("BTC/USDT"); print(f"  {n}: {(time.time()-t)*1000:.0f} ms")
    except Exception as ex: print(f"  {n}: FAIL {str(ex)[:50]}")
    finally: await e.close()
async def m():
    for n in ("mexc","bitget","bingx"): await p(n)
asyncio.run(m())
PY

echo "== installing systemd service (autostart on boot, auto-restart on crash) =="
SVC=/etc/systemd/system/orbit.service
# Render orbit.service with the current user + install dir so it works regardless
# of where the repo was cloned (template assumes /home/orbit/orbit).
sed -e "s|^User=.*|User=$(id -un)|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$(pwd)|" \
    -e "s|^ExecStart=.*|ExecStart=$(pwd)/.venv/bin/python $(pwd)/watchdog.py|" \
    orbit.service | sudo tee "$SVC" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable orbit.service

echo ""
echo "== DONE =="
echo "Next:"
echo "  1) make sure .env is filled with your API keys and MODE=live"
echo "  2) start the bot:        sudo systemctl start orbit"
echo "     check status:         systemctl status orbit"
echo "     follow logs:          journalctl -u orbit -f"
echo "     bot's own logs:       tail -f logs/watchdog.log logs/scanner.stdout.log"
echo "     stop:                 sudo systemctl stop orbit"
