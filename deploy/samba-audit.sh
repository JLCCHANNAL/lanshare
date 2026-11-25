#!/usr/bin/env bash
# Record file operations made through the Windows share (Samba) so they show up in
# LANShare's activity log (บันทึกกิจกรรม). Safe to run more than once.
# Usage (as root, in the container that runs Samba + LANShare):  bash deploy/samba-audit.sh
set -euo pipefail
CONF=/etc/samba/smb.conf
[ -f "$CONF" ] || { echo "Samba is not installed here - nothing to do"; exit 0; }
cp "$CONF" "$CONF.lanshare-bak"

python3 - "$CONF" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()

def add_full_audit(m):
    objs = m.group(2).split()
    if "full_audit" not in objs:
        objs.insert(0, "full_audit")      # first in the stack: sees the original delete, not the recycle move
    return m.group(1) + " ".join(objs)

new = re.sub(r"(?m)^(\s*vfs objects\s*=\s*)(.*)$", add_full_audit, text)
if not re.search(r"(?m)^\s*vfs objects", new):
    new = re.sub(r"(?m)^\[global\][ \t]*$", "[global]\n   vfs objects = full_audit", new, count=1)
settings = {
    "full_audit:prefix": "%u|%I|%S",
    "full_audit:success": "mkdirat renameat unlinkat openat",
    "full_audit:failure": "none",
    "full_audit:facility": "LOCAL5",
    "full_audit:priority": "NOTICE",
}
for key, value in settings.items():
    if not re.search(rf"(?m)^\s*{re.escape(key)}\s*=", new):
        new = re.sub(r"(?m)^\[global\][ \t]*$", f"[global]\n   {key} = {value}", new, count=1)
if new != text:
    open(path, "w", encoding="utf-8").write(new)
    print("smb.conf: Windows-share activity logging turned on")
else:
    print("smb.conf: already set")
PY

if ! testparm -s >/dev/null 2>&1; then
  echo "smb.conf check failed - restoring the previous file"
  cp "$CONF.lanshare-bak" "$CONF"
  exit 1
fi
systemctl reload smbd 2>/dev/null || systemctl restart smbd
# let LANShare read those log lines
if id lanshare >/dev/null 2>&1; then
  usermod -aG systemd-journal lanshare
  systemctl restart lanshare 2>/dev/null || true
fi
echo "Done."
