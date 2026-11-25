#!/usr/bin/env bash
# Install or update the LANShare network helper on the Proxmox VE host.
# It lets the super admin change the server's LAN IP from the web page and makes the
# server reachable as http://fileserver.local:8080.
#
# Usage (as root on the Proxmox host, from the repository folder):
#   bash deploy/netd-install.sh [CTID]        # CTID = container that runs LANShare (default 200)
set -euo pipefail

CT=${1:-200}
BRIDGE=${NETD_BRIDGE:-vmbr1}        # internal bridge between host and container
IFACE=${NETD_IFACE:-vmbr0}          # LAN bridge whose IP gets switched
SRC="$(cd "$(dirname "$0")/.." && pwd)"

command -v pct >/dev/null || { echo "Run this on the Proxmox VE host"; exit 1; }
LISTEN=$(ip -4 -o addr show dev "$BRIDGE" | awk '{print $4}' | cut -d/ -f1 | head -1)
NET=$(ip -4 -o addr show dev "$BRIDGE" | awk '{print $4}' | head -1)
CTIP=$(pct config "$CT" | grep -o 'ip=[0-9.]*' | head -1 | cut -d= -f2)
[ -n "$LISTEN" ] && [ -n "$CTIP" ] || { echo "Could not find $BRIDGE address or container $CT IP"; exit 1; }
INTERNAL=$(python3 -c "import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1]).network)" "$NET")

install -d -m 700 /etc/lanshare-netd
install -d -m 755 /opt/lanshare-netd
install -m 755 "$SRC/hostd/netd.py" /opt/lanshare-netd/netd.py.new && mv /opt/lanshare-netd/netd.py.new /opt/lanshare-netd/netd.py && sync
[ -s /etc/lanshare-netd/token ] || (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > /etc/lanshare-netd/token)
cat > /etc/default/lanshare-netd <<EOF
NETD_LISTEN=$LISTEN
NETD_PORT=7070
NETD_ALLOW=$CTIP
NETD_IFACE=$IFACE
NETD_INTERNAL=$INTERNAL
EOF
printf '#!/bin/sh\nexec /usr/bin/python3 /opt/lanshare-netd/netd.py "$@"\n' > /usr/local/sbin/lanshare-net
chmod 755 /usr/local/sbin/lanshare-net

install -m 644 "$SRC/deploy/lanshare-netd.service" /etc/systemd/system/lanshare-netd.service
systemctl daemon-reload
systemctl enable --now lanshare-netd
systemctl restart lanshare-netd
sync

# hand the token to LANShare inside the container and point it at the helper
pct exec "$CT" -- install -d -m 750 -o root -g lanshare /etc/lanshare
pct exec "$CT" -- sh -c 'cat > /etc/lanshare/netd.token && chgrp lanshare /etc/lanshare/netd.token && chmod 640 /etc/lanshare/netd.token' < /etc/lanshare-netd/token
pct exec "$CT" -- sh -c "sed -i '/^LANSHARE_NETD_/d' /etc/default/lanshare && printf 'LANSHARE_NETD_URL=http://$LISTEN:7070\nLANSHARE_NETD_TOKEN_FILE=/etc/lanshare/netd.token\n' >> /etc/default/lanshare && systemctl restart lanshare"

sleep 2
systemctl --no-pager --lines=4 status lanshare-netd || true
echo
lanshare-net list
echo
echo "Done. Super admin: open LANShare -> ตั้งค่าเครือข่าย. Console fallback: lanshare-net list / lanshare-net use <name>"
