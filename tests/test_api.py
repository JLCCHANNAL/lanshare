"""End-to-end API tests: start a real server on a temp folder and exercise roles and safety rules.

Run:  python -m unittest discover -s tests -v
"""
import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.parse
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

    def test_org_name(self):
        boss = self.as_("boss")
        s, r = boss.req("POST", "/api/settings/org", {"name": "  Jlc   Industry ", "tagline": "ระบบแชร์ไฟล์"})
        self.assertEqual((s, r["name"]), (200, "Jlc Industry"))
        self.assertEqual(self.as_("acc").req("POST", "/api/settings/org", {"name": "x"})[0], 403)
        self.assertEqual(self.as_("ed").req("POST", "/api/settings/org", {"name": "x"})[0], 403)
        self.assertEqual(boss.req("POST", "/api/settings/org", {"name": "<script>"})[0], 400)
        self.assertEqual(boss.req("POST", "/api/settings/org", {"name": ""})[0], 400)
        s, page = Client(self.port).req("GET", "/")
        self.assertIn("Jlc Industry".encode(), page)
        self.assertEqual(boss.req("GET", "/api/me")[1]["app"], "Jlc Industry")

    def test_delete_own_account(self):
        server.create_user_cli("leaver", "user", ["HR"])
        c = self.as_("leaver")
        self.assertEqual(c.req("POST", "/api/me/delete", {"password": "wrong-password"})[0], 400)
        self.assertEqual(c.req("GET", "/api/me")[0], 200)           # still there
        self.assertEqual(c.req("POST", "/api/me/delete", {"password": PW})[0], 200)
        self.assertEqual(c.req("GET", "/api/me")[0], 401)           # session gone
        self.assertEqual(Client(self.port).login("leaver")[0], 401)  # account gone
        # the only super admin cannot remove themself
        s, r = self.as_("boss").req("POST", "/api/me/delete", {"password": PW})
        self.assertEqual(s, 400)
        self.assertIn("คนสุดท้าย", r["error"])

    def uid(self, client):
        return client.req("GET", "/api/me")[1]["user"]["id"]

    def test_chat_rooms_and_permissions(self):
        acc, hr, ed = self.as_("acc"), self.as_("hr"), self.as_("ed")
        convs = [c["id"] for c in acc.req("GET", "/api/chat/convs")[1]["convs"]]
        self.assertEqual(convs[:1], ["all"])
        self.assertIn("dept:Accounting", convs)
        self.assertNotIn("dept:HR", convs)
        self.assertEqual(acc.req("GET", "/api/chat/messages?c=dept:HR")[0], 403)
        self.assertEqual(acc.req("POST", "/api/chat/send", {"c": "dept:HR", "body": "x"})[0], 403)
        self.assertEqual(acc.req("POST", "/api/chat/send", {"c": "dept:Nope", "body": "x"})[0], 403)
        self.assertEqual(acc.req("POST", "/api/chat/send", {"c": "all", "body": "   "})[0], 400)
        self.assertEqual(acc.req("POST", "/api/chat/send", {"c": "all", "body": "x" * 2001})[0], 400)
        s, r = acc.req("POST", "/api/chat/send", {"c": "dept:Accounting", "body": "งบ Q4 อยู่ใน Accounting นะ"})
        self.assertEqual(s, 200, r)
        self.assertEqual(ed.req("GET", "/api/chat/messages?c=dept:Accounting")[1]["messages"][-1]["body"], "งบ Q4 อยู่ใน Accounting นะ")
        self.assertEqual(hr.req("GET", "/api/chat/messages?c=dept:Accounting")[0], 403)

    def test_chat_direct_messages_unread_and_live(self):
        acc, hr, ed = self.as_("acc"), self.as_("hr"), self.as_("ed")
        a, h = self.uid(acc), self.uid(hr)
        dm = f"dm:{min(a, h)}-{max(a, h)}"
        # hr listens on the live channel
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/chat/events", headers={"Cookie": hr.cookie})
        live = conn.getresponse()
        self.assertEqual(live.status, 200)
        got = b""
        while b"event: ready" not in got:
            got += live.readline()
        s, r = acc.req("POST", "/api/chat/send", {"c": dm, "body": "สวัสดี <b>hr</b>"})
        self.assertEqual(s, 200, r)
        deadline, got = time.time() + 5, b""
        while time.time() < deadline and b"event: msg" not in got:
            got += live.readline()
        got += live.readline()
        self.assertIn("สวัสดี <b>hr</b>".encode(), got)                  # stored as text; the page escapes it
        conn.close()
        item = next(c for c in hr.req("GET", "/api/chat/convs")[1]["convs"] if c["id"] == dm)
        self.assertEqual((item["kind"], item["unread"]), ("dm", 1))
        self.assertEqual(hr.req("POST", "/api/chat/read", {"c": dm})[0], 200)
        item = next(c for c in hr.req("GET", "/api/chat/convs")[1]["convs"] if c["id"] == dm)
        self.assertEqual(item["unread"], 0)
        # a third person can neither read nor write it, nor spoof a pair they are not in
        self.assertEqual(ed.req("GET", f"/api/chat/messages?c={dm}")[0], 403)
        self.assertEqual(ed.req("POST", "/api/chat/send", {"c": dm, "body": "x"})[0], 403)
        self.assertEqual(acc.req("POST", "/api/chat/send", {"c": f"dm:{max(a, h)}-{min(a, h)}", "body": "x"})[0], 403)

    def test_chat_forgotten_when_account_deleted(self):
        server.create_user_cli("temp1", "user", ["Common"])
        t, acc = self.as_("temp1"), self.as_("acc")
        ti, ai = self.uid(t), self.uid(acc)
        dm = f"dm:{min(ti, ai)}-{max(ti, ai)}"
        t.req("POST", "/api/chat/send", {"c": dm, "body": "private"})
        t.req("POST", "/api/chat/send", {"c": "all", "body": "bye all"})
        self.assertEqual(t.req("POST", "/api/me/delete", {"password": PW})[0], 200)
        self.assertNotIn(dm, [c["id"] for c in acc.req("GET", "/api/chat/convs")[1]["convs"]])
        last = acc.req("GET", "/api/chat/messages?c=all")[1]["messages"][-1]
        self.assertEqual((last["body"], last["sender"], last["gone"]), ("bye all", 0, True))

    def test_login_with_display_name(self):
        boss = self.as_("boss")
        self.assertEqual(boss.req("POST", "/api/users", {"username": "smart", "display_name": "sltch", "password": PW, "role": "user", "depts": ["HR"]})[0], 200)
        self.assertEqual(Client(self.port).login("smart")[0], 200)
        self.assertEqual(Client(self.port).req("POST", "/api/login", {"username": " SLTCH ", "password": PW})[0], 200)
        self.assertEqual(Client(self.port).req("POST", "/api/login", {"username": "sltch", "password": "wrong-pass"})[0], 401)
        # two people with the same display name: ambiguous, so only the login name works
        boss.req("POST", "/api/users", {"username": "smart2", "display_name": "sltch", "password": PW, "role": "user", "depts": []})
        self.assertEqual(Client(self.port).req("POST", "/api/login", {"username": "sltch", "password": PW})[0], 401)
        self.assertEqual(Client(self.port).login("smart2")[0], 200)

    def test_activity_log(self):
        boss, acc = self.as_("boss"), self.as_("acc")
        acc.req("PUT", "/api/upload?path=" + urllib.parse.quote("/Accounting/act-test.txt"), raw=b"hi")
        acc.req("GET", "/api/download?path=" + urllib.parse.quote("/Accounting/act-test.txt"))
        self.assertEqual(acc.req("GET", "/api/activity")[0], 403)
        self.assertEqual(self.as_("ed").req("GET", "/api/activity")[0], 403)
        s, r = boss.req("GET", "/api/activity?user=acc&group=files")
        self.assertEqual(s, 200)
        acts = [(i["action"], i["detail"]) for i in r["items"]]
        self.assertIn(("upload", "/Accounting/act-test.txt"), acts)
        self.assertIn(("download", "/Accounting/act-test.txt"), acts)
        self.assertEqual(r["items"][0]["ip"], "127.0.0.1")
        self.assertTrue(all(i["username"] == "acc" for i in r["items"]))
        self.assertIn("acc", r["users"])
        s, body = boss.req("GET", "/api/activity?q=act-test")
        self.assertTrue(body["items"])
        s, csv = boss.req("GET", "/api/activity/csv?user=acc")
        self.assertEqual(s, 200)
        self.assertTrue(csv.startswith("﻿เวลา".encode()))
        self.assertIn(b"/Accounting/act-test.txt", csv)

    def test_parse_samba_audit(self):
        p = server.parse_smb_audit
        self.assertEqual(p("acc01|192.168.100.145|Accounting|unlinkat|ok|sub/report.xlsx"),
                         {"username": "acc01", "ip": "192.168.100.145", "action": "delete", "detail": "/Accounting/sub/report.xlsx"})
        self.assertEqual(p("acc01|1.2.3.4|Accounting|openat|ok|w|a.docx")["action"], "smb-write")
        self.assertIsNone(p("acc01|1.2.3.4|Accounting|openat|ok|r|a.docx"))            # reads are skipped
        self.assertIsNone(p("acc01|1.2.3.4|Accounting|unlinkat|fail (No such file)|x"))
        self.assertIsNone(p("acc01|1.2.3.4|Accounting|openat|ok|w|~$a.docx"))          # Office lock file
        self.assertEqual(p("acc01|1.2.3.4|Common|renameat|ok|a.txt|b.txt")["detail"], "/Common/a.txt -> b.txt")
        self.assertEqual(p("acc01|1.2.3.4|Common|renameat|ok|~WRL0001.tmp|plan.docx"),       # Office save
                         {"username": "acc01", "ip": "1.2.3.4", "action": "smb-write", "detail": "/Common/plan.docx"})
        self.assertEqual(p("acc01|1.2.3.4|Common|renameat|ok|a.txt|.recycle/acc01/a.txt")["action"], "delete")
        self.assertEqual(p("acc01|1.2.3.4|Sales|mkdirat|ok|Q4")["detail"], "/Sales/Q4")
        root = server.ROOT.as_posix().rstrip("/")                                          # newer Samba: absolute paths
        self.assertEqual(p(f"acc01|1.2.3.4|Accounting|unlinkat|ok|{root}/Accounting/x.txt")["detail"], "/Accounting/x.txt")
        self.assertEqual(p(f"acc01|1.2.3.4|Accounting|renameat|ok|{root}/Accounting/a.txt|{root}/Accounting/b.txt")["detail"],
                         "/Accounting/a.txt -> b.txt")

    def test_folder_upload(self):
        acc, boss = self.as_("acc"), self.as_("boss")
        up = lambda c, path, extra="&mkdirs=1": c.req("PUT", "/api/upload?path=" + urllib.parse.quote(path) + extra, raw=b"data")
        self.assertEqual(up(acc, "/Accounting/Project X/2026/q1.xlsx")[0], 200)
        self.assertEqual(up(acc, "/Accounting/Project X/notes.txt")[0], 200)
        self.assertTrue((TMP / "share" / "Accounting" / "Project X" / "2026" / "q1.xlsx").is_file())
        self.assertEqual(up(acc, "/Accounting/NoParents/a.txt", extra="")[0], 404)          # needs mkdirs=1
        self.assertEqual(up(acc, "/Accounting/Project X/notes.txt")[0], 409)                # exists, no overwrite
        self.assertEqual(up(acc, "/Accounting/Project X/notes.txt", "&mkdirs=1&overwrite=1")[0], 200)
        self.assertEqual(up(acc, "/HR/Sneaky/a.txt")[0], 403)                               # not acc's department
        self.assertEqual(up(acc, "/NewDept/a.txt")[0], 403)                                 # users cannot add departments
        self.assertEqual(up(acc, "/Accounting/bad:name/a.txt")[0], 400)
        self.assertEqual(up(boss, "/Marketing/plan.txt")[0], 200)                           # admins can
        self.assertTrue((TMP / "share" / "Marketing" / "plan.txt").is_file())
        shutil.rmtree(TMP / "share" / "Marketing")

    def test_chat_log_for_admin(self):
        boss, acc, hr, ed = self.as_("boss"), self.as_("acc"), self.as_("hr"), self.as_("ed")
        a, h = self.uid(acc), self.uid(hr)
        acc.req("POST", "/api/chat/send", {"c": f"dm:{min(a, h)}-{max(a, h)}", "body": "เรื่องลับ"})
        acc.req("POST", "/api/chat/send", {"c": "all", "body": "ประกาศทุกคน"})
        self.assertEqual(ed.req("GET", "/api/chatlog")[0], 403)
        self.assertEqual(acc.req("GET", "/api/chatlog")[0], 403)
        s, r = boss.req("GET", f"/api/chatlog?user={a}")
        self.assertEqual(s, 200)
        dm = next(i for i in r["items"] if i["kind"] == "dm")
        self.assertEqual((dm["to"], dm["body"]), ("hr", "เรื่องลับ"))                    # super admin sees private text
        room = next(i for i in r["items"] if i["kind"] == "all")
        self.assertEqual(room["body"], "ประกาศทุกคน")
        acts = boss.req("GET", "/api/activity?user=boss")[1]["items"]
        self.assertTrue(any(i["action"] == "view-chatlog" for i in acts))                # viewing is recorded
        one = boss.req("GET", "/api/chatlog?conv=" + dm["conv"])[1]["items"]
        self.assertTrue(one and all(i["conv"] == dm["conv"] for i in one))
        self.assertEqual(hr.req("GET", "/api/chatlog")[0], 403)
        only_dm = boss.req("GET", "/api/chatlog?kind=dm")[1]["items"]
        self.assertTrue(only_dm and all(i["kind"] == "dm" for i in only_dm))

    def test_chat_reactions(self):
        acc, ed, hr = self.as_("acc"), self.as_("ed"), self.as_("hr")
        mid = acc.req("POST", "/api/chat/send", {"c": "dept:Accounting", "body": "ส่งงบแล้วนะ"})[1]["message"]["id"]
        a, e = self.uid(acc), self.uid(ed)
        s, r = ed.req("POST", "/api/chat/react", {"id": mid, "emoji": "❤️"})
        self.assertEqual((s, r["reactions"]), (200, {"❤️": [e]}))
        acc.req("POST", "/api/chat/react", {"id": mid, "emoji": "👍"})
        self.assertEqual(sorted(ed.req("POST", "/api/chat/react", {"id": mid, "emoji": "👍"})[1]["reactions"]["👍"]), sorted([a, e]))  # changed
        self.assertEqual(ed.req("POST", "/api/chat/react", {"id": mid, "emoji": "👍"})[1]["reactions"], {"👍": [a]})     # same again = remove
        msg = next(m for m in acc.req("GET", "/api/chat/messages?c=dept:Accounting")[1]["messages"] if m["id"] == mid)
        self.assertEqual(msg["reactions"], {"👍": [a]})
        self.assertEqual(ed.req("POST", "/api/chat/react", {"id": mid, "emoji": "🍕"})[0], 400)      # only the six
        self.assertEqual(hr.req("POST", "/api/chat/react", {"id": mid, "emoji": "👍"})[0], 404)      # not in that room
        self.assertEqual(ed.req("POST", "/api/chat/react", {"id": 999999, "emoji": "👍"})[0], 404)

    def test_chat_images(self):
        acc, hr, ed = self.as_("acc"), self.as_("hr"), self.as_("ed")
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 300
        url = "/api/chat/image?c=dept:Accounting&caption=" + urllib.parse.quote("ใบเสร็จครับ")
        s, r = acc.req("PUT", url, raw=png, headers={"Content-Type": "image/png"})
        self.assertEqual(s, 200, r)
        m = r["message"]
        self.assertTrue(m["image"])
        self.assertEqual(m["body"], "ใบเสร็จครับ")
        self.assertEqual(ed.req("GET", f"/api/chat/image?id={m['id']}"), (200, png))       # editor can open Accounting
        self.assertEqual(hr.req("GET", f"/api/chat/image?id={m['id']}")[0], 404)           # hr is not in that room
        self.assertEqual(Client(self.port).req("GET", f"/api/chat/image?id={m['id']}")[0], 401)
        self.assertTrue(ed.req("GET", "/api/chat/messages?c=dept:Accounting")[1]["messages"][-1]["image"])
        # picture only, no caption
        s, r = acc.req("PUT", "/api/chat/image?c=all", raw=b"GIF89a" + b"\0" * 50)
        self.assertEqual((s, r["message"]["body"]), (200, ""))
        # not a picture / not allowed room / too big
        self.assertEqual(acc.req("PUT", "/api/chat/image?c=all", raw=b"<html>")[0], 400)
        self.assertEqual(acc.req("PUT", "/api/chat/image?c=dept:HR", raw=png)[0], 403)
        try:                                   # the server refuses before reading 10 MB; Windows may report a dropped connection
            self.assertEqual(acc.req("PUT", "/api/chat/image?c=all", raw=png + b"\0" * (10 * 1024 * 1024))[0], 413)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass
        self.assertEqual(acc.req("PUT", "/api/chat/image?c=all", raw=png, headers={"X-LANShare": "0"})[0], 403)

    def test_profile_change_is_pushed_live(self):
        hr, acc = self.as_("ed"), self.as_("acc")   # hr may be locked out by the throttle test
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/chat/events", headers={"Cookie": hr.cookie})
        live = conn.getresponse()
        got = b""
        while b"event: ready" not in got:
            got += live.readline()
        acc.req("PUT", "/api/me/avatar", raw=b"\x89PNG\r\n\x1a\n" + b"\0" * 64, headers={"Content-Type": "image/png"})
        deadline, got = time.time() + 5, b""
        while time.time() < deadline and b"event: profile" not in got:
            got += live.readline()
        got += live.readline()
        conn.close()
        self.assertIn(b"event: profile", got)
        data = json.loads(got.split(b"event: profile", 1)[1].split(b"data: ", 1)[1].split(b"\n", 1)[0])
        self.assertEqual((data["username"], data["avatar"] > 0), ("acc", True))
        acc.req("POST", "/api/me/avatar/delete")

    def test_chat_presence(self):
        hr, acc = self.as_("hr"), self.as_("acc")
        h = self.uid(hr)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/chat/events", headers={"Cookie": hr.cookie})
        live = conn.getresponse()
        got = b""
        while b"event: ready" not in got:
            got += live.readline()
        r = acc.req("GET", "/api/chat/convs")[1]
        self.assertIn(h, r["online"])
        self.assertTrue(next(p for p in r["people"] if p["id"] == h)["online"])
        conn.close()

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
