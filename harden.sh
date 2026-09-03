#!/usr/bin/env bash
# Hardening pass: local DB backups (daily, rotated) + ENV=prod (guarded) + grant admin.
# Set ADMIN_EMAIL=you@example.com before running to grant yourself admin.
set -e

echo "==== [1] local postgres backups (daily 03:00, keep 14) ===="
mkdir -p /root/backups
cat > /root/pg_backup.sh <<'EOF'
#!/usr/bin/env bash
set -e
TS=$(date +%Y%m%d_%H%M%S)
OUT=/root/backups/eyecrypt_$TS.sql.gz
sudo -u postgres pg_dump eyecrypt | gzip > "$OUT"
# keep newest 14
ls -1t /root/backups/eyecrypt_*.sql.gz | tail -n +15 | xargs -r rm -f
# offsite push if /root/backups is a git repo with a remote (set up separately)
if [ -d /root/backups/.git ] && git -C /root/backups remote get-url origin >/dev/null 2>&1; then
  git -C /root/backups add -A
  git -C /root/backups commit -m "backup $TS" >/dev/null 2>&1 || true
  git -C /root/backups push -q origin HEAD 2>/dev/null || true
fi
echo "backup done: $OUT"
EOF
chmod +x /root/pg_backup.sh

cat > /etc/systemd/system/pg-backup.service <<EOF
[Unit]
Description=eyecrypt postgres backup
[Service]
Type=oneshot
ExecStart=/root/pg_backup.sh
EOF
cat > /etc/systemd/system/pg-backup.timer <<EOF
[Unit]
Description=daily eyecrypt postgres backup
[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now pg-backup.timer
/root/pg_backup.sh

echo "==== [2] ENV=prod (only if JWT_SECRET is long enough) ===="
cd /root/eyecrypt-src/backend
JWTLEN=$(grep '^JWT_SECRET=' .env | cut -d= -f2- | tr -d '\r' | wc -c)
if [ "$JWTLEN" -ge 33 ]; then
  grep -q '^ENV=' .env && sed -i 's/^ENV=.*/ENV=prod/' .env || echo 'ENV=prod' >> .env
  systemctl restart eyecrypt-api
  sleep 3
  echo "ENV=prod set; health:"; curl -s http://127.0.0.1:8001/health; echo
else
  echo "SKIP ENV=prod — JWT_SECRET too short ($((JWTLEN-1)) chars); would brick API. Fix secret first."
fi

echo "==== [3] grant admin ===="
if [ -n "$ADMIN_EMAIL" ]; then
  sudo -u postgres psql -d eyecrypt -c "UPDATE users SET is_admin=true WHERE email='$ADMIN_EMAIL';"
  echo "current admins:"
  sudo -u postgres psql -d eyecrypt -tc "SELECT email FROM users WHERE is_admin=true;"
else
  echo "ADMIN_EMAIL not set — skipped. Re-run with: ADMIN_EMAIL=you@mail.com bash harden.sh"
fi

echo ""
echo "=== backups dir ==="; ls -la /root/backups/
echo "=== timer ==="; systemctl list-timers pg-backup.timer --no-pager | cat
echo "############ HARDEN DONE ############"
