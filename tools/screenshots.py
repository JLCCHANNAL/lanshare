"""Developer tool: regenerate the README screenshots with headless Chrome.

Starts a throwaway server on 127.0.0.1 with demo data, signs requests in as the chosen
demo user (this override exists only inside this script), and saves PNGs to docs/.
Usage:  python tools/screenshots.py "C:/Program Files/Google/Chrome/Application/chrome.exe"
"""
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="lanshare-shots-"))
os.environ.update(LANSHARE_ROOT=str(TMP / "share"), LANSHARE_DATA=str(TMP / "data"))
DEMO = {
    "เอกสารกลาง": ["ประกาศวันหยุดปี 2027.pdf", "แบบฟอร์มลางาน.docx", "ระเบียบบริษัท.pdf", "โลโก้บริษัท.png"],
    "บัญชี": ["งบประมาณ 2027.xlsx", "ใบแจ้งหนี้ พ.ย. 2026.pdf", "สรุปค่าใช้จ่าย Q3.xlsx", "ใบเสร็จ/"],
    "ฝ่ายขาย": ["ใบเสนอราคา ลูกค้า A.docx", "รายชื่อลูกค้า.xlsx"],
    "ฝ่ายบุคคล": ["สัญญาจ้าง (แม่แบบ).docx"],
}
for dept, items in DEMO.items():
    for it in items:
        p = TMP / "share" / dept / it.rstrip("/")
        if it.endswith("/"):
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(os.urandom(2000 + len(it) * 997))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lanshare import server  # noqa: E402

os.environ["LANSHARE_PASSWORD"] = "unused-demo-password"
server.create_user_cli("admin", "superadmin", [])
server.create_user_cli("editor", "editor", [])
server.create_user_cli("somchai", "user", ["บัญชี", "เอกสารกลาง"])
with server.db() as con:
    con.execute("UPDATE users SET display_name='สมชาย ใจดี' WHERE username='somchai'")
    con.execute("UPDATE users SET display_name='วิภา (ผู้แก้ไข)' WHERE username='editor'")
    con.execute("UPDATE users SET display_name='ผู้ดูแลระบบ' WHERE username='admin'")

AS_USER = {"name": "admin"}


class DemoHandler(server.Handler):
    def current_user(self):  # demo only: every request is the chosen user
        with server.db() as con:
            uid = con.execute("SELECT id FROM users WHERE username=?", (AS_USER["name"],)).fetchone()[0]
            return server.load_user(con, uid)

    def api_get_events(self):  # headless Chrome waits for open connections; send one event and close
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", "26")
        self.end_headers()
        self.wfile.write(b"retry: 600000\n\nevent: ready\n\n"[:26])


httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()

chrome = sys.argv[1] if len(sys.argv) > 1 else "chrome"
docs = Path(__file__).resolve().parents[1] / "docs"
docs.mkdir(exist_ok=True)
SHOTS = [  # (file, user, query, hash)
    ("screenshot-files-light.png", "admin", "mode=light&accent=blue", "#/บัญชี"),
    ("screenshot-files-dark.png", "somchai", "mode=dark&accent=violet", "#/เอกสารกลาง"),
    ("screenshot-home.png", "somchai", "mode=light&accent=teal", "#/"),
    ("screenshot-users.png", "admin", "mode=light&accent=blue", "#users"),
]
for fname, user, query, frag in SHOTS:
    AS_USER["name"] = user
    args = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=1440,860",
            f"--user-data-dir={TMP / 'profile'}", "--virtual-time-budget=6000",
            f"--screenshot={docs / fname}", f"http://127.0.0.1:{port}/?{query}{frag}"]
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("saved", fname)
httpd.shutdown()
