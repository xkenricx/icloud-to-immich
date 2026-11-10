"""Run the real wizard against a fake iCloud + fake Immich, for trying the UI
without real accounts. Not shipped in the app.

    python tests/demo_server.py [--server] [--port 8765]

Any Immich address/key and Apple ID/password work; the 2FA code is 123456.
--server mimics the Docker mode (shows the access-key screen when the key is missing).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("ICLOUD2IMMICH_DATA", tempfile.mkdtemp(prefix="i2i-demo-"))

import app.server as server  # noqa: E402
from app.icloud import ICloudError, Item, Part  # noqa: E402
from app.immich import Immich, ImmichError  # noqa: E402
from test_engine import FakeImmich  # noqa: E402


def make_library() -> list[Item]:
    now = datetime.now(timezone.utc)
    items = []
    for i in range(120):
        taken = now - timedelta(days=i * 9 + 1)
        parts = [Part("original", f"IMG_{1000 + i}.HEIC", 2_400_000)]
        if i % 3 == 0:
            parts.append(Part("live_video", f"IMG_{1000 + i}.MOV", 1_800_000))
        if i % 7 == 0:
            parts.append(Part("edited", f"IMG_{1000 + i}_edited.HEIC", 900_000))
        albums = ["Holiday"] if 10 <= i < 16 else (["Family"] if i % 25 == 0 else [])
        items.append(Item(id=f"demo{i}", filename=f"IMG_{1000 + i}.HEIC", kind="image",
                          taken=taken, parts=parts, albums=albums))
    return sorted(items, key=lambda x: x.taken)


class DemoICloud:
    def __init__(self):
        self.apple_id = None
        self.items = make_library()
        self.deleted = []

    def sign_in(self, apple_id, password):
        if password == "wrong":
            raise ICloudError("Apple ID or password is wrong.")
        self.apple_id = apple_id
        return "needs_2fa"

    def delivery_method(self):
        return "trusted_device"

    def verify_code(self, code):
        return code.strip() == "123456"

    def check_photos_access(self):
        return len(self.items)

    def scan(self, progress):
        for n in range(0, len(self.items) + 1, 20):
            progress(n, len(self.items))
            time.sleep(0.15)
        return [i for i in self.items if i.id not in self.deleted]

    def download(self, item_id, role, dest):
        time.sleep(0.05)
        data = hashlib.sha256(f"{item_id}:{role}".encode()).digest() * 4
        dest.write_bytes(data)
        return len(data), hashlib.sha1(data).hexdigest()

    def delete(self, item_id):
        self.deleted.append(item_id)

    def close(self):
        pass


class DemoImmich(FakeImmich):
    def __init__(self, url, key):
        super().__init__()
        self._real = Immich(url, key)     # reuse the real address clean-up + its errors
        self.url = self._real.url
        self._key = key

    def connect(self):
        if self._key.strip() == "wrong":
            raise ImmichError("Immich didn't accept that API key.")
        return {"version": "demo", "user": "Demo User", "email": "demo@example.com"}


server.ICloud = DemoICloud
# Sizes in the fake library don't match the tiny fake files; skip the size check.
server.Immich = DemoImmich
_orig_transfer = server.Engine.transfer_item


def _transfer(self, item, work):
    for p in item.parts:
        p.size = None
    return _orig_transfer(self, item, work)


server.Engine.transfer_item = _transfer

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", action="store_true")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    # Always listens on this computer only; --server just switches the behavior.
    server.serve(open_browser=False, port=a.port, host="127.0.0.1",
                 idle_exit=False, server_mode=a.server)
