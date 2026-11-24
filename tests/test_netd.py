"""Tests for the host network helper (hostd/netd.py) and LANShare's /api/net proxy.

The helper runs in dry-run mode against temporary copies of /etc/network/interfaces
and /etc/hosts, so nothing on the machine running the tests is touched.
"""
import importlib.util
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("netd", ROOT / "hostd" / "netd.py")
netd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(netd)

INTERFACES = """auto lo
iface lo inet loopback

iface nic0 inet manual

auto vmbr0
iface vmbr0 inet static
\taddress 192.168.100.250/24
\tgateway 192.168.100.1
\tbridge-ports nic0
\tbridge-stp off
\tbridge-fd 0

source /etc/network/interfaces.d/*

auto vmbr1
iface vmbr1 inet static
    address 10.10.10.1/24
    bridge-ports none
    post-up iptables -t nat -A PREROUTING -i vmbr0 -p tcp --dport 8080 -j DNAT --to-destination 10.10.10.20:8080
"""
HOSTS = "127.0.0.1 localhost.localdomain localhost\n192.168.100.250 pve.localdomain pve\n"


def setup_helper():
    tmp = Path(tempfile.mkdtemp(prefix="netd-test-"))
    (tmp / "interfaces").write_text(INTERFACES)
    (tmp / "hosts").write_text(HOSTS)
    (tmp / "state").mkdir()
    (tmp / "state" / "token").write_text("secret-token\n")
    netd.DIR, netd.INTERFACES, netd.HOSTS = tmp / "state", tmp / "interfaces", tmp / "hosts"
    netd.DRY, netd.MDNS, netd.ALLOW = True, False, set()
    netd.STATE = {}
    return tmp


class HelperUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = setup_helper()

    def test_read_and_rewrite_interfaces(self):
        self.assertEqual(netd.read_config(INTERFACES), {"method": "static", "address": "192.168.100.250/24", "gateway": "192.168.100.1"})
        out = netd.rewrite_interfaces(INTERFACES, "192.168.1.250/24", "192.168.1.1")
        self.assertIn("\taddress 192.168.1.250/24\n\tgateway 192.168.1.1\n\tbridge-ports nic0", out)
        self.assertIn("address 10.10.10.1/24", out)          # other bridges untouched
        self.assertIn("--dport 8080", out)
        self.assertEqual(out.count("address"), 2)

    def test_rewrite_from_dhcp(self):
        src = INTERFACES.replace("inet static\n\taddress 192.168.100.250/24\n\tgateway 192.168.100.1\n", "inet dhcp\n")
        out = netd.rewrite_interfaces(src, "192.168.1.250/24", "192.168.1.1")
        self.assertIn("iface vmbr0 inet static\n\taddress 192.168.1.250/24", out)

    def test_rewrite_to_dhcp_and_back(self):
        out = netd.rewrite_interfaces_dhcp(INTERFACES)
        self.assertIn("iface vmbr0 inet dhcp\n\tbridge-ports nic0", out)
        self.assertNotIn("192.168.100.250", out)
        self.assertIn("address 10.10.10.1/24", out)          # internal bridge untouched
        self.assertEqual(netd.read_config(out), {"method": "dhcp"})
        back = netd.rewrite_interfaces(out, "192.168.1.250/24", "192.168.1.1")
        self.assertIn("iface vmbr0 inet static\n\taddress 192.168.1.250/24\n\tgateway 192.168.1.1\n\tbridge-ports nic0", back)

    def test_rewrite_hosts(self):
        out = netd.rewrite_hosts(HOSTS, "192.168.1.250", "pve")
        self.assertIn("192.168.1.250 pve.localdomain pve", out)
        self.assertIn("127.0.0.1 localhost", out)

    def test_profile_validation(self):
        ok = netd.clean_profile({"label": "บ้าน", "address": "192.168.1.250/24", "gateway": "192.168.1.1"})
        self.assertEqual(ok["address"], "192.168.1.250/24")
        bad = [
            {"label": "", "address": "192.168.1.250/24", "gateway": "192.168.1.1"},
            {"label": "x", "address": "192.168.1.250/24", "gateway": "192.168.2.1"},   # other subnet
            {"label": "x", "address": "192.168.1.255/24", "gateway": "192.168.1.1"},   # broadcast
            {"label": "x", "address": "192.168.1.1/24", "gateway": "192.168.1.1"},     # same as gw
            {"label": "x", "address": "10.10.10.50/24", "gateway": "10.10.10.254"},    # internal bridge
            {"label": "x", "address": "192.168.1.250/24; rm -rf /", "gateway": "192.168.1.1"},
            {"label": "x", "address": "192.168.1.250/31", "gateway": "192.168.1.251"},
        ]
        for p in bad:
            with self.assertRaises(netd.NetError, msg=p):
                netd.clean_profile(p)

    def test_mdns_answer(self):
        q = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + netd.enc_name("FileServer.local") + struct.pack("!HH", 1, 0x8001)
        r = netd.mdns_answer(q, "fileserver", "192.168.1.250", legacy=False)
        self.assertIsNotNone(r)
        self.assertEqual(struct.unpack("!HHHH", r[:8]), (0, 0x8400, 0, 1))
        self.assertTrue(r.endswith(socket.inet_aton("192.168.1.250")))
        self.assertIsNone(netd.mdns_answer(q.replace(b"FileServer", b"OtherHost1"), "fileserver", "1.2.3.4", False))
        legacy = netd.mdns_answer(struct.pack("!H", 4321) + q[2:], "fileserver", "192.168.1.250", legacy=True)
        self.assertEqual(struct.unpack("!HHHH", legacy[:8]), (4321, 0x8400, 1, 1))
        self.assertIsNone(netd.mdns_answer(b"\x00\x01garbage", "fileserver", "1.2.3.4", False))


class HelperAndProxyTests(unittest.TestCase):
    """Run the helper's HTTP API and LANShare together, the way they talk in production."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = setup_helper()
        netd.save_state(netd.load_state())
        cls.helper = netd.ThreadingHTTPServer(("127.0.0.1", 0), netd.Handler)
        threading.Thread(target=cls.helper.serve_forever, daemon=True).start()
        import os
        import test_api                       # sets up LANShare's temp folders and env
        server = test_api.server
        server.init_db()
        os.environ["LANSHARE_PASSWORD"] = test_api.PW
        for u, role in (("boss", "superadmin"), ("ed", "editor"), ("acc", "user")):
            server.create_user_cli(u, role, [])
        cls.web = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.web.serve_forever, daemon=True).start()
        cls.server, cls.Client, cls.port = server, test_api.Client, cls.web.server_address[1]
        server.NETD_URL = f"http://127.0.0.1:{cls.helper.server_address[1]}"
        server.NETD_TOKEN_FILE = netd.DIR / "token"

    @classmethod
    def tearDownClass(cls):
        cls.helper.shutdown()
        cls.web.shutdown()
        cls.server.NETD_URL = ""

    def as_(self, user):
        c = self.Client(self.port)
        self.assertEqual(c.login(user)[0], 200)
        return c

    def test_dhcp_mode(self):
        c = self.as_("boss")
        self.assertEqual(self.as_("ed").req("POST", "/api/net/dhcp")[0], 403)
        s, r = c.req("POST", "/api/net/dhcp")
        self.assertEqual(s, 200, r)
        for _ in range(50):
            if "inet dhcp" in netd.INTERFACES.read_text():
                break
            threading.Event().wait(0.1)
        try:
            self.assertIn("iface vmbr0 inet dhcp", netd.INTERFACES.read_text())
            for _ in range(30):
                st = c.req("GET", "/api/net")[1]
                if not st["applying"]:
                    break
                threading.Event().wait(0.1)
            self.assertTrue(st["dhcp"])
            self.assertIsNone(st["active"])
        finally:
            netd.INTERFACES.write_text(INTERFACES)

    def test_only_superadmin(self):
        for u in ("ed", "acc"):
            self.assertEqual(self.as_(u).req("GET", "/api/net")[0], 403)

    def test_bad_token_rejected_by_helper(self):
        good = self.server.NETD_TOKEN_FILE
        bad = self.tmp / "bad-token"
        bad.write_text("nope")
        self.server.NETD_TOKEN_FILE = bad
        try:
            self.assertEqual(self.as_("boss").req("GET", "/api/net")[0], 403)
        finally:
            self.server.NETD_TOKEN_FILE = good

    def test_profiles_and_apply(self):
        c = self.as_("boss")
        s, st = c.req("GET", "/api/net")
        self.assertEqual(s, 200)
        self.assertTrue(st["available"])
        self.assertEqual(st["current"]["address"], "192.168.100.250/24")
        office = {"label": "ที่ทำงาน", "address": "192.168.100.250/24", "gateway": "192.168.100.1"}
        home = {"label": "ที่บ้าน", "address": "192.168.1.250/24", "gateway": "192.168.1.1"}
        s, st = c.req("POST", "/api/net/save", {"profiles": [office, home], "name": "Office-Files", "auto": False})
        self.assertEqual(s, 200, st)
        self.assertEqual(st["name"], "office-files")
        self.assertEqual(len(st["profiles"]), 2)
        self.assertEqual(st["active"], st["profiles"][0]["id"])
        self.assertEqual(c.req("POST", "/api/net/save", {"name": "bad name!"})[0], 400)
        self.assertEqual(c.req("POST", "/api/net/save", {"profiles": [{**home, "gateway": "10.0.0.1"}]})[0], 400)

        home_id = st["profiles"][1]["id"]
        s, r = c.req("POST", "/api/net/apply", {"id": home_id})
        self.assertEqual(s, 200, r)
        self.assertEqual(r["ip"], "192.168.1.250")
        for _ in range(50):                       # the helper switches after a short delay
            if "192.168.1.250/24" in netd.INTERFACES.read_text():
                break
            threading.Event().wait(0.1)
        text = netd.INTERFACES.read_text()
        self.assertIn("\taddress 192.168.1.250/24\n\tgateway 192.168.1.1", text)
        self.assertIn("192.168.1.250 pve.localdomain pve", netd.HOSTS.read_text()) if socket.gethostname().split(".")[0] == "pve" else None
        self.assertTrue((netd.INTERFACES.with_name("interfaces.lanshare-bak")).exists())
        for _ in range(30):
            if c.req("GET", "/api/net")[1]["active"] == home_id:
                break
            threading.Event().wait(0.1)
        self.assertEqual(c.req("GET", "/api/net")[1]["active"], home_id)
        self.assertEqual(c.req("POST", "/api/net/apply", {"id": "missing1"})[0], 404)


if __name__ == "__main__":
    unittest.main()
