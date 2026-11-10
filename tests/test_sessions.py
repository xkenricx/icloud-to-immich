"""Several people sharing one server: each browser is its own private session."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import app.server as server  # noqa: E402


class SharedServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"ICLOUD2IMMICH_DATA": cls.tmp.name})
        cls.env.start()
        import demo_server  # noqa: F401  (patches server.ICloud / server.Immich with fakes)
        cls.demo = demo_server
        server.MODE, server.NETWORK_MODE, server.TOKEN = "server", True, "k" * 32
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        server._shutdown_all()
        cls.env.stop()
        cls.tmp.cleanup()

    def call(self, sid, path, body=None, runs=""):
        url = self.base + path + (f"?runs={runs}" if runs else "")
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="GET" if body is None else "POST",
                                     headers={"X-Token": "k" * 32, "X-Session": sid,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def sign_in(self, sid, name):
        self.assertEqual(self.call(sid, "/api/immich", {"url": "192.168.1.10:2283",
                                                         "email": f"{name}@home", "password": "pw"})[0], 200)
        self.call(sid, "/api/icloud/signin", {"apple_id": f"{name}@icloud.com", "password": "pw"})
        self.assertEqual(self.call(sid, "/api/icloud/code", {"code": "123456"})[0], 200)

    def wait_idle(self, sid):
        for _ in range(200):
            if not self.call(sid, "/api/state")[1]["busy"]:
                return
            time.sleep(0.1)

    def test_two_people_get_separate_private_sessions(self):
        alice, bob = "a" * 32, "b" * 32
        self.sign_in(alice, "alice")
        _, k = self.call(alice, "/api/state")
        _, e = self.call(bob, "/api/state")
        self.assertEqual(k["immich"]["user"], "Alice")
        self.assertEqual(k["icloud"]["status"], "signed_in")
        self.assertTrue(k["immich"]["with_password"])
        # Bob's browser sees a fresh session, not Alice's sign-ins
        self.assertFalse(e["immich"]["connected"])
        self.assertEqual(e["icloud"]["status"], "signed_out")
        self.assertIsNone(e["icloud"]["apple_id"])
        self.sign_in(bob, "bob")
        _, e = self.call(bob, "/api/state")
        self.assertEqual(e["immich"]["user"], "Bob")
        self.assertNotEqual(e["run_id"], k["run_id"])
        # overview counts people without revealing who
        _, o = self.call(alice, "/api/overview")
        self.assertGreaterEqual(o["signed_in"], 2)
        self.assertNotIn("alice", json.dumps(o).lower())
        # Alice signing out leaves Bob signed in
        self.call(alice, "/api/quit", {})
        self.assertEqual(self.call(bob, "/api/state")[1]["icloud"]["status"], "signed_in")
        self.assertEqual(self.call(alice, "/api/state")[1]["icloud"]["status"], "signed_out")
        self.call(bob, "/api/quit", {})

    def test_same_icloud_account_in_two_browsers_is_refused(self):
        laptop, phone = "c" * 32, "d" * 32
        self.sign_in(laptop, "carol")
        self.call(phone, "/api/immich", {"url": "192.168.1.10:2283", "email": "carol@home", "password": "pw"})
        self.call(phone, "/api/icloud/signin", {"apple_id": "carol@icloud.com", "password": "pw"})
        code, r = self.call(phone, "/api/icloud/code", {"code": "123456"})
        self.assertEqual(code, 400)
        self.assertIn("another browser", r["error"])
        self.assertEqual(self.call(phone, "/api/state")[1]["icloud"]["status"], "signed_out")
        # once the laptop signs out, the phone can take over
        self.call(laptop, "/api/quit", {})
        self.call(phone, "/api/icloud/signin", {"apple_id": "carol@icloud.com", "password": "pw"})
        self.assertEqual(self.call(phone, "/api/icloud/code", {"code": "123456"})[0], 200)
        self.call(phone, "/api/quit", {})

    def test_welcome_back_only_shows_this_browsers_own_runs(self):
        me, other = "e" * 32, "f" * 32
        self.sign_in(me, "alice2")
        self.call(me, "/api/scan", {}); self.wait_idle(me)
        self.call(me, "/api/scope", {"mode": "range", "start": "2026-01-01"})
        self.call(me, "/api/transfer", {}); self.wait_idle(me)
        run = self.call(me, "/api/state")[1]["run_id"]
        self.call(me, "/api/quit", {})
        self.assertEqual([p["run_id"] for p in self.call(me, "/api/state", runs=run)[1]["past"]], [run])
        self.assertEqual(self.call(other, "/api/state")[1]["past"], [])           # sees nothing
        self.assertEqual(self.call(other, "/api/state", runs="0" * 16)[1]["past"], [])

    def test_wrong_immich_password_is_explained(self):
        code, r = self.call("g" * 32, "/api/immich", {"url": "192.168.1.10:2283",
                                                       "email": "x@home", "password": "wrong"})
        self.assertEqual(code, 400)
        self.assertIn("email and password", r["error"])

    def test_household_immich_address_is_offered_to_everyone(self):
        with mock.patch.dict(os.environ, {"ICLOUD2IMMICH_IMMICH_URL": "http://192.168.1.10:30041"}):
            self.assertEqual(self.call("j" * 32, "/api/state")[1]["default_immich_url"],
                             "http://192.168.1.10:30041")
        self.assertEqual(self.call("j" * 32, "/api/state")[1]["default_immich_url"], "")

    def test_requests_without_a_session_id_are_refused(self):
        code, _ = self.call("", "/api/state")
        self.assertEqual(code, 400)

    def test_idle_sessions_are_signed_out_but_never_mid_copy(self):
        idle, copying = "h" * 32, "i" * 32
        self.sign_in(idle, "idle")
        self.sign_in(copying, "copying")
        for sid in (idle, copying):                       # nobody has looked for 20+ minutes
            server.SESSIONS[sid].last_seen -= server.IDLE_EXIT_SECONDS + 5
        real_engine = server.SESSIONS[copying].engine
        server.SESSIONS[copying].engine = types.SimpleNamespace(busy=True, stop=lambda: None)
        try:
            self.assertEqual(server._prune(), 1)
        finally:
            server.SESSIONS[copying].engine = real_engine
        self.assertNotIn(idle, server.SESSIONS)          # signed out
        self.assertIn(copying, server.SESSIONS)          # left alone mid-copy
        self.call(copying, "/api/quit", {})


if __name__ == "__main__":
    unittest.main()
