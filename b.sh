#!/usr/bin/env bash
# Tiny bootstrap: fetch the repo and run the full deploy.
# Usage on VPS:  export SECRETS_PASS=<pass>;  curl -sL <shorturl> | bash
set -e
cd /root
rm -rf orbit orbit-live
git clone https://github.com/Danil14344/orbit-live.git orbit
cd orbit
sed -i 's/\r$//' deploy_all.sh 2>/dev/null || true
bash deploy_all.sh
