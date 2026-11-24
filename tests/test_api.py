"""End-to-end API tests: start a real server on a temp folder and exercise roles and safety rules.

Run:  python -m unittest discover -s tests -v
"""
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="lanshare-test-"))
os.environ.update(LANSHARE_ROOT=str(TMP / "share"), LANSHARE_DATA=str(TMP / "data"), LANSHARE_PORT="0")
(TMP / "share" / "Accounting").mkdir(parents=True)
(TMP / "share" / "HR").mkdir()
(TMP / "share" / "Common").mkdir()

from lanshare import server  # noqa: E402  (env must be set first)

PW = "Passw0rd!x"


class Client:
    def __init__(self, port):
        self.port, self.cookie = port, None

    def req(self, method, path, body=None, raw=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"X-LANShare": "1", **(headers or {})}
        if self.cookie:
            h["Cookie"] = self.cookie
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if body is not None:
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        payload = r.read()
        sc = r.getheader("Set-Cookie")
        if sc and sc.startswith("ls_session="):
            self.cookie = sc.split(";")[0]
        try:
            return r.status, json.loads(payload or b"{}")
        except ValueError:
            return r.status, payload

    def login(self, u, p=PW):
        return self.req("POST", "/api/login", {"username": u, "password": p})


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.init_db()
        os.environ["LANSHARE_PASSWORD"] = PW
        server.create_user_cli("boss", "superadmin", [])
        server.create_user_cli("ed", "editor", [])
        server.create_user_cli("acc", "user", ["Accounting", "Common"])
        server.create_user_cli("hr", "user", ["HR", "Common"])
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        threading.Thread(target=server.HUB.poll_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def as_(self, user):
        c = Client(self.port)
        self.assertEqual(c.login(user)[0], 200)
        return c

    # ---- auth
    def test_login_wrong_password(self):
        self.assertEqual(Client(self.port).login("acc", "nope-nope")[0], 401)

    def test_requires_login(self):
        self.assertEqual(Client(self.port).req("GET", "/api/list?path=/")[0], 401)

    def test_csrf_header_required(self):
        c = self.as_("acc")
        s, _ = c.req("POST", "/api/mkdir", {"path": "/Accounting", "name": "x"}, headers={"X-LANShare": "0"})
        self.assertEqual(s, 403)

    # ---- visibility
    def test_user_sees_only_own_departments(self):
        s, r = self.as_("acc").req("GET", "/api/list?path=/")
        self.assertEqual(s, 200)
        self.assertEqual({e["name"] for e in r["entries"]}, {"Accounting", "Common"})
        self.assertFalse(r["canWrite"])

    def test_user_cannot_open_other_department(self):
        self.assertEqual(self.as_("acc").req("GET", "/api/list?path=/HR")[0], 403)

    def test_editor_sees_everything(self):
        s, r = self.as_("ed").req("GET", "/api/list?path=/")
        self.assertTrue({"Accounting", "HR", "Common"} <= {e["name"] for e in r["entries"]})
        self.assertTrue(r["canWrite"])

    # ---- top level rules
    def test_user_cannot_create_at_top_level(self):
        self.assertEqual(self.as_("acc").req("POST", "/api/mkdir", {"path": "/", "name": "Mine"})[0], 403)

    def test_editor_and_admin_create_at_top_level(self):
        self.assertEqual(self.as_("ed").req("POST", "/api/mkdir", {"path": "/", "name": "Marketing"})[0], 200)
        self.assertEqual(self.as_("boss").req("POST", "/api/mkdir", {"path": "/", "name": "Legal"})[0], 200)
        self.assertTrue((TMP / "share" / "Marketing").is_dir())

    # ---- files
    def test_upload_download_rename_delete_restore(self):
        c = self.as_("acc")
        s, _ = c.req("PUT", "/api/upload?path=" + "/Accounting/%E0%B9%83%E0%B8%9A%E0%B9%81%E0%B8%88%E0%B9%89%E0%B8%87.txt", raw=b"hello")
        self.assertEqual(s, 200)
        self.assertEqual(c.req("PUT", "/api/upload?path=/Accounting/%E0%B9%83%E0%B8%9A%E0%B9%81%E0%B8%88%E0%B9%89%E0%B8%87.txt", raw=b"x")[0], 409)
        s, body = c.req("GET", "/api/download?path=/Accounting/%E0%B9%83%E0%B8%9A%E0%B9%81%E0%B8%88%E0%B9%89%E0%B8%87.txt")
        self.assertEqual((s, body), (200, b"hello"))
        self.assertEqual(c.req("POST", "/api/rename", {"path": "/Accounting/ใบแจ้ง.txt", "newName": "inv.txt"})[0], 200)
        self.assertEqual(c.req("POST", "/api/delete", {"paths": ["/Accounting/inv.txt"]})[0], 200)
        self.assertFalse((TMP / "share" / "Accounting" / "inv.txt").exists())
        e = self.as_("ed")
        s, r = e.req("GET", "/api/recycle")
        rid = next(i["id"] for i in r["items"] if i["path"] == "/Accounting/inv.txt")
        self.assertEqual(e.req("POST", "/api/recycle/restore", {"id": rid})[0], 200)
        self.assertTrue((TMP / "share" / "Accounting" / "inv.txt").exists())
        self.assertEqual(c.req("GET", "/api/recycle")[0], 403)

    def test_user_cannot_upload_to_other_department(self):
        self.assertEqual(self.as_("acc").req("PUT", "/api/upload?path=/HR/x.txt", raw=b"x")[0], 403)

    def test_path_traversal_blocked(self):
        c = self.as_("boss")
        for bad in ("/../data/lanshare.db", "/Accounting/../../data", "/.lanshare-recycle", "/Accounting/..%2F..%2Fx"):
            self.assertIn(c.req("GET", "/api/list?path=" + bad)[0], (400, 403, 404), bad)
        self.assertIn(c.req("GET", "/api/download?path=/../data/lanshare.db")[0], (400, 403))

    def test_bad_names_rejected(self):
        c = self.as_("acc")
        for n in ("..", ".hidden", "a/b", "a\\b", "x:y", ""):
            self.assertEqual(c.req("POST", "/api/mkdir", {"path": "/Accounting", "name": n})[0], 400, n)

    def test_html_never_served_inline(self):
        c = self.as_("acc")
        c.req("PUT", "/api/upload?path=/Accounting/evil.html", raw=b"<script>alert(1)</script>")
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/download?inline=1&path=/Accounting/evil.html", headers={"Cookie": c.cookie})
        r = conn.getresponse(); r.read()
        self.assertEqual(r.getheader("Content-Type"), "application/octet-stream")
        self.assertIn("attachment", r.getheader("Content-Disposition"))

    # ---- user management rules
    def test_editor_can_add_user_but_not_delete_or_promote(self):
        e = self.as_("ed")
        self.assertEqual(e.req("POST", "/api/users", {"username": "newbie", "password": PW, "role": "user", "depts": ["HR"]})[0], 200)
        self.assertEqual(e.req("POST", "/api/users", {"username": "sneaky", "password": PW, "role": "editor"})[0], 403)
        s, r = e.req("GET", "/api/users")
        uid = next(u["id"] for u in r["users"] if u["username"] == "newbie")
        boss_id = next(u["id"] for u in r["users"] if u["username"] == "boss")
        self.assertEqual(e.req("POST", f"/api/users/{uid}/update", {"password": "Another#Pass1"})[0], 200)
        self.assertEqual(e.req("POST", f"/api/users/{uid}/update", {"role": "editor"})[0], 403)
        self.assertEqual(e.req("POST", f"/api/users/{uid}/delete")[0], 403)
        self.assertEqual(e.req("POST", f"/api/users/{boss_id}/update", {"password": "Hijack#Pass1"})[0], 403)

    def test_superadmin_can_delete_users_and_change_roles(self):
        b = self.as_("boss")
        b.req("POST", "/api/users", {"username": "temp1", "password": PW, "role": "user"})
        s, r = b.req("GET", "/api/users")
        uid = next(u["id"] for u in r["users"] if u["username"] == "temp1")
        self.assertEqual(b.req("POST", f"/api/users/{uid}/update", {"role": "editor"})[0], 200)
        self.assertEqual(b.req("POST", f"/api/users/{uid}/delete")[0], 200)

    def test_last_superadmin_cannot_be_demoted(self):
        b = self.as_("boss")
        s, r = b.req("GET", "/api/users")
        me = next(u["id"] for u in r["users"] if u["username"] == "boss")
        self.assertEqual(b.req("POST", f"/api/users/{me}/update", {"role": "user"})[0], 400)

    def test_short_password_rejected(self):
        self.assertEqual(self.as_("boss").req("POST", "/api/users", {"username": "shorty", "password": "1234"})[0], 400)

    def test_login_throttle(self):
        c = Client(self.port)
        for _ in range(5):
            c.login("hr", "wrong-password")
        self.assertEqual(c.login("hr")[0], 429)

    # ---- live updates
    def test_live_event_on_change(self):
        c = self.as_("hr")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/events?path=/HR", headers={"Cookie": c.cookie})
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn(b"event: ready", r.readline() + r.readline() + r.readline() + r.readline())
        (TMP / "share" / "HR" / "made-outside.txt").write_text("via samba")   # change outside the app
        deadline, got = time.time() + 6, b""
        while time.time() < deadline and b"event: change" not in got:
            got += r.readline()
        self.assertIn(b"event: change", got)

    def test_profile_picture(self):
        c = self.as_("ed")
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 200
        s, r = c.req("PUT", "/api/me/avatar", raw=png, headers={"Content-Type": "image/png"})
        self.assertEqual(s, 200, r)
        self.assertGreater(r["user"]["avatar"], 0)
        uid = r["user"]["id"]
        self.assertGreater(c.req("GET", "/api/me")[1]["user"]["avatar"], 0)
        # any signed-in user may see it (shown in the user list); anonymous may not
        s, body = self.as_("acc").req("GET", f"/api/avatar?id={uid}")
        self.assertEqual((s, body), (200, png))
        self.assertEqual(Client(self.port).req("GET", f"/api/avatar?id={uid}")[0], 401)
        # not an image / too big / no CSRF header
        self.assertEqual(c.req("PUT", "/api/me/avatar", raw=b"<svg onload=alert(1)>")[0], 400)
        self.assertEqual(c.req("PUT", "/api/me/avatar", raw=png + b"\0" * 1_000_001)[0], 413)
        self.assertEqual(c.req("PUT", "/api/me/avatar", raw=png, headers={"X-LANShare": "0"})[0], 403)
        self.assertEqual(c.req("POST", "/api/me/avatar/delete")[0], 200)
        self.assertEqual(c.req("GET", "/api/me")[1]["user"]["avatar"], 0)
        self.assertEqual(c.req("GET", f"/api/avatar?id={uid}")[0], 404)
        self.assertEqual(c.req("GET", "/api/avatar?id=../../etc")[0], 404)


if __name__ == "__main__":
    unittest.main()
