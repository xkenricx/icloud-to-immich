"""First-run / setup behavior: access key, Immich address clean-up, friendly errors."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.server as server  # noqa: E402
from app.immich import Immich, ImmichError  # noqa: E402


class AccessKeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"ICLOUD2IMMICH_DATA": self.tmp.name}, clear=False)
        self.env.start()
        os.environ.pop("ICLOUD2IMMICH_TOKEN", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_server_mode_key_is_created_once_and_reused(self):
        first = server._load_token(network=True)
        self.assertGreaterEqual(len(first), 32)
        self.assertTrue((Path(self.tmp.name) / "access-key.txt").exists())
        self.assertEqual(server._load_token(network=True), first)   # same link after restart

    def test_desktop_mode_key_is_fresh_each_launch(self):
        self.assertNotEqual(server._load_token(network=False), server._load_token(network=False))
        self.assertFalse((Path(self.tmp.name) / "access-key.txt").exists())

    def test_env_key_wins_and_must_be_long(self):
        with mock.patch.dict(os.environ, {"ICLOUD2IMMICH_TOKEN": "x" * 30}):
            self.assertEqual(server._load_token(network=True), "x" * 30)
        with mock.patch.dict(os.environ, {"ICLOUD2IMMICH_TOKEN": "short"}):
            with self.assertRaises(SystemExit):
                server._load_token(network=True)


class ImmichAddressTest(unittest.TestCase):
    def test_whatever_is_in_the_address_bar_works(self):
        cases = {
            "192.168.1.10:2283": "http://192.168.1.10:2283",
            "http://192.168.1.10:2283/photos": "http://192.168.1.10:2283",
            "https://photos.example.com/albums/abc?x=1": "https://photos.example.com",
            "http://nas.local:30041/api/": "http://nas.local:30041",
            "  10.0.0.5:2283/  ": "http://10.0.0.5:2283",
        }
        for given, want in cases.items():
            self.assertEqual(Immich(given, "k").url, want, given)

    def test_nonsense_address_gets_a_plain_message(self):
        with self.assertRaises(ImmichError):
            Immich("http://", "k")


class FriendlyErrorsTest(unittest.TestCase):
    def setUp(self):
        self.app = server.App()

    def test_localhost_is_explained_in_server_mode(self):
        self.app.mode = "server"
        with self.assertRaisesRegex(ValueError, "not 'localhost'"):
            self.app.connect_immich("localhost:2283", "key")

    def test_missing_fields_say_what_to_do(self):
        with self.assertRaisesRegex(ValueError, "Immich address"):
            self.app.connect_immich("", "key")
        with self.assertRaisesRegex(ValueError, "API key"):
            self.app.connect_immich("192.168.1.10:2283", "")
        with self.assertRaisesRegex(ValueError, "Apple ID"):
            self.app.sign_in("", "pw")


if __name__ == "__main__":
    unittest.main()
