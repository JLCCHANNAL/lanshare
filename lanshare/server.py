#!/usr/bin/env python3
"""LANShare — a small Thai-language file-sharing web app for an office LAN.

Standard library only (Python 3.11+). Serves files from department folders under a
single root, with three roles, live updates over Server-Sent Events, and a recycle bin.

Run:  python3 -m lanshare.server serve
Admin: python3 -m lanshare.server create-admin <username>
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import html
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------- configuration

ROOT = Path(os.environ.get("LANSHARE_ROOT", "/srv/share")).resolve()
DATA = Path(os.environ.get("LANSHARE_DATA", "/var/lib/lanshare"))
HOST = os.environ.get("LANSHARE_HOST", "0.0.0.0")
PORT = int(os.environ.get("LANSHARE_PORT", "8080"))
MAX_UPLOAD = int(os.environ.get("LANSHARE_MAX_UPLOAD", str(4 * 1024**3)))  # bytes per file
SESSION_HOURS = int(os.environ.get("LANSHARE_SESSION_HOURS", "12"))
APP_NAME = os.environ.get("LANSHARE_NAME", "แชร์ไฟล์องค์กร")
NETD_URL = os.environ.get("LANSHARE_NETD_URL", "").rstrip("/")   # optional host network helper
NETD_TOKEN_FILE = Path(os.environ.get("LANSHARE_NETD_TOKEN_FILE", "/etc/lanshare/netd.token"))
STATIC = Path(__file__).resolve().parent / "static"
RECYCLE = ".lanshare-recycle"           # hidden folder inside ROOT
ROLES = ("superadmin", "editor", "user")
NAME_RE = re.compile(r'^[^/\\:*?"<>|\x00-\x1f]{1,200}$')
USER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
MIN_PASSWORD = 8

# ---------------------------------------------------------------- passwords

def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, want = stored.split("$")
        dk = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(dk.hex(), want)
    except (ValueError, TypeError):
        return False

# ---------------------------------------------------------------- database

_db_lock = threading.Lock()


def db() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA / "lanshare.db", timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db() -> None:
    with _db_lock, db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
              id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('superadmin','editor','user')),
              pw_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS user_depts(
              user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, dept TEXT NOT NULL,
              PRIMARY KEY(user_id, dept));
            CREATE TABLE IF NOT EXISTS sessions(
              token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              expires INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(
              ts INTEGER NOT NULL, username TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chat_messages(
              id INTEGER PRIMARY KEY AUTOINCREMENT, conv TEXT NOT NULL, sender INTEGER NOT NULL,
              sender_name TEXT NOT NULL, body TEXT NOT NULL, ts INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS chat_conv ON chat_messages(conv, id);
            CREATE TABLE IF NOT EXISTS chat_reactions(
              msg_id INTEGER NOT NULL, user_id INTEGER NOT NULL, emoji TEXT NOT NULL, ts INTEGER NOT NULL,
              PRIMARY KEY(msg_id, user_id));
            CREATE TABLE IF NOT EXISTS chat_read(
              user_id INTEGER NOT NULL, conv TEXT NOT NULL, last_id INTEGER NOT NULL,
              PRIMARY KEY(user_id, conv));
            """
        )
        cols = {r[1] for r in con.execute("PRAGMA table_info(audit)")}
        if "ip" not in cols:
            con.execute("ALTER TABLE audit ADD COLUMN ip TEXT NOT NULL DEFAULT ''")
        if "source" not in cols:
            con.execute("ALTER TABLE audit ADD COLUMN source TEXT NOT NULL DEFAULT 'web'")
        con.execute("CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts)")
        if "image" not in {r[1] for r in con.execute("PRAGMA table_info(chat_messages)")}:
            con.execute("ALTER TABLE chat_messages ADD COLUMN image TEXT NOT NULL DEFAULT ''")


DEFAULT_TAGLINE = "ระบบแชร์ไฟล์ภายในองค์กร"


def get_setting(key: str, default: str) -> str:
    try:
        with db() as con:
            row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:          # table not created yet
        return default
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _db_lock, db() as con:
        con.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def org() -> dict:
    return {"name": get_setting("org_name", APP_NAME), "tagline": get_setting("org_tagline", DEFAULT_TAGLINE)}


def clean_text(v, field: str, lo: int, hi: int) -> str:
    s = re.sub(r"\s+", " ", str(v or "")).strip()
    if not lo <= len(s) <= hi or re.search(r"[\x00-\x1f<>]", s):
        raise ApiError(400, f"{field}ต้องยาว {lo}–{hi} ตัวอักษร และห้ามมีเครื่องหมาย < >")
    return s


_req = threading.local()          # client IP of the request being handled (for the activity log)
_recent: dict[tuple, float] = {}  # repeated downloads/previews within a minute are logged once


def audit(user: dict, action: str, detail: str, *, ip: str | None = None, source: str = "web", ts: int | None = None) -> None:
    ip = ip if ip is not None else getattr(_req, "ip", "")
    if action in ("download", "preview", "smb-write", "view-chatlog"):
        key, now = (user["username"], action, detail), time.time()
        if now - _recent.get(key, 0) < 60:
            return
        _recent[key] = now
        if len(_recent) > 5000:
            _recent.clear()
    with _db_lock, db() as con:
        con.execute("INSERT INTO audit(ts, username, action, detail, ip, source) VALUES(?,?,?,?,?,?)",
                    (ts or int(time.time()), user["username"], action, detail, ip or "", source))

# ---------------------------------------------------------------- Windows-drive (Samba) activity
# Samba's full_audit module writes one syslog line per operation, e.g.
#   smbd_audit: acc01|192.168.100.145|Accounting|unlinkat|ok|sub/report.xlsx
# A background thread reads them from the journal and stores them next to the web activity.

SMB_NOISE = re.compile(r"(^|/)(~\$|\.~lock|~WRL|Thumbs\.db$|desktop\.ini$|\.DS_Store$|\._)|\.tmp$|(^|/)\.recycle(/|$)", re.I)
SMB_OPS = {"mkdirat": "mkdir", "renameat": "rename", "unlinkat": "delete", "openat": "smb-write"}


def parse_smb_audit(msg: str) -> dict | None:
    f = msg.split("|")
    if len(f) < 6 or f[4] != "ok" or f[3] not in SMB_OPS:
        return None
    user, ip, share, op = f[0], f[1], f[2], f[3]
    args = f[5:]
    if op == "openat":
        if len(args) < 2 or args[0] != "w":
            return None                                   # only writes; reads are far too noisy
        args = args[1:]
    root = ROOT.as_posix().rstrip("/")

    def vpath(a: str) -> str:
        if a.startswith(root + "/"):                       # newer Samba logs absolute paths
            return a[len(root):]
        if a.startswith("/"):
            return "/" + share + "/" + a.rsplit("/", 1)[-1]
        return "/" + share + ("/" + (a[2:] if a.startswith("./") else a) if a not in (".", "") else "")

    paths = [vpath(a) for a in args]
    if any(SMB_NOISE.search(p) for p in paths) and op != "renameat":
        return None
    if op == "renameat":
        if len(paths) < 2 or all(SMB_NOISE.search(p) for p in paths):
            return None
        detail = f"{paths[0]} -> {paths[1].rsplit('/', 1)[-1]}"
        if re.search(r"/\.recycle(/|$)", paths[1]):         # Samba recycle bin: this is a delete
            op, detail = "unlinkat", paths[0]
        elif SMB_NOISE.search(paths[0]):                    # Office "save": temp file renamed over the real one
            op, detail = "openat", paths[1]
    else:
        detail = paths[0]
    return {"username": user, "ip": ip, "action": SMB_OPS[op], "detail": detail}


def import_smb_audit_forever() -> None:
    if not shutil.which("journalctl"):
        return
    try:   # rows imported before absolute paths were understood: "/Share/srv/share/Share/x" -> "/Share/x"
        bad = re.compile(r"/[^/ ]+" + re.escape(ROOT.as_posix().rstrip("/")) + "/")
        with _db_lock, db() as con:
            for rid, detail in con.execute("SELECT rowid, detail FROM audit WHERE source='smb'").fetchall():
                fixed = bad.sub("/", detail)
                if fixed != detail:
                    con.execute("UPDATE audit SET detail=? WHERE rowid=?", (fixed, rid))
    except sqlite3.Error:
        pass
    while True:
        try:
            cursor = get_setting("smb_cursor", "")
            cmd = ["journalctl", "-t", "smbd_audit", "-o", "json", "--no-pager", "-q"]
            cmd += [f"--after-cursor={cursor}"] if cursor else ["--since", "-1d"]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
            last = None
            for line in out.splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                last = e.get("__CURSOR") or last
                msg = e.get("MESSAGE")
                if isinstance(msg, list):                     # non-UTF-8 messages come as byte lists
                    msg = bytes(msg).decode("utf-8", "replace")
                ev = parse_smb_audit(str(msg or ""))
                if ev:
                    audit({"username": ev["username"]}, ev["action"], ev["detail"], ip=ev["ip"], source="smb",
                          ts=int(int(e.get("__REALTIME_TIMESTAMP", "0")) / 1_000_000) or None)
            if last:
                set_setting("smb_cursor", last)
        except Exception as e:  # never stop the importer
            sys.stderr.write(f"smb audit import: {e!r}\n")
        time.sleep(5)


def load_user(con: sqlite3.Connection, user_id: int) -> dict | None:
    row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return None
    u = dict(row)
    u["depts"] = [r["dept"] for r in con.execute("SELECT dept FROM user_depts WHERE user_id=? ORDER BY dept", (user_id,))]
    return u


def public_user(u: dict) -> dict:
    d = {k: u[k] for k in ("id", "username", "display_name", "role", "active", "depts")}
    d["avatar"] = avatar_ver(u["id"])
    return d


def avatar_ver(uid: int) -> int:
    """Picture version for cache busting (file mtime), 0 when there is none."""
    f = avatar_file(uid)
    return int(f.stat().st_mtime) if f.is_file() else 0


def avatar_file(uid: int) -> Path:
    return DATA / "avatars" / f"{int(uid)}.img"


def image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None

# ---------------------------------------------------------------- login throttling

_fail: dict[str, list[float]] = {}
_fail_lock = threading.Lock()


def throttled(key: str) -> bool:
    now = time.time()
    with _fail_lock:
        hits = [t for t in _fail.get(key, []) if now - t < 300]
        _fail[key] = hits
        return len(hits) >= 5


def record_fail(key: str) -> None:
    with _fail_lock:
        _fail.setdefault(key, []).append(time.time())

# ---------------------------------------------------------------- paths and permissions

class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def split_vpath(vpath: str) -> list[str]:
    parts = [p for p in (vpath or "/").replace("\\", "/").split("/") if p not in ("", ".")]
    for p in parts:
        if p == ".." or p.startswith(".") or not NAME_RE.match(p):
            raise ApiError(400, "ชื่อหรือที่อยู่ไฟล์ไม่ถูกต้อง")
    return parts


def real_path(parts: list[str]) -> Path:
    p = ROOT.joinpath(*parts)
    rp = p.resolve()
    if rp != ROOT and ROOT not in rp.parents:
        raise ApiError(403, "ไม่อนุญาตให้เข้าถึงตำแหน่งนี้")
    return p


def is_manager(u: dict) -> bool:
    return u["role"] in ("superadmin", "editor")


def can_view(u: dict, parts: list[str]) -> bool:
    return not parts or is_manager(u) or parts[0] in u["depts"]


def can_write(u: dict, parts: list[str]) -> bool:
    """parts = the folder being written into (or the parent of the item being changed)."""
    if not parts:
        return is_manager(u)            # top level: only superadmin / editor
    return is_manager(u) or parts[0] in u["depts"]


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not name or name in (".", "..") or name.startswith(".") or not NAME_RE.match(name):
        raise ApiError(400, "ชื่อไม่ถูกต้อง: ห้ามว่าง ห้ามขึ้นต้นด้วยจุด และห้ามมีอักขระ / \\ : * ? \" < > |")
    return name


def list_dir(p: Path) -> list[dict]:
    out = []
    try:
        with os.scandir(p) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                is_dir = e.is_dir()
                out.append({"name": e.name, "type": "dir" if is_dir else "file",
                            "size": 0 if is_dir else st.st_size, "mtime": int(st.st_mtime)})
    except FileNotFoundError:
        raise ApiError(404, "ไม่พบโฟลเดอร์นี้")
    out.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    return out


def departments() -> list[str]:
    return [e["name"] for e in list_dir(ROOT) if e["type"] == "dir"]

# ---------------------------------------------------------------- live updates (SSE)

class Hub:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.subs: dict[queue.Queue, str] = {}      # queue -> watched vpath ("/A/B")
        self.sigs: dict[str, str] = {}

    def subscribe(self, vpath: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self.lock:
            self.subs[q] = vpath
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.subs.pop(q, None)

    def notify(self, vpath: str) -> None:
        with self.lock:
            targets = [q for q, w in self.subs.items() if w == vpath]
        for q in targets:
            try:
                q.put_nowait(vpath)
            except queue.Full:
                pass

    @staticmethod
    def signature(vpath: str) -> str:
        try:
            ents = list_dir(real_path(split_vpath(vpath)))
        except ApiError:
            return "missing"
        return hashlib.sha1(json.dumps(ents, ensure_ascii=False).encode()).hexdigest()

    def poll_forever(self) -> None:
        """Catch changes made outside the app (e.g. through the Windows share)."""
        while True:
            time.sleep(2)
            with self.lock:
                watched = set(self.subs.values())
            for vp in watched:
                sig = self.signature(vp)
                if self.sigs.get(vp) not in (None, sig):
                    self.notify(vp)
                self.sigs[vp] = sig


HUB = Hub()

# ---------------------------------------------------------------- chat
# Conversations: "all" (everyone), "dept:<folder>" (people who can open that folder),
# "dm:<low id>-<high id>" (two people). Messages live in SQLite on this server only.

CHAT_MAX = 2000


class ChatHub:
    """Pushes new chat messages to the signed-in users allowed to see them."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.subs: dict[queue.Queue, int] = {}       # queue -> user id
        self.conns: dict[int, int] = {}              # user id -> open live connections (tabs/devices)
        self.seen: dict[int, int] = {}               # user id -> when they were last online

    def subscribe(self, uid: int) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self.lock:
            self.subs[q] = uid
            self.conns[uid] = self.conns.get(uid, 0) + 1
            came_online = self.conns[uid] == 1
        if came_online:
            self.presence(uid, True)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            uid = self.subs.pop(q, None)
            went_offline = False
            if uid is not None:
                self.conns[uid] = self.conns.get(uid, 1) - 1
                if self.conns[uid] <= 0:
                    self.conns.pop(uid, None)
                    self.seen[uid] = int(time.time())
                    went_offline = True
        if went_offline:
            self.presence(uid, False)

    def online(self) -> set[int]:
        with self.lock:
            return set(self.conns)

    def presence(self, uid: int, on: bool) -> None:
        """Tell everyone signed in that a colleague came online / went offline."""
        data = json.dumps({"id": uid, "online": on, "seen": self.seen.get(uid, 0)})
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(("presence", data))
            except queue.Full:
                pass

    def publish(self, conv: str, payload: dict, event: str = "msg") -> None:
        with self.lock:
            subs = list(self.subs.items())
        allowed: dict[int, bool] = {}
        with db() as con:
            for _, uid in subs:
                if uid not in allowed:
                    u = load_user(con, uid)
                    allowed[uid] = bool(u and u["active"] and chat_access(u, conv))
        data = json.dumps(payload, ensure_ascii=False)
        for q, uid in subs:
            if allowed.get(uid):
                try:
                    q.put_nowait((event, data))
                except queue.Full:
                    pass


CHAT = ChatHub()


def dm_peer(u: dict, conv: str) -> int | None:
    m = re.fullmatch(r"dm:(\d+)-(\d+)", conv)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    if a >= b or u["id"] not in (a, b):
        return None
    return b if u["id"] == a else a


def chat_access(u: dict, conv: str) -> bool:
    if conv == "all":
        return True
    if conv.startswith("dept:"):
        d = conv[5:]
        return d in departments() and can_view(u, [d])
    return dm_peer(u, conv) is not None


def dm_conv(a: int, b: int) -> str:
    return f"dm:{min(a, b)}-{max(a, b)}"


def chat_row(r) -> dict:
    live = r["display_name"] is not None
    return {"id": r["id"], "conv": r["conv"], "sender": r["sender"], "body": r["body"], "ts": r["ts"],
            "name": r["display_name"] if live else r["sender_name"], "gone": not live,
            "avatar": avatar_ver(r["sender"]) if live else 0, "image": bool(r["image"])}


CHAT_SELECT = ("SELECT m.id, m.conv, m.sender, m.sender_name, m.body, m.ts, m.image, u.display_name FROM chat_messages m "
               "LEFT JOIN users u ON u.id = m.sender ")
CHAT_IMAGES = DATA / "chat-images"
CHAT_IMAGE_MAX = 10 * 1024 * 1024


REACTIONS = ("👍", "❤️", "😆", "😮", "😢", "😡")


def reactions_for(con: sqlite3.Connection, ids: list[int]) -> dict[int, dict[str, list[int]]]:
    """{message id: {emoji: [user ids in the order they reacted]}}"""
    out: dict[int, dict[str, list[int]]] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        if not chunk:
            continue
        for r in con.execute(f"SELECT msg_id, user_id, emoji FROM chat_reactions WHERE msg_id IN ({','.join('?' * len(chunk))}) "
                             "ORDER BY ts", chunk):
            out.setdefault(r["msg_id"], {}).setdefault(r["emoji"], []).append(r["user_id"])
    return out


def forget_user_chat(con: sqlite3.Connection, uid: int) -> None:
    """On account deletion: drop their private chats, keep room messages under their old name."""
    for (img,) in con.execute("SELECT image FROM chat_messages WHERE image<>'' AND (conv LIKE ? OR conv LIKE ?)",
                              (f"dm:{uid}-%", f"dm:%-{uid}")).fetchall():
        (CHAT_IMAGES / Path(img).name).unlink(missing_ok=True)
    con.execute("DELETE FROM chat_messages WHERE conv LIKE ? OR conv LIKE ?", (f"dm:{uid}-%", f"dm:%-{uid}"))
    con.execute("DELETE FROM chat_read WHERE user_id=? OR conv LIKE ? OR conv LIKE ?", (uid, f"dm:{uid}-%", f"dm:%-{uid}"))
    con.execute("UPDATE chat_messages SET sender=0 WHERE sender=?", (uid,))    # ids may be reused later
    con.execute("DELETE FROM chat_reactions WHERE user_id=? OR msg_id NOT IN (SELECT id FROM chat_messages)", (uid,))


def parent_vpath(parts: list[str]) -> str:
    return "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"


def changed(parts_of_folder: list[str]) -> None:
    vp = "/" + "/".join(parts_of_folder) if parts_of_folder else "/"
    HUB.sigs[vp] = HUB.signature(vp)
    HUB.notify(vp)

# ---------------------------------------------------------------- HTTP handler

class Handler(BaseHTTPRequestHandler):
    server_version = "LANShare/1.0"
    protocol_version = "HTTP/1.1"

    # ---- helpers
    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' blob: data:; frame-src 'self'; object-src 'none'; base-uri 'none'")

    def send_json(self, status: int, obj, extra: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise ApiError(413, "คำขอใหญ่เกินไป")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            raise ApiError(400, "ข้อมูลไม่ถูกต้อง")

    def query(self) -> dict:
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    def cookie_token(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "ls_session":
                return v
        return None

    def current_user(self) -> dict | None:
        tok = self.cookie_token()
        if not tok:
            return None
        with db() as con:
            row = con.execute("SELECT user_id, expires FROM sessions WHERE token=?", (tok,)).fetchone()
            if not row or row["expires"] < time.time():
                return None
            u = load_user(con, row["user_id"])
        return u if u and u["active"] else None

    def require_user(self) -> dict:
        u = self.current_user()
        if not u:
            raise ApiError(401, "กรุณาเข้าสู่ระบบ")
        return u

    def require_csrf(self) -> None:
        if self.headers.get("X-LANShare") != "1":
            raise ApiError(403, "คำขอไม่ถูกต้อง (CSRF)")

    # ---- dispatch
    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def dispatch(self, method: str) -> None:
        path = urllib.parse.urlsplit(self.path).path
        _req.ip = self.client_address[0]
        try:
            if path.startswith("/api/"):
                if method != "GET":
                    self.require_csrf()
                route = getattr(self, f"api_{method.lower()}_{path[5:].replace('/', '_').replace('-', '_')}", None)
                m = re.fullmatch(r"/api/users/(\d+)/(update|delete)", path)
                if m and method == "POST":
                    return self.api_user_action(int(m.group(1)), m.group(2))
                if not route:
                    raise ApiError(404, "ไม่พบ API นี้")
                return route()
            if method == "GET":
                return self.static(path)
            raise ApiError(405, "ไม่รองรับคำขอนี้")
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # never leak internals
            sys.stderr.write(f"ERROR {e!r}\n")
            self.send_json(500, {"error": "เกิดข้อผิดพลาดที่เซิร์ฟเวอร์"})

    def static(self, path: str) -> None:
        name = {"/": "index.html"}.get(path, path.lstrip("/"))
        if "/" in name or name.startswith(".") or not (STATIC / name).is_file():
            name = "index.html"
        data = (STATIC / name).read_bytes()
        if name == "index.html":
            o = org()
            data = (data.replace(b"{{APP_NAME}}", html.escape(o["name"]).encode())
                        .replace(b"{{ORG_TAGLINE}}", html.escape(o["tagline"]).encode()))
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.security_headers()
        self.end_headers()
        self.wfile.write(data)

    # ---- auth
    def api_post_login(self):
        body = self.read_json()
        username = str(body.get("username", "")).strip().lower()
        key = f"{self.client_address[0]}|{username}"
        if throttled(key):
            raise ApiError(429, "ใส่รหัสผิดหลายครั้ง กรุณารอ 5 นาทีแล้วลองใหม่")
        typed = str(body.get("username", "")).strip()
        with db() as con:
            row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if not row:   # people often type the name they see in the list (display name) — accept it when unique
                rows = con.execute("SELECT * FROM users WHERE lower(display_name)=lower(?)", (typed,)).fetchall()
                row = rows[0] if len(rows) == 1 else None
        reason = ("no such user" if not row else "account disabled" if not row["active"]
                  else "" if verify_password(str(body.get("password", "")), row["pw_hash"]) else "wrong password")
        if reason:
            record_fail(key)
            sys.stderr.write(f"login failed for {typed[:40]!r} from {self.client_address[0]}: {reason}\n")
            audit({"username": row["username"] if row else typed[:40] or "?"}, "login-failed",
                  {"no such user": "ไม่มีชื่อผู้ใช้นี้", "account disabled": "บัญชีถูกปิดใช้งาน"}.get(reason, "รหัสผ่านไม่ถูกต้อง"))
            raise ApiError(401, "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
        tok = secrets.token_urlsafe(32)
        with _db_lock, db() as con:
            con.execute("DELETE FROM sessions WHERE expires < ?", (int(time.time()),))
            con.execute("INSERT INTO sessions VALUES(?,?,?)", (tok, row["id"], int(time.time()) + SESSION_HOURS * 3600))
        cookie = f"ls_session={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_HOURS * 3600}"
        with db() as con:
            u = load_user(con, row["id"])
        audit(u, "login", (self.headers.get("User-Agent") or "")[:120])
        self.send_json(200, {"user": public_user(u)}, {"Set-Cookie": cookie})

    def api_post_logout(self):
        tok = self.cookie_token()
        u = self.current_user()
        if u:
            audit(u, "logout", "")
        if tok:
            with _db_lock, db() as con:
                con.execute("DELETE FROM sessions WHERE token=?", (tok,))
        self.send_json(200, {"ok": True}, {"Set-Cookie": "ls_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})

    def api_get_me(self):
        o = org()
        self.send_json(200, {"user": public_user(self.require_user()), "app": o["name"], "tagline": o["tagline"]})

    def api_post_settings_org(self):
        u = self.require_superadmin()
        b = self.read_json()
        name = clean_text(b.get("name"), "ชื่อองค์กร", 1, 60)
        tagline = clean_text(b.get("tagline", DEFAULT_TAGLINE), "คำอธิบาย", 0, 80)
        set_setting("org_name", name)
        set_setting("org_tagline", tagline)
        audit(u, "org-settings", name)
        self.send_json(200, org())

    def api_post_me_password(self):
        u = self.require_user()
        body = self.read_json()
        with db() as con:
            row = con.execute("SELECT pw_hash FROM users WHERE id=?", (u["id"],)).fetchone()
        if not verify_password(str(body.get("current", "")), row["pw_hash"]):
            raise ApiError(400, "รหัสผ่านปัจจุบันไม่ถูกต้อง")
        new = str(body.get("new", ""))
        if len(new) < MIN_PASSWORD:
            raise ApiError(400, f"รหัสผ่านใหม่ต้องยาวอย่างน้อย {MIN_PASSWORD} ตัว")
        with _db_lock, db() as con:
            con.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(new), u["id"]))
        audit(u, "change-password", u["username"])
        self.send_json(200, {"ok": True})

    def api_post_me_delete(self):
        """A user deletes their own account after re-entering the password."""
        u = self.require_user()
        key = f"{self.client_address[0]}|{u['username']}"
        if throttled(key):
            raise ApiError(429, "ใส่รหัสผิดหลายครั้ง กรุณารอ 5 นาทีแล้วลองใหม่")
        with db() as con:
            row = con.execute("SELECT pw_hash FROM users WHERE id=?", (u["id"],)).fetchone()
            supers = con.execute("SELECT COUNT(*) FROM users WHERE role='superadmin' AND active=1").fetchone()[0]
        if not verify_password(str(self.read_json().get("password", "")), row["pw_hash"]):
            record_fail(key)
            raise ApiError(400, "รหัสผ่านไม่ถูกต้อง")
        if u["role"] == "superadmin" and supers <= 1:
            raise ApiError(400, "คุณเป็นผู้ดูแลสูงสุดคนสุดท้าย ต้องตั้งผู้ดูแลสูงสุดคนอื่นก่อนจึงจะลบบัญชีนี้ได้")
        with _db_lock, db() as con:
            con.execute("DELETE FROM users WHERE id=?", (u["id"],))
            forget_user_chat(con, u["id"])
        avatar_file(u["id"]).unlink(missing_ok=True)
        audit(u, "self-delete", u["username"])
        self.send_json(200, {"ok": True}, {"Set-Cookie": "ls_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})

    # ---- profile picture (the browser crops/resizes to 256 px before upload)
    def api_put_me_avatar(self):
        u = self.require_user()
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 < n <= 1_000_000:
            raise ApiError(413, "รูปใหญ่เกินไป (ไม่เกิน 1 MB)")
        data = self.rfile.read(n)
        if not image_type(data):
            raise ApiError(400, "รองรับเฉพาะรูป PNG, JPEG หรือ WebP")
        f = avatar_file(u["id"])
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, f)
        audit(u, "avatar", u["username"])
        self.send_json(200, {"user": public_user(u)})

    def api_post_me_avatar_delete(self):
        u = self.require_user()
        avatar_file(u["id"]).unlink(missing_ok=True)
        self.send_json(200, {"user": public_user(u)})

    def api_get_avatar(self):
        self.require_user()
        try:
            f = avatar_file(int(self.query().get("id", "")))
            data = f.read_bytes()
        except (ValueError, OSError):
            raise ApiError(404, "ไม่มีรูปโปรไฟล์")
        self.send_response(200)
        self.send_header("Content-Type", image_type(data) or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=604800")
        self.security_headers()
        self.end_headers()
        self.wfile.write(data)

    # ---- files
    def api_get_list(self):
        u = self.require_user()
        parts = split_vpath(self.query().get("path", "/"))
        if not can_view(u, parts):
            raise ApiError(403, "คุณไม่มีสิทธิ์เปิดโฟลเดอร์นี้")
        p = real_path(parts)
        if not p.is_dir():
            raise ApiError(404, "ไม่พบโฟลเดอร์นี้")
        ents = list_dir(p)
        if not parts and not is_manager(u):
            ents = [e for e in ents if e["type"] == "dir" and e["name"] in u["depts"]]
        self.send_json(200, {"path": "/" + "/".join(parts), "entries": ents, "canWrite": can_write(u, parts)})

    def api_get_download(self):
        u = self.require_user()
        parts = split_vpath(self.query().get("path", ""))
        if not parts or not can_view(u, parts):
            raise ApiError(403, "คุณไม่มีสิทธิ์เปิดไฟล์นี้")
        p = real_path(parts)
        if not p.is_file():
            raise ApiError(404, "ไม่พบไฟล์นี้")
        inline = self.query().get("inline") == "1"
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        safe_inline = ctype.startswith("image/") and ctype != "image/svg+xml" or ctype in ("application/pdf", "text/plain")
        if not (inline and safe_inline):
            inline = False
            if ctype.startswith("text/") or ctype in ("image/svg+xml", "application/xhtml+xml"):
                ctype = "application/octet-stream"
        size = p.stat().st_size
        fname = urllib.parse.quote(p.name)
        audit(u, "preview" if inline else "download", "/" + "/".join(parts))
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype == "text/plain" else ""))
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{fname}")
        self.send_header("Cache-Control", "private, no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'")
        self.end_headers()
        with open(p, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1024 * 256)

    def api_put_upload(self):
        u = self.require_user()
        q = self.query()
        parts = split_vpath(q.get("path", ""))
        if len(parts) < 1:
            raise ApiError(400, "ต้องระบุชื่อไฟล์")
        folder, name = parts[:-1], check_name(parts[-1])
        if not can_write(u, folder):
            raise ApiError(403, "ที่หน้าแรกสร้างได้เฉพาะผู้ดูแลและผู้แก้ไข กรุณาเปิดโฟลเดอร์ฝ่ายก่อน" if not folder
                           else "คุณไม่มีสิทธิ์อัปโหลดในโฟลเดอร์นี้")
        dest_dir = real_path(folder)
        created_at = None                                   # first folder we had to create (folder uploads)
        if not dest_dir.is_dir() and q.get("mkdirs") == "1" and folder:
            for i in range(1, len(folder) + 1):
                d = real_path(folder[:i])
                if d.exists() and not d.is_dir():
                    raise ApiError(409, f"มีไฟล์ชื่อ “{folder[i - 1]}” อยู่แล้ว สร้างโฟลเดอร์ชื่อเดียวกันไม่ได้")
                if not d.exists():
                    if i == 1 and not can_write(u, []):
                        raise ApiError(403, "สร้างโฟลเดอร์ที่หน้าแรกได้เฉพาะผู้ดูแลและผู้แก้ไข")
                    check_name(folder[i - 1])
                    d.mkdir()
                    created_at = created_at if created_at is not None else i - 1
        if not dest_dir.is_dir():
            raise ApiError(404, "ไม่พบโฟลเดอร์ปลายทาง")
        dest = real_path(folder + [name])
        if dest.exists() and q.get("overwrite") != "1":
            raise ApiError(409, f"มีไฟล์ชื่อ “{name}” อยู่แล้ว")
        length = int(self.headers.get("Content-Length") or -1)
        if length < 0:
            raise ApiError(411, "ต้องระบุขนาดไฟล์")
        if length > MAX_UPLOAD:
            raise ApiError(413, "ไฟล์ใหญ่เกินกำหนด")
        tmp = dest_dir / f".upload-{secrets.token_hex(6)}"
        try:
            with open(tmp, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 1024 * 256))
                    if not chunk:
                        raise ApiError(400, "การอัปโหลดขาดหาย กรุณาลองใหม่")
                    f.write(chunk)
                    remaining -= len(chunk)
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                tmp.unlink()
        audit(u, "upload", "/" + "/".join(folder + [name]))
        changed(folder)
        if created_at is not None:
            changed(folder[:created_at])                    # show the new sub-folder to people looking at its parent
        self.send_json(200, {"ok": True})

    def api_post_mkdir(self):
        u = self.require_user()
        body = self.read_json()
        folder = split_vpath(body.get("path", "/"))
        name = check_name(body.get("name", ""))
        if not can_write(u, folder):
            raise ApiError(403, "ที่หน้าแรกสร้างได้เฉพาะผู้ดูแลและผู้แก้ไข กรุณาเปิดโฟลเดอร์ฝ่ายก่อน" if not folder
                           else "คุณไม่มีสิทธิ์สร้างโฟลเดอร์ที่นี่")
        dest = real_path(folder + [name])
        if dest.exists():
            raise ApiError(409, f"มี “{name}” อยู่แล้ว")
        dest.mkdir()
        audit(u, "mkdir", "/" + "/".join(folder + [name]))
        changed(folder)
        self.send_json(200, {"ok": True})

    def api_post_rename(self):
        u = self.require_user()
        body = self.read_json()
        parts = split_vpath(body.get("path", ""))
        new = check_name(body.get("newName", ""))
        if not parts or not can_write(u, parts[:-1]):
            raise ApiError(403, "คุณไม่มีสิทธิ์เปลี่ยนชื่อรายการนี้")
        src, dest = real_path(parts), real_path(parts[:-1] + [new])
        if not src.exists():
            raise ApiError(404, "ไม่พบรายการนี้")
        if dest.exists():
            raise ApiError(409, f"มี “{new}” อยู่แล้ว")
        src.rename(dest)
        if len(parts) == 1:   # department folder renamed: keep user access in step
            with _db_lock, db() as con:
                con.execute("UPDATE user_depts SET dept=? WHERE dept=?", (new, parts[0]))
        audit(u, "rename", f"/{'/'.join(parts)} -> {new}")
        changed(parts[:-1])
        self.send_json(200, {"ok": True})

    def api_post_delete(self):
        u = self.require_user()
        body = self.read_json()
        paths = body.get("paths") or []
        if not isinstance(paths, list) or not paths:
            raise ApiError(400, "ไม่ได้เลือกรายการ")
        done, folders = 0, set()
        for vp in paths:
            parts = split_vpath(str(vp))
            if not parts or not can_write(u, parts[:-1]):
                raise ApiError(403, "คุณไม่มีสิทธิ์ลบรายการนี้")
            src = real_path(parts)
            if not src.exists():
                continue
            slot = ROOT / RECYCLE / f"{int(time.time())}-{secrets.token_hex(4)}"
            slot.mkdir(parents=True)
            (slot / "meta.json").write_text(json.dumps(
                {"path": "/" + "/".join(parts), "by": u["username"], "ts": int(time.time()),
                 "type": "dir" if src.is_dir() else "file"}, ensure_ascii=False), encoding="utf-8")
            shutil.move(str(src), str(slot / "item"))
            audit(u, "delete", "/" + "/".join(parts))
            done += 1
            folders.add(tuple(parts[:-1]))
        for f in folders:
            changed(list(f))
        self.send_json(200, {"ok": True, "deleted": done})

    # ---- recycle bin (superadmin / editor)
    def require_manager(self) -> dict:
        u = self.require_user()
        if not is_manager(u):
            raise ApiError(403, "เฉพาะผู้ดูแลและผู้แก้ไข")
        return u

    def api_get_recycle(self):
        self.require_manager()
        items = []
        base = ROOT / RECYCLE
        if base.is_dir():
            for slot in sorted(base.iterdir(), reverse=True):
                try:
                    meta = json.loads((slot / "meta.json").read_text(encoding="utf-8"))
                    items.append({"id": slot.name, **meta})
                except (OSError, ValueError):
                    continue
        self.send_json(200, {"items": items})

    def recycle_slot(self, rid: str) -> Path:
        if not re.fullmatch(r"\d+-[0-9a-f]{8}", rid or ""):
            raise ApiError(400, "รายการไม่ถูกต้อง")
        slot = ROOT / RECYCLE / rid
        if not slot.is_dir():
            raise ApiError(404, "ไม่พบรายการในถังขยะ")
        return slot

    def api_post_recycle_restore(self):
        u = self.require_manager()
        slot = self.recycle_slot(self.read_json().get("id"))
        meta = json.loads((slot / "meta.json").read_text(encoding="utf-8"))
        parts = split_vpath(meta["path"])
        dest = real_path(parts)
        if not dest.parent.is_dir():
            raise ApiError(409, "โฟลเดอร์เดิมไม่มีแล้ว กู้คืนไม่ได้")
        if dest.exists():
            stem, ext = os.path.splitext(parts[-1])
            parts[-1] = f"{stem} (กู้คืน){ext}"
            dest = real_path(parts)
        shutil.move(str(slot / "item"), str(dest))
        shutil.rmtree(slot)
        audit(u, "restore", "/" + "/".join(parts))
        changed(parts[:-1])
        self.send_json(200, {"ok": True, "path": "/" + "/".join(parts)})

    def api_post_recycle_purge(self):
        u = self.require_user()
        if u["role"] != "superadmin":
            raise ApiError(403, "เฉพาะผู้ดูแลสูงสุดลบถาวรได้")
        slot = self.recycle_slot(self.read_json().get("id"))
        shutil.rmtree(slot)
        audit(u, "purge", slot.name)
        self.send_json(200, {"ok": True})

    # ---- live updates
    def api_get_events(self):
        u = self.require_user()
        parts = split_vpath(self.query().get("path", "/"))
        if not can_view(u, parts):
            raise ApiError(403, "คุณไม่มีสิทธิ์เปิดโฟลเดอร์นี้")
        vp = "/" + "/".join(parts) if parts else "/"
        q = HUB.subscribe(vp)
        HUB.sigs.setdefault(vp, HUB.signature(vp))
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\nevent: ready\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                try:
                    changed_vp = q.get(timeout=20)
                    msg = json.dumps({"path": changed_vp}, ensure_ascii=False)
                    self.wfile.write(f"event: change\ndata: {msg}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            HUB.unsubscribe(q)

    # ---- departments and users
    def api_get_depts(self):
        u = self.require_user()
        self.send_json(200, {"depts": departments() if is_manager(u) else u["depts"]})

    def api_get_users(self):
        self.require_manager()
        with db() as con:
            ids = [r["id"] for r in con.execute(
                "SELECT id FROM users ORDER BY CASE role WHEN 'superadmin' THEN 0 WHEN 'editor' THEN 1 ELSE 2 END, username")]
            users = [public_user(load_user(con, i)) for i in ids]
        self.send_json(200, {"users": users})

    def validate_depts(self, depts) -> list[str]:
        if not isinstance(depts, list):
            raise ApiError(400, "รายการฝ่ายไม่ถูกต้อง")
        valid = set(departments())
        bad = [d for d in depts if d not in valid]
        if bad:
            raise ApiError(400, f"ไม่พบโฟลเดอร์ฝ่าย: {', '.join(bad)}")
        return sorted(set(depts))

    def api_post_users(self):
        actor = self.require_manager()
        b = self.read_json()
        username = str(b.get("username", "")).strip().lower()
        if not USER_RE.match(username):
            raise ApiError(400, "ชื่อผู้ใช้ใช้ได้เฉพาะ a-z 0-9 . _ - ยาว 2–32 ตัว และขึ้นต้นด้วยตัวอักษรหรือตัวเลข")
        role = b.get("role", "user")
        if role not in ROLES:
            raise ApiError(400, "บทบาทไม่ถูกต้อง")
        if actor["role"] != "superadmin" and role != "user":
            raise ApiError(403, "ผู้แก้ไขสร้างได้เฉพาะผู้ใช้ทั่วไป")
        pw = str(b.get("password", ""))
        if len(pw) < MIN_PASSWORD:
            raise ApiError(400, f"รหัสผ่านต้องยาวอย่างน้อย {MIN_PASSWORD} ตัว")
        depts = self.validate_depts(b.get("depts", []))
        display = str(b.get("display_name", "")).strip()[:80] or username
        try:
            with _db_lock, db() as con:
                cur = con.execute("INSERT INTO users(username,display_name,role,pw_hash,active,created_at) VALUES(?,?,?,?,1,?)",
                                  (username, display, role, hash_password(pw), int(time.time())))
                con.executemany("INSERT INTO user_depts VALUES(?,?)", [(cur.lastrowid, d) for d in depts])
        except sqlite3.IntegrityError:
            raise ApiError(409, f"มีชื่อผู้ใช้ “{username}” อยู่แล้ว")
        audit(actor, "user-create", f"{username} ({role})")
        self.send_json(200, {"ok": True})

    def api_user_action(self, uid: int, action: str):
        actor = self.require_manager()
        with db() as con:
            target = load_user(con, uid)
        if not target:
            raise ApiError(404, "ไม่พบผู้ใช้")
        if action == "delete":
            if actor["role"] != "superadmin":
                raise ApiError(403, "เฉพาะผู้ดูแลสูงสุดลบผู้ใช้ได้")
            if target["id"] == actor["id"]:
                raise ApiError(400, "ลบบัญชีของตัวเองไม่ได้")
            with _db_lock, db() as con:
                con.execute("DELETE FROM users WHERE id=?", (uid,))
                forget_user_chat(con, uid)
            avatar_file(uid).unlink(missing_ok=True)       # ids can be reused
            audit(actor, "user-delete", target["username"])
            return self.send_json(200, {"ok": True})

        b = self.read_json()
        if actor["role"] != "superadmin" and target["role"] != "user":
            raise ApiError(403, "ผู้แก้ไขแก้ไขได้เฉพาะบัญชีผู้ใช้ทั่วไป")
        sets, args = [], []
        if "display_name" in b:
            sets.append("display_name=?"); args.append(str(b["display_name"]).strip()[:80] or target["username"])
        if "password" in b and b["password"]:
            if len(str(b["password"])) < MIN_PASSWORD:
                raise ApiError(400, f"รหัสผ่านต้องยาวอย่างน้อย {MIN_PASSWORD} ตัว")
            sets.append("pw_hash=?"); args.append(hash_password(str(b["password"])))
        if "active" in b:
            if target["id"] == actor["id"] and not b["active"]:
                raise ApiError(400, "ปิดใช้งานบัญชีของตัวเองไม่ได้")
            sets.append("active=?"); args.append(1 if b["active"] else 0)
        if "role" in b and b["role"] != target["role"]:
            if actor["role"] != "superadmin":
                raise ApiError(403, "เฉพาะผู้ดูแลสูงสุดเปลี่ยนบทบาทได้")
            if b["role"] not in ROLES:
                raise ApiError(400, "บทบาทไม่ถูกต้อง")
            if target["role"] == "superadmin":
                with db() as con:
                    n = con.execute("SELECT COUNT(*) FROM users WHERE role='superadmin' AND active=1").fetchone()[0]
                if n <= 1:
                    raise ApiError(400, "ต้องมีผู้ดูแลสูงสุดอย่างน้อย 1 คน")
            sets.append("role=?"); args.append(b["role"])
        depts = self.validate_depts(b["depts"]) if "depts" in b else None
        with _db_lock, db() as con:
            if sets:
                con.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", (*args, uid))
            if depts is not None:
                con.execute("DELETE FROM user_depts WHERE user_id=?", (uid,))
                con.executemany("INSERT INTO user_depts VALUES(?,?)", [(uid, d) for d in depts])
            if "password" in b or b.get("active") is False:
                con.execute("DELETE FROM sessions WHERE user_id=? AND user_id<>?", (uid, actor["id"]))
        audit(actor, "user-update", target["username"])
        self.send_json(200, {"ok": True})

    # ---- chat
    def chat_conv(self, u: dict, conv) -> str:
        conv = str(conv or "")
        if not chat_access(u, conv):
            raise ApiError(403, "คุณไม่มีสิทธิ์ในห้องแชทนี้")
        return conv

    def api_get_chat_convs(self):
        u = self.require_user()
        depts = departments()
        rooms = ["all"] + [f"dept:{d}" for d in depts if can_view(u, [d])]
        out = []
        with db() as con:
            dms = [r["conv"] for r in con.execute(
                "SELECT conv, MAX(id) AS last FROM chat_messages WHERE conv LIKE ? OR conv LIKE ? GROUP BY conv ORDER BY last DESC",
                (f"dm:{u['id']}-%", f"dm:%-{u['id']}"))]
            for c in rooms + [c for c in dms if dm_peer(u, c) is not None]:
                last = con.execute(CHAT_SELECT + "WHERE m.conv=? ORDER BY m.id DESC LIMIT 1", (c,)).fetchone()
                seen = con.execute("SELECT last_id FROM chat_read WHERE user_id=? AND conv=?", (u["id"], c)).fetchone()
                unread = con.execute("SELECT COUNT(*) FROM chat_messages WHERE conv=? AND id>? AND sender<>?",
                                     (c, seen["last_id"] if seen else 0, u["id"])).fetchone()[0]
                item = {"id": c, "unread": unread, "last": chat_row(last) if last else None}
                if c == "all":
                    item.update(kind="all", title="ทุกคนในองค์กร")
                elif c.startswith("dept:"):
                    item.update(kind="dept", title=c[5:])
                else:
                    peer = load_user(con, dm_peer(u, c))
                    item.update(kind="dm", title=peer["display_name"] if peer else "ผู้ใช้", peer=public_user(peer) if peer else None)
                out.append(item)
            people = [public_user(load_user(con, r["id"])) for r in
                      con.execute("SELECT id FROM users WHERE active=1 AND id<>? ORDER BY display_name", (u["id"],))]
        online = CHAT.online()
        for p in people:
            p["online"], p["seen"] = p["id"] in online, CHAT.seen.get(p["id"], 0)
        self.send_json(200, {"convs": out, "people": people, "online": sorted(online)})

    def api_get_chat_messages(self):
        u = self.require_user()
        q = self.query()
        conv = self.chat_conv(u, q.get("c"))
        before = int(q["before"]) if str(q.get("before", "")).isdigit() else 0
        with db() as con:
            rows = con.execute(CHAT_SELECT + "WHERE m.conv=? AND (?=0 OR m.id<?) ORDER BY m.id DESC LIMIT 60",
                               (conv, before, before)).fetchall()
        msgs = [chat_row(r) for r in reversed(rows)]
        with db() as con:
            reacts = reactions_for(con, [m["id"] for m in msgs])
        for m in msgs:
            m["reactions"] = reacts.get(m["id"], {})
        self.send_json(200, {"messages": msgs, "more": len(rows) == 60})

    def chat_post(self, u: dict, conv: str, body: str, image: str = "") -> dict:
        """Store a message (text and/or picture), mark it read for the sender, push it live."""
        peer = dm_peer(u, conv)
        if peer is not None:
            with db() as con:
                p = load_user(con, peer)
            if not p or not p["active"]:
                raise ApiError(400, "ผู้ใช้นี้ไม่ได้ใช้งานแล้ว ส่งข้อความไม่ได้")
        body = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(body or "").replace("\r\n", "\n")).strip()
        if not body and not image:
            raise ApiError(400, "พิมพ์ข้อความก่อนส่ง")
        if len(body) > CHAT_MAX:
            raise ApiError(400, f"ข้อความยาวเกิน {CHAT_MAX} ตัวอักษร")
        with _db_lock, db() as con:
            mid = con.execute("INSERT INTO chat_messages(conv, sender, sender_name, body, ts, image) VALUES(?,?,?,?,?,?)",
                              (conv, u["id"], u["display_name"], body, int(time.time()), image)).lastrowid
            con.execute("INSERT INTO chat_read VALUES(?,?,?) ON CONFLICT(user_id, conv) DO UPDATE SET last_id=excluded.last_id",
                        (u["id"], conv, mid))
            msg = chat_row(con.execute(CHAT_SELECT + "WHERE m.id=?", (mid,)).fetchone())
        CHAT.publish(conv, msg)
        return msg

    def api_post_chat_react(self):
        """Messenger-style: one reaction per person per message; same emoji again removes it."""
        u = self.require_user()
        b = self.read_json()
        emoji = str(b.get("emoji", ""))
        if emoji and emoji not in REACTIONS:
            raise ApiError(400, "ไม่รองรับอีโมจินี้")
        mid = int(b["id"]) if str(b.get("id", "")).isdigit() else -1
        with db() as con:
            row = con.execute("SELECT conv, sender FROM chat_messages WHERE id=?", (mid,)).fetchone()
        if not row or not chat_access(u, row["conv"]):
            raise ApiError(404, "ไม่พบข้อความนี้")
        with _db_lock, db() as con:
            cur = con.execute("SELECT emoji FROM chat_reactions WHERE msg_id=? AND user_id=?", (mid, u["id"])).fetchone()
            if not emoji or (cur and cur["emoji"] == emoji):
                con.execute("DELETE FROM chat_reactions WHERE msg_id=? AND user_id=?", (mid, u["id"]))
                emoji = ""
            else:
                con.execute("INSERT INTO chat_reactions VALUES(?,?,?,?) ON CONFLICT(msg_id, user_id) "
                            "DO UPDATE SET emoji=excluded.emoji, ts=excluded.ts", (mid, u["id"], emoji, int(time.time())))
            reactions = reactions_for(con, [mid]).get(mid, {})
        event = {"id": mid, "conv": row["conv"], "reactions": reactions, "by": u["id"], "by_name": u["display_name"],
                 "emoji": emoji, "owner": row["sender"]}
        CHAT.publish(row["conv"], event, "react")
        self.send_json(200, event)

    def api_post_chat_send(self):
        u = self.require_user()
        b = self.read_json()
        self.send_json(200, {"message": self.chat_post(u, self.chat_conv(u, b.get("c")), b.get("body", ""))})

    def api_put_chat_image(self):
        """Body = the picture (the browser shrinks big photos first). ?c=conversation&caption=text"""
        u = self.require_user()
        q = self.query()
        conv = self.chat_conv(u, q.get("c"))
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 < n <= CHAT_IMAGE_MAX:
            raise ApiError(413, f"รูปใหญ่เกินไป (ไม่เกิน {CHAT_IMAGE_MAX // 1024 // 1024} MB)")
        data = self.rfile.read(n)
        kind = image_type(data)
        if not kind:
            raise ApiError(400, "รองรับเฉพาะรูป PNG, JPEG, WebP หรือ GIF")
        name = secrets.token_hex(12) + {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}[kind]
        CHAT_IMAGES.mkdir(parents=True, exist_ok=True)
        (CHAT_IMAGES / name).write_bytes(data)
        try:
            msg = self.chat_post(u, conv, q.get("caption", ""), image=name)
        except ApiError:
            (CHAT_IMAGES / name).unlink(missing_ok=True)
            raise
        self.send_json(200, {"message": msg})

    def api_get_chat_image(self):
        u = self.require_user()
        mid = self.query().get("id", "")
        with db() as con:
            row = con.execute("SELECT conv, image FROM chat_messages WHERE id=?", (int(mid) if mid.isdigit() else -1,)).fetchone()
        if not row or not row["image"] or not (chat_access(u, row["conv"]) or u["role"] == "superadmin"):
            raise ApiError(404, "ไม่พบรูปนี้")
        try:
            data = (CHAT_IMAGES / Path(row["image"]).name).read_bytes()
        except OSError:
            raise ApiError(404, "ไม่พบรูปนี้")
        self.send_response(200)
        self.send_header("Content-Type", image_type(data) or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        self.security_headers()
        self.end_headers()
        self.wfile.write(data)

    def api_post_chat_read(self):
        u = self.require_user()
        conv = self.chat_conv(u, self.read_json().get("c"))
        with _db_lock, db() as con:
            top = con.execute("SELECT COALESCE(MAX(id), 0) FROM chat_messages WHERE conv=?", (conv,)).fetchone()[0]
            con.execute("INSERT INTO chat_read VALUES(?,?,?) ON CONFLICT(user_id, conv) DO UPDATE SET last_id=MAX(last_id, excluded.last_id)",
                        (u["id"], conv, top))
        self.send_json(200, {"ok": True})

    def api_get_chat_events(self):
        u = self.require_user()
        q = CHAT.subscribe(u["id"])
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\nevent: ready\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                try:
                    event, data = q.get(timeout=20)
                    self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            CHAT.unsubscribe(q)

    # ---- activity log (super admin): who did what to which file, from the web and the Windows drive
    ACT_GROUPS = {
        "files": ("upload", "download", "preview", "mkdir", "rename", "delete", "restore", "purge", "smb-write"),
        "access": ("login", "login-failed", "logout"),
        "admin": ("user-create", "user-update", "user-delete", "self-delete", "change-password", "avatar",
                  "org-settings", "net-save", "net-apply", "net-dhcp"),
    }

    def activity_query(self, q: dict, limit: int):
        where, args = [], []
        if q.get("user"):
            where.append("username = ?"); args.append(q["user"])
        if q.get("group") in self.ACT_GROUPS:
            acts = self.ACT_GROUPS[q["group"]]
            where.append(f"action IN ({','.join('?' * len(acts))})"); args += acts
        if q.get("source") in ("web", "smb"):
            where.append("source = ?"); args.append(q["source"])
        if q.get("q"):
            where.append("(detail LIKE ? OR username LIKE ? OR ip LIKE ?)"); args += [f"%{q['q']}%"] * 3
        if str(q.get("before", "")).isdigit():
            where.append("rowid < ?"); args.append(int(q["before"]))
        sql = ("SELECT rowid AS id, ts, username, action, detail, ip, source FROM audit"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY rowid DESC LIMIT ?")
        with db() as con:
            return [dict(r) for r in con.execute(sql, (*args, limit))]

    def api_get_activity(self):
        self.require_superadmin()
        q = self.query()
        items = self.activity_query(q, 101)
        with db() as con:
            users = [r[0] for r in con.execute("SELECT DISTINCT username FROM audit ORDER BY username")]
        self.send_json(200, {"items": items[:100], "more": len(items) > 100, "users": users,
                             "smb": bool(shutil.which("journalctl"))})

    def api_get_activity_csv(self):
        self.require_superadmin()
        rows = self.activity_query(self.query(), 50000)
        lines = ["เวลา,ผู้ใช้,การกระทำ,รายการ,ช่องทาง,IP"]
        for r in rows:
            cells = [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])), r["username"], r["action"], r["detail"],
                     "ไดรฟ์ Windows" if r["source"] == "smb" else "หน้าเว็บ", r["ip"]]
            lines.append(",".join('"' + str(c).replace('"', '""') + '"' if re.search(r'[",\n]|^[=+\-@]', str(c)) else str(c) for c in cells))
        body = ("﻿" + "\r\n".join(lines)).encode("utf-8")       # BOM so Excel shows Thai correctly
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename=activity-{time.strftime('%Y%m%d')}.csv")
        self.send_header("Content-Length", str(len(body)))
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    # ---- chat log (super admin): who wrote to whom, when and what - private messages included.
    # Opening it is itself recorded in the activity log, and users see a notice in private chats.
    def api_get_chatlog(self):
        self.require_superadmin()
        q = self.query()
        where, args = [], []
        if q.get("kind") == "dm":
            where.append("m.conv LIKE 'dm:%'")
        elif q.get("kind") == "room":
            where.append("m.conv NOT LIKE 'dm:%'")
        if str(q.get("user", "")).isdigit():
            uid = int(q["user"])
            where.append("(m.sender = ? OR m.conv LIKE ? OR m.conv LIKE ?)"); args += [uid, f"dm:{uid}-%", f"dm:%-{uid}"]
        if re.fullmatch(r"dm:\d+-\d+|all|dept:.+", str(q.get("conv", ""))):
            where.append("m.conv = ?"); args.append(q["conv"])
        if str(q.get("before", "")).isdigit():
            where.append("m.id < ?"); args.append(int(q["before"]))
        sql = (CHAT_SELECT + ("WHERE " + " AND ".join(where) + " " if where else "") + "ORDER BY m.id DESC LIMIT 101")
        with db() as con:
            rows = con.execute(sql, args).fetchall()
            names = {r["id"]: r["display_name"] for r in con.execute("SELECT id, display_name FROM users")}
            users = [{"id": r["id"], "name": r["display_name"]} for r in con.execute("SELECT id, display_name FROM users ORDER BY display_name")]
        items = []
        for r in rows[:100]:
            conv, private = r["conv"], r["conv"].startswith("dm:")
            if private:
                a, b = (int(x) for x in conv[3:].split("-"))
                to = names.get(b if r["sender"] == a else a, "ผู้ใช้ที่ถูกลบ")
            else:
                to = "ทุกคนในองค์กร" if conv == "all" else conv[5:]
            items.append({"id": r["id"], "ts": r["ts"], "from": r["display_name"] or r["sender_name"], "to": to,
                          "kind": "dm" if private else ("all" if conv == "all" else "dept"), "image": bool(r["image"]),
                          "chars": len(r["body"]), "body": r["body"], "conv": conv})
        if any(i["kind"] == "dm" for i in items):
            audit(self.require_superadmin(), "view-chatlog", "เปิดดูข้อความส่วนตัว" + (f" ({q['conv']})" if q.get("conv") else ""))
        self.send_json(200, {"items": items, "more": len(rows) > 100, "users": users})

    # ---- network settings (super admin; proxied to the helper on the Proxmox host)
    def require_superadmin(self) -> dict:
        u = self.require_user()
        if u["role"] != "superadmin":
            raise ApiError(403, "เฉพาะผู้ดูแลสูงสุดเท่านั้น")
        return u

    def send_net(self, st: dict) -> None:
        self.send_json(200, {**st, "available": True, "port": PORT})

    def api_get_net(self):
        self.require_superadmin()
        if not NETD_URL:
            return self.send_json(200, {"available": False, "port": PORT})
        self.send_net(netd("GET", "/state"))

    def api_post_net_save(self):
        u = self.require_superadmin()
        b = self.read_json()
        st = netd("POST", "/state", {k: b[k] for k in ("name", "auto", "profiles") if k in b})
        audit(u, "net-save", ", ".join(k for k in ("name", "auto", "profiles") if k in b))
        self.send_net(st)

    def api_post_net_probe(self):
        self.require_superadmin()
        self.send_json(200, netd("POST", "/probe"))

    def api_post_net_dhcp(self):
        u = self.require_superadmin()
        r = netd("POST", "/dhcp", {})
        audit(u, "net-dhcp", "automatic")
        self.send_json(200, {**r, "port": PORT})

    def api_post_net_apply(self):
        u = self.require_superadmin()
        r = netd("POST", "/apply", {"id": str(self.read_json().get("id", ""))})
        audit(u, "net-apply", f"{r.get('label')} {r.get('ip')}")
        self.send_json(200, {**r, "port": PORT})


def netd(method: str, path: str, body: dict | None = None) -> dict:
    """Call the network helper on the host (see hostd/netd.py)."""
    if not NETD_URL:
        raise ApiError(404, "ยังไม่ได้ติดตั้งตัวช่วยเครือข่ายบนเครื่องหลัก")
    try:
        tok = NETD_TOKEN_FILE.read_text().strip()
    except OSError:
        raise ApiError(503, "อ่านกุญแจของตัวช่วยเครือข่ายไม่ได้")
    req = urllib.request.Request(NETD_URL + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    try:
        with _netd_opener.open(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error")
        except ValueError:
            msg = None
        raise ApiError(e.code if 400 <= e.code < 500 else 502, msg or "ตัวช่วยเครือข่ายตอบผิดพลาด")
    except (OSError, ValueError):
        raise ApiError(503, "ติดต่อตัวช่วยเครือข่ายบนเครื่องหลักไม่ได้")


_netd_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ---------------------------------------------------------------- CLI

def create_user_cli(username: str, role: str, depts: list[str]) -> None:
    init_db()
    pw = os.environ.get("LANSHARE_PASSWORD") or getpass.getpass(f"รหัสผ่านสำหรับ {username}: ")
    if len(pw) < MIN_PASSWORD:
        sys.exit(f"รหัสผ่านต้องยาวอย่างน้อย {MIN_PASSWORD} ตัว")
    with _db_lock, db() as con:
        con.execute("INSERT INTO users(username,display_name,role,pw_hash,active,created_at) VALUES(?,?,?,?,1,?) "
                    "ON CONFLICT(username) DO UPDATE SET role=excluded.role, pw_hash=excluded.pw_hash, active=1",
                    (username, username, role, hash_password(pw), int(time.time())))
        uid = con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
        con.execute("DELETE FROM user_depts WHERE user_id=?", (uid,))
        con.executemany("INSERT INTO user_depts VALUES(?,?)", [(uid, d) for d in depts])
    print(f"บันทึกผู้ใช้ {username} ({role}) แล้ว")


def serve() -> None:
    init_db()
    ROOT.mkdir(parents=True, exist_ok=True)
    os.umask(0o007)
    threading.Thread(target=HUB.poll_forever, daemon=True).start()
    threading.Thread(target=import_smb_audit_forever, daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    print(f"LANShare กำลังทำงานที่ http://{HOST}:{PORT}  (โฟลเดอร์: {ROOT})", flush=True)
    httpd.serve_forever()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="lanshare", description="LANShare file-sharing web app")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="start the web server")
    for name, role in (("create-admin", "superadmin"), ("create-editor", "editor"), ("create-user", "user")):
        p = sub.add_parser(name, help=f"create or reset a {role}")
        p.add_argument("username")
        p.add_argument("--dept", action="append", default=[], help="department folder (repeatable)")
        p.set_defaults(role=role)
    p = sub.add_parser("set-name", help="set the organisation name shown on every page")
    p.add_argument("name")
    p.add_argument("--tagline", help="short line under the name")
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        serve()
    elif a.cmd == "set-name":
        init_db()
        try:
            set_setting("org_name", clean_text(a.name, "ชื่อองค์กร", 1, 60))
            if a.tagline is not None:
                set_setting("org_tagline", clean_text(a.tagline, "คำอธิบาย", 0, 80))
        except ApiError as e:
            sys.exit(e.message)
        print(f"ตั้งชื่อองค์กรเป็น “{org()['name']}” แล้ว")
    else:
        if not USER_RE.match(a.username):
            sys.exit("ชื่อผู้ใช้ใช้ได้เฉพาะ a-z 0-9 . _ -")
        create_user_cli(a.username, a.role, a.dept)


if __name__ == "__main__":
    main()
