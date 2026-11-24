#!/usr/bin/env bash
# Install or update LANShare on Debian 12/13 (also inside a Proxmox LXC container).
# Usage (as root, from the repository folder):   bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/lanshare
DATA_DIR=/var/lib/lanshare
SHARE_DIR=${LANSHARE_ROOT:-/srv/share}
SRC="$(cd "$(dirname "$0")/.." && pwd)"

command -v python3 >/dev/null || { apt-get update -qq && apt-get install -y -qq python3; }
python3 - <<'PY'
import sys
assert sys.version_info >= (3, 11), "LANShare needs Python 3.11 or newer"
PY

id lanshare >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin lanshare
# let the app read/write every department folder that already uses group ownership (e.g. Samba groups)
for g in $(stat -c %G "$SHARE_DIR"/* 2>/dev/null | sort -u); do
  [ "$g" != root ] && usermod -aG "$g" lanshare || true
done

mkdir -p "$APP_DIR" "$DATA_DIR" "$SHARE_DIR"
rm -rf "$APP_DIR/lanshare"
cp -r "$SRC/lanshare" "$APP_DIR/"
chown -R lanshare:lanshare "$DATA_DIR"
# top level: only the app (superadmin/editor) may create department folders
chgrp lanshare "$SHARE_DIR" && chmod 2775 "$SHARE_DIR"

install -m 644 "$SRC/deploy/lanshare.service" /etc/systemd/system/lanshare.service
[ -f /etc/default/lanshare ] || install -m 640 "$SRC/deploy/lanshare.env" /etc/default/lanshare
# organisation name shown on every page (asked once; change later in the web UI → ตั้งค่าระบบ)
# non-interactive: LANSHARE_ORG="JLC Industry" bash deploy/install.sh
if [ -z "${LANSHARE_ORG:-}" ] && [ -t 0 ] && [ ! -s "$DATA_DIR/lanshare.db" ]; then
  read -rp "ชื่อองค์กร (เช่น JLC Industry): " LANSHARE_ORG || true
fi
if [ -n "${LANSHARE_ORG:-}" ]; then
  (cd "$APP_DIR" && runuser -u lanshare -- env $(grep -v '^#' /etc/default/lanshare | xargs) \
     python3 -m lanshare.server set-name "$LANSHARE_ORG")
fi
systemctl daemon-reload
systemctl enable --now lanshare
systemctl restart lanshare
sleep 1
systemctl --no-pager --lines=3 status lanshare || true
echo
echo "Done. Create the first administrator with:"
echo "  sudo -u lanshare env \$(grep -v '^#' /etc/default/lanshare | xargs) python3 -m lanshare.server create-admin admin"
echo "  (run from $APP_DIR)"
