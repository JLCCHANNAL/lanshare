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
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import urllib.parse
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
            """
        )


def audit(user: dict, action: str, detail: str) -> None:
    with _db_lock, db() as con:
        con.execute("INSERT INTO audit VALUES(?,?,?,?)", (int(time.time()), user["username"], action, detail))


def load_user(con: sqlite3.Connection, user_id: int) -> dict | None:
    row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return None
    u = dict(row)
    u["depts"] = [r["dept"] for r in con.execute("SELECT dept FROM user_depts WHERE user_id=? ORDER BY dept", (user_id,))]
    return u


def public_user(u: dict) -> dict:
    return {k: u[k] for k in ("id", "username", "display_name", "role", "active", "depts")}

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
            data = data.replace(b"{{APP_NAME}}", APP_NAME.encode())
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
        with db() as con:
            row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row or not row["active"] or not verify_password(str(body.get("password", "")), row["pw_hash"]):
            record_fail(key)
            raise ApiError(401, "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
        tok = secrets.token_urlsafe(32)
        with _db_lock, db() as con:
            con.execute("DELETE FROM sessions WHERE expires < ?", (int(time.time()),))
            con.execute("INSERT INTO sessions VALUES(?,?,?)", (tok, row["id"], int(time.time()) + SESSION_HOURS * 3600))
        cookie = f"ls_session={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_HOURS * 3600}"
        with db() as con:
            u = load_user(con, row["id"])
        self.send_json(200, {"user": public_user(u)}, {"Set-Cookie": cookie})

    def api_post_logout(self):
        tok = self.cookie_token()
        if tok:
            with _db_lock, db() as con:
                con.execute("DELETE FROM sessions WHERE token=?", (tok,))
        self.send_json(200, {"ok": True}, {"Set-Cookie": "ls_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})

    def api_get_me(self):
        self.send_json(200, {"user": public_user(self.require_user()), "app": APP_NAME})

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
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        serve()
    else:
        if not USER_RE.match(a.username):
            sys.exit("ชื่อผู้ใช้ใช้ได้เฉพาะ a-z 0-9 . _ -")
        create_user_cli(a.username, a.role, a.dept)


if __name__ == "__main__":
    main()
