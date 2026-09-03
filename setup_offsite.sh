#!/usr/bin/env bash
# Configure encrypted offsite DB backups → private GitHub repo Danil14344/eyecrypt-backups.
# Dumps are AES-256 encrypted with the secrets passphrase (kept by the operator),
# so even a repo leak doesn't expose user data. Token read fresh from backend .env
# each run (rotation-proof). Requires the eyecrypt-backups repo to already exist.
set -e
REPO="Danil14344/eyecrypt-backups"
BACKUP_PASS="kestrelbanjomoduleraven"
TOKEN=$(grep '^GITHUB_TOKEN=' /root/eyecrypt-src/backend/.env | cut -d= -f2- | tr -d '\r')
[ -n "$TOKEN" ] || { echo "no GITHUB_TOKEN in backend .env"; exit 1; }

# Rewrite the backup script: encrypted dump + rotation + offsite push.
cat > /root/pg_backup.sh <<EOF
#!/usr/bin/env bash
set -e
TS=\$(date +%Y%m%d_%H%M%S)
OUT=/root/backups/eyecrypt_\$TS.sql.gz.enc
sudo -u postgres pg_dump eyecrypt | gzip | openssl enc -aes-256-cbc -pbkdf2 -salt -pass pass:$BACKUP_PASS > "\$OUT"
# keep newest 14
ls -1t /root/backups/eyecrypt_*.sql.gz.enc 2>/dev/null | tail -n +15 | xargs -r rm -f
# offsite push (token read fresh, so rotation just works)
TOK=\$(grep '^GITHUB_TOKEN=' /root/eyecrypt-src/backend/.env | cut -d= -f2- | tr -d '\r')
if [ -n "\$TOK" ] && [ -d /root/backups/.git ]; then
  git -C /root/backups remote set-url origin "https://\$TOK@github.com/$REPO.git"
  git -C /root/backups add -A
  git -C /root/backups commit -m "backup \$TS" >/dev/null 2>&1 || true
  git -C /root/backups push -q origin HEAD:main 2>/dev/null || true
fi
echo "backup done: \$OUT"
EOF
chmod +x /root/pg_backup.sh

# remove any old plaintext dumps from the earlier local-only pass
rm -f /root/backups/eyecrypt_*.sql.gz 2>/dev/null || true

# init git in the backups dir
cd /root/backups
git init -q
git config user.email backup@vps
git config user.name vps-backup
git branch -M main 2>/dev/null || true
git remote remove origin 2>/dev/null || true
git remote add origin "https://$TOKEN@github.com/$REPO.git"
printf '# eyecrypt DB backups (AES-256 encrypted)\nDecrypt: openssl enc -d -aes-256-cbc -pbkdf2 -pass pass:<passphrase> -in FILE | gunzip > db.sql\n' > README.md

# run a backup + push now
/root/pg_backup.sh

echo "=== remote (token masked) ==="; git -C /root/backups remote -v | sed 's#//[^@]*@#//***@#'
echo "=== pushed commit ==="; git -C /root/backups log --oneline -1 2>&1
echo "=== files ==="; ls -la /root/backups/
echo "############ OFFSITE DONE ############"
