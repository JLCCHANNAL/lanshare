#!/usr/bin/env python3
"""LANShare network helper — runs as root on the Proxmox VE host (standard library only).

It does exactly three things for the LANShare web app running in the container:
  * keeps a list of LAN "profiles" (office, home, ...) and switches the host bridge
    (vmbr0) to one of them: address + gateway in /etc/network/interfaces, /etc/hosts,
    then `ifreload -a`;
  * optionally picks the matching profile by itself at boot (gateway answers ARP);
  * answers mDNS so the server is reachable as http://<name>.local:8080 whatever its IP.

The HTTP API listens only on the internal bridge (vmbr1) and accepts only the
container's IP plus a shared token. Nothing else on the host can be changed through it.

  netd.py serve            run the service (systemd: lanshare-netd.service)
  netd.py list             show profiles and the current address
  netd.py use <label|id>   switch now (use from the VMware console if the web page is unreachable)
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LISTEN = os.environ.get("NETD_LISTEN", "10.10.10.1")
PORT = int(os.environ.get("NETD_PORT", "7070"))
ALLOW = {a.strip() for a in os.environ.get("NETD_ALLOW", "10.10.10.20").split(",") if a.strip()}
IFACE = os.environ.get("NETD_IFACE", "vmbr0")
INTERNAL = ipaddress.IPv4Network(os.environ.get("NETD_INTERNAL", "10.10.10.0/24"))
DIR = Path(os.environ.get("NETD_DIR", "/etc/lanshare-netd"))
INTERFACES = Path(os.environ.get("NETD_INTERFACES", "/etc/network/interfaces"))
HOSTS = Path(os.environ.get("NETD_HOSTS", "/etc/hosts"))
DRY = os.environ.get("NETD_DRYRUN") == "1"          # tests: edit files, run no commands
MDNS = os.environ.get("NETD_MDNS", "1") == "1"
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
MAX_PROFILES = 12
MDNS_GROUP = "224.0.0.251"

_lock = threading.RLock()
_applying: str | None = None
_mdns_sock: socket.socket | None = None
STATE: dict = {}


class NetError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.msg, self.status = msg, status


def log(msg: str) -> None:
    sys.stderr.write(f"lanshare-netd: {msg}\n")
    sys.stderr.flush()

# ---------------------------------------------------------------- state


def load_state() -> dict:
    try:
        st = json.loads((DIR / "state.json").read_text("utf-8"))
    except (OSError, ValueError):
        st = {}
    st.setdefault("name", "fileserver")
    st.setdefault("auto", True)
    st.setdefault("profiles", [])
    return st


def save_state(st: dict) -> None:
    global STATE
    DIR.mkdir(parents=True, exist_ok=True)
    write_atomic(DIR / "state.json", json.dumps(st, ensure_ascii=False, indent=1) + "\n")
    STATE = st


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".lanshare-tmp")
    tmp.write_text(text, "utf-8")
    try:
        os.chmod(tmp, path.stat().st_mode & 0o7777)
    except OSError:
        pass
    os.replace(tmp, path)

# ---------------------------------------------------------------- /etc/network/interfaces


def _stanza(lines: list[str], iface: str) -> tuple[int, int]:
    """(header index, end index) of the `iface <iface> inet ...` block."""
    for i, ln in enumerate(lines):
        if re.match(rf"^iface\s+{re.escape(iface)}\s+inet\s+\S+", ln):
            j = i + 1
            while j < len(lines) and (lines[j][:1] in (" ", "\t") or not lines[j].strip()):
                j += 1
            while j > i + 1 and not lines[j - 1].strip():
                j -= 1
            return i, j
    raise NetError(f"ไม่พบการตั้งค่า {iface} ใน {INTERFACES}", 500)


def read_config(text: str) -> dict:
    lines = text.splitlines()
    i, j = _stanza(lines, IFACE)
    cfg = {"method": lines[i].split()[3]}
    for ln in lines[i + 1:j]:
        t = ln.split()
        if len(t) >= 2 and t[0] in ("address", "gateway"):
            cfg[t[0]] = t[1]
    return cfg


def rewrite_interfaces(text: str, address: str, gateway: str) -> str:
    lines = text.splitlines()
    i, j = _stanza(lines, IFACE)
    lines[i] = re.sub(r"(\binet\s+)\S+", r"\1static", lines[i], count=1)
    body = lines[i + 1:j]
    indent = next((re.match(r"^\s+", ln).group(0) for ln in body if ln.strip()), "\t")
    body = [ln for ln in body if not (ln.split() and ln.split()[0] in ("address", "gateway", "netmask"))]
    lines[i + 1:j] = [f"{indent}address {address}", f"{indent}gateway {gateway}", *body]
    return "\n".join(lines) + "\n"


def rewrite_hosts(text: str, ip: str, hostname: str) -> str:
    out = []
    for ln in text.splitlines():
        t = ln.split()
        if (len(t) > 1 and hostname in t[1:] and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", t[0])
                and not t[0].startswith("127.")):
            ln = ln.replace(t[0], ip, 1)
        out.append(ln)
    return "\n".join(out) + "\n"


def current() -> dict:
    try:
        return read_config(INTERFACES.read_text("utf-8"))
    except (OSError, NetError):
        return {}


def live_ip() -> str | None:
    """The address really configured on the interface right now."""
    if not DRY:
        try:
            import fcntl
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                r = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", IFACE.encode()[:15]))  # SIOCGIFADDR
            return socket.inet_ntoa(r[20:24])
        except (ImportError, OSError):
            pass
    return (current().get("address") or "").split("/")[0] or None


def iface_addrs() -> list[str]:
    if DRY:
        return [a for a in [current().get("address")] if a]
    out = run(["ip", "-4", "-o", "addr", "show", "dev", IFACE])
    return re.findall(r"inet (\S+)", out)


def run(cmd: list[str], timeout: int = 60) -> str:
    if DRY:
        log("dry-run: " + " ".join(cmd))
        return ""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        log(f"{' '.join(cmd)} -> {r.returncode}: {r.stderr.strip()[:300]}")
    return r.stdout

# ---------------------------------------------------------------- profiles


def clean_profile(p: dict) -> dict:
    label = str(p.get("label", "")).strip()
    if not 1 <= len(label) <= 40:
        raise NetError("ชื่อเรียกเครือข่ายต้องยาว 1–40 ตัวอักษร")
    try:
        addr = ipaddress.IPv4Interface(str(p.get("address", "")).strip())
        gw = ipaddress.IPv4Address(str(p.get("gateway", "")).strip())
    except ValueError:
        raise NetError(f"IP หรือ Gateway ของ “{label}” ไม่ถูกต้อง")
    net = addr.network
    if not 8 <= net.prefixlen <= 30:
        raise NetError(f"Subnet mask ของ “{label}” ไม่ถูกต้อง")
    if addr.ip in (net.network_address, net.broadcast_address) or addr.ip.is_loopback or addr.ip.is_multicast:
        raise NetError(f"IP ของ “{label}” ใช้ไม่ได้ (เป็นเลขพิเศษของวง)")
    if gw not in net or gw in (net.network_address, net.broadcast_address):
        raise NetError(f"Gateway ของ “{label}” ต้องอยู่วงเดียวกับ IP เครื่อง")
    if gw == addr.ip:
        raise NetError(f"IP เครื่องของ “{label}” ต้องไม่ซ้ำกับ Gateway")
    if net.overlaps(INTERNAL):
        raise NetError(f"วง {net} ชนกับเครือข่ายภายในของเซิร์ฟเวอร์ ({INTERNAL})")
    pid = str(p.get("id") or "")
    if not re.fullmatch(r"[a-z0-9]{6,16}", pid):
        pid = secrets.token_hex(4)
    return {"id": pid, "label": label, "address": str(addr), "gateway": str(gw)}


def active_id(st: dict, cur: dict) -> str | None:
    return next((p["id"] for p in st["profiles"]
                 if p["address"] == cur.get("address") and p["gateway"] == cur.get("gateway")), None)


def public_state() -> dict:
    st, cur = STATE or load_state(), current()
    return {"name": st["name"], "auto": st["auto"],
            "profiles": [{k: p[k] for k in ("id", "label", "address", "gateway")} for p in st["profiles"]],
            "active": active_id(st, cur), "applying": _applying, "iface": IFACE,
            "current": {"address": cur.get("address"), "gateway": cur.get("gateway"),
                        "method": cur.get("method"), "ip": live_ip()}}


def update_state(body: dict) -> dict:
    with _lock:
        st = load_state()
        if "name" in body:
            name = str(body["name"]).strip().lower()
            if not NAME_RE.fullmatch(name):
                raise NetError("ชื่อเครื่องใช้ได้เฉพาะ a-z, 0-9 และขีด (-) ยาวไม่เกิน 63 ตัว")
            st["name"] = name
        if "auto" in body:
            st["auto"] = bool(body["auto"])
        if "profiles" in body:
            ps = body["profiles"]
            if not isinstance(ps, list) or len(ps) > MAX_PROFILES:
                raise NetError(f"บันทึกได้ไม่เกิน {MAX_PROFILES} เครือข่าย")
            old = {p["id"]: p for p in st["profiles"]}
            out, seen = [], set()
            for p in ps:
                c = clean_profile(p if isinstance(p, dict) else {})
                if c["id"] in seen:
                    c["id"] = secrets.token_hex(4)
                seen.add(c["id"])
                o = old.get(c["id"])
                if o and o.get("mac") and o["gateway"] == c["gateway"]:
                    c["mac"] = o["mac"]          # keep the learned router identity
                out.append(c)
            st["profiles"] = out
        save_state(st)
    if "name" in body:
        announce()
    return public_state()


def find_profile(key: str) -> dict:
    for p in load_state()["profiles"]:
        if key in (p["id"], p["label"]):
            return p
    raise NetError("ไม่พบเครือข่ายนี้", 404)


def apply_profile(p: dict, delay: float = 0.0, wait: bool = False) -> None:
    """Write the profile into the host config and reload networking."""
    global _applying

    def work():
        global _applying
        try:
            time.sleep(delay)
            with _lock:
                old = INTERFACES.read_text("utf-8")
                new = rewrite_interfaces(old, p["address"], p["gateway"])
                if new != old:
                    (INTERFACES.with_name(INTERFACES.name + ".lanshare-bak")).write_text(old, "utf-8")
                    write_atomic(INTERFACES, new)
                ip = p["address"].split("/")[0]
                try:
                    h = HOSTS.read_text("utf-8")
                    h2 = rewrite_hosts(h, ip, socket.gethostname().split(".")[0])
                    if h2 != h:
                        write_atomic(HOSTS, h2)
                except OSError as e:
                    log(f"hosts: {e}")
                run(["ifreload", "-a"], timeout=120)
            log(f"switched {IFACE} to {p['label']} ({p['address']} via {p['gateway']})")
            announce()
        except Exception as e:  # keep the service alive
            log(f"apply failed: {e!r}")
        finally:
            _applying = None

    _applying = p["id"]
    if wait:
        work()
    else:
        threading.Thread(target=work, daemon=True).start()

# ---------------------------------------------------------------- detection


def gateway_mac(src_cidr: str, gw: str) -> str | None:
    """Ask the gateway for its MAC from `src_cidr` (temporarily added if needed)."""
    if DRY:
        return None
    have = iface_addrs()
    temp = src_cidr not in have
    same_net = next((a for a in have if ipaddress.IPv4Interface(a).network == ipaddress.IPv4Interface(src_cidr).network), None)
    if temp and same_net:                      # already on that LAN: probe from the real address
        src_cidr, temp = same_net, False
    src = src_cidr.split("/")[0]
    try:
        if temp:
            run(["ip", "addr", "add", src_cidr, "dev", IFACE])
        run(["ip", "neigh", "flush", "to", gw, "dev", IFACE])
        subprocess.run(["ping", "-c", "2", "-W", "1", "-I", src, gw], capture_output=True, timeout=8)
        out = run(["ip", "neigh", "show", "to", gw, "dev", IFACE])
        m = re.search(r"lladdr ([0-9a-f:]{17})", out)
        return m.group(1) if m and "FAILED" not in out and "INCOMPLETE" not in out else None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if temp:
            run(["ip", "addr", "del", src_cidr, "dev", IFACE])


def probe() -> dict[str, bool]:
    """Which saved networks are reachable right now (their router answers, with the same MAC as before)."""
    with _lock:
        st = load_state()
        cur = active_id(st, current())
        order = sorted(st["profiles"], key=lambda p: p["id"] != cur)
        res, learned = {}, False
        for p in order:
            mac = gateway_mac(p["address"], p["gateway"])
            res[p["id"]] = bool(mac) and (not p.get("mac") or p["mac"] == mac)
            if mac and p["id"] == cur and p.get("mac") != mac:
                p["mac"], learned = mac, True
        if learned:
            save_state(st)
        return res


def auto_select() -> None:
    st = load_state()
    if not st["auto"] or not st["profiles"]:
        return
    time.sleep(3)
    for _ in range(6):                          # link may come up late after boot
        res = probe()
        st = load_state()
        cur = active_id(st, current())
        if cur and res.get(cur):
            log("auto: current network is fine")
            return
        hit = next((p for p in st["profiles"] if res.get(p["id"])), None)
        if hit:
            log(f"auto: found {hit['label']}")
            apply_profile(hit, wait=True)
            return
        time.sleep(5)
    log("auto: no saved network answered; keeping the current one")

# ---------------------------------------------------------------- mDNS (<name>.local)


def dns_name(data: bytes, off: int) -> tuple[str, int]:
    labels, end, hops = [], None, 0
    while True:
        n = data[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            if end is None:
                end = off + 2
            off = ((n & 0x3F) << 8) | data[off + 1]
            hops += 1
            if hops > 20:
                raise ValueError("loop")
            continue
        labels.append(data[off + 1:off + 1 + n].decode("utf-8", "replace"))
        off += 1 + n
    return ".".join(labels), end if end is not None else off


def enc_name(name: str) -> bytes:
    return b"".join(bytes([len(x)]) + x for x in (lbl.encode() for lbl in name.strip(".").split("."))) + b"\0"


def a_record(name: str, ip: str, legacy: bool) -> bytes:
    return enc_name(name) + struct.pack("!HHIH", 1, 0x0001 if legacy else 0x8001, 10 if legacy else 120, 4) + socket.inet_aton(ip)


def mdns_answer(query: bytes, name: str, ip: str, legacy: bool) -> bytes | None:
    """Response to an mDNS query asking for <name>.local (A or ANY), else None."""
    try:
        qid, flags, qd = struct.unpack("!HHH", query[:6])
        if flags & 0x8000:
            return None
        off, hit, want = 12, None, f"{name}.local"
        for _ in range(qd):
            qn, off = dns_name(query, off)
            qtype, qclass = struct.unpack("!HH", query[off:off + 4])
            off += 4
            if qn.lower().rstrip(".") == want and qtype in (1, 255) and hit is None:
                hit = (qn, qtype, qclass)
    except (ValueError, IndexError, struct.error):
        return None
    if not hit:
        return None
    qsec = enc_name(hit[0]) + struct.pack("!HH", hit[1], hit[2] & 0x7FFF) if legacy else b""
    hdr = struct.pack("!HHHHHH", qid if legacy else 0, 0x8400, 1 if legacy else 0, 1, 0, 0)
    return hdr + qsec + a_record(want, ip, legacy)


def announce() -> None:
    s, ip = _mdns_sock, live_ip()
    if not s or not ip:
        return
    pkt = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + a_record(f"{(STATE or load_state())['name']}.local", ip, False)

    def send():
        for _ in range(2):
            try:
                s.sendto(pkt, (MDNS_GROUP, 5353))
            except OSError:
                pass
            time.sleep(1)
    threading.Thread(target=send, daemon=True).start()


def mdns_loop() -> None:
    global _mdns_sock
    while True:
        try:
            idx = socket.if_nametoindex(IFACE)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            s.bind(("", 5353))
            mreqn = struct.pack("4s4si", socket.inet_aton(MDNS_GROUP), socket.inet_aton("0.0.0.0"), idx)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreqn)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, mreqn)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
            _mdns_sock = s
            log(f"mDNS: answering {(STATE or load_state())['name']}.local on {IFACE}")
            announce()
            while True:
                data, (src, sport) = s.recvfrom(9000)
                ip = live_ip()
                legacy = sport != 5353
                resp = ip and mdns_answer(data, (STATE or load_state())["name"], ip, legacy)
                if not resp:
                    continue
                if legacy:
                    s.sendto(resp, (src, sport))
                else:
                    s.sendto(resp, (MDNS_GROUP, 5353))
                    s.sendto(resp, (src, 5353))  # direct copy: passes desktop firewalls that drop multicast
        except Exception as e:
            log(f"mDNS error: {e!r}")
            _mdns_sock = None
            time.sleep(5)

# ---------------------------------------------------------------- HTTP API (container only)


def token() -> str:
    try:
        return (DIR / "token").read_text().strip()
    except OSError:
        return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "lanshare-netd"

    def log_message(self, fmt, *args):
        pass

    def reply(self, status: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            raise NetError("คำขอใหญ่เกินไป", 413)
        try:
            b = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise NetError("ข้อมูลไม่ถูกต้อง")
        if not isinstance(b, dict):
            raise NetError("ข้อมูลไม่ถูกต้อง")
        return b

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method: str) -> None:
        try:
            tok, got = token(), self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if (ALLOW and self.client_address[0] not in ALLOW) or not tok or not hmac.compare_digest(got, tok):
                raise NetError("ไม่อนุญาต", 403)
            path = self.path.split("?")[0]
            if method == "GET" and path == "/state":
                return self.reply(200, public_state())
            if method == "POST" and path == "/state":
                return self.reply(200, update_state(self.body()))
            if method == "POST" and path == "/probe":
                return self.reply(200, {"reachable": probe()})
            if method == "POST" and path == "/apply":
                if _applying:
                    raise NetError("กำลังเปลี่ยนเครือข่ายอยู่ กรุณารอสักครู่", 409)
                p = find_profile(str(self.body().get("id", "")))
                apply_profile(p, delay=1.5)           # answer first, then switch
                return self.reply(200, {"ok": True, "label": p["label"], "ip": p["address"].split("/")[0],
                                        "name": load_state()["name"]})
            raise NetError("ไม่พบ", 404)
        except NetError as e:
            self.reply(e.status, {"error": e.msg})
        except Exception as e:
            log(f"error: {e!r}")
            self.reply(500, {"error": "ตัวช่วยเครือข่ายทำงานผิดพลาด"})


def serve() -> None:
    global STATE
    STATE = load_state()
    if not STATE["profiles"]:                   # first run: remember the network we are on now
        cur = current()
        if cur.get("address") and cur.get("gateway"):
            try:
                STATE["profiles"] = [clean_profile({"label": "ที่ทำงาน", **cur})]
                save_state(STATE)
            except NetError as e:
                log(f"seed: {e.msg}")
    if MDNS:
        threading.Thread(target=mdns_loop, daemon=True).start()
    if not DRY:
        threading.Thread(target=auto_select, daemon=True).start()
    httpd = ThreadingHTTPServer((LISTEN, PORT), Handler)
    log(f"API on {LISTEN}:{PORT} (allowed: {', '.join(sorted(ALLOW)) or 'any'})")
    httpd.serve_forever()


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "list":
        st, cur = load_state(), current()
        act = active_id(st, cur)
        print(f"ชื่อเครื่อง: {st['name']}.local   ตอนนี้: {cur.get('address')} via {cur.get('gateway')}")
        for p in st["profiles"]:
            print(f"  {'*' if p['id'] == act else ' '} {p['label']:<20} {p['address']:<20} gw {p['gateway']}")
    elif cmd == "use" and len(argv) > 2:
        try:
            p = find_profile(" ".join(argv[2:]))
        except NetError as e:
            print(e.msg)
            return 1
        apply_profile(p, wait=True)
        print(f"เปลี่ยนเป็น {p['label']} แล้ว: http://{load_state()['name']}.local:8080  หรือ  http://{p['address'].split('/')[0]}:8080")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
