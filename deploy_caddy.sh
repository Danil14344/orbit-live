#!/usr/bin/env bash
# Put Caddy in front of the backend with automatic HTTPS for api.eyecryptbot.com.
# Run AFTER api.eyecryptbot.com DNS A-record points to this VPS (grey cloud).
set -e
DOMAIN=api.eyecryptbot.com

echo "==== [1/4] firewall: open 80/443 ===="
ufw allow 80/tcp  >/dev/null 2>&1 || true
ufw allow 443/tcp >/dev/null 2>&1 || true

echo "==== [2/4] install caddy ===="
export DEBIAN_FRONTEND=noninteractive
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -y
apt-get install -y caddy

echo "==== [3/4] Caddyfile ===="
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
    reverse_proxy 127.0.0.1:8001
}
EOF
systemctl restart caddy
echo "waiting for certificate (Let's Encrypt)..."
sleep 12

echo "==== [4/4] verify ===="
systemctl --no-pager status caddy | head -8 || true
echo "--- local backend ---"
curl -s http://127.0.0.1:8001/health; echo
echo "--- public https ---"
curl -sI https://$DOMAIN/health 2>&1 | head -5 || echo "(https not ready yet — DNS may still be propagating)"
echo ""
echo "############################################################"
echo "# Done. If https shows 200, api.eyecryptbot.com is live.   #"
echo "############################################################"
