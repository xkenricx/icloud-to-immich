"""Uploads must stream: a 1.75 GB video once got the app OOM-killed in a 512 MB container."""

from __future__ import annotations

import re
import sys
import tempfile
import threading
import tracemalloc
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.immich import Immich, StreamingMultipart  # noqa: E402


class SameBytesAsRequestsTest(unittest.TestCase):
    def test_body_matches_requests_encoding_exactly(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.heic"
            f.write_bytes(b"\x00\x01binary\xffdata" * 50)
            fields = {"fileCreatedAt": "2024-01-01T00:00:00+00:00", "filename": 'IMG "1".HEIC',
                      "isFavorite": "false"}
            with f.open("rb") as fh:
                req = requests.Request("POST", "http://x/", data=fields,
                                       files={"assetData": ('IMG "1".HEIC', fh)}).prepare()
            boundary = re.search(r"boundary=(\S+)", req.headers["Content-Type"]).group(1)
            ours = StreamingMultipart(fields, "assetData", 'IMG "1".HEIC', f, boundary=boundary)
            with ours:
                body = b""
                while chunk := ours.read(7):          # odd block size on purpose
                    body += chunk
            self.assertEqual(len(ours), len(req.body))
            # requests adds "Content-Type: application/octet-stream" only when it can
            # guess one; compare with that header removed so the layout is what's tested.
            strip = lambda b: re.sub(rb"Content-Type: [^\r]+\r\n", b"", b)  # noqa: E731
            self.assertEqual(strip(body), strip(req.body))


class _Sink(BaseHTTPRequestHandler):
    received = 0

    def do_POST(self):
        left = int(self.headers["Content-Length"])
        while left:
            left -= len(self.rfile.read(min(left, 1 << 20)))
            _Sink.received = int(self.headers["Content-Length"]) - left
        out = b'{"id":"abc","status":"created"}'
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class BigFileMemoryTest(unittest.TestCase):
    def test_300mb_upload_uses_little_memory(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Sink)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                big = Path(d) / "big.mov"
                with big.open("wb") as fh:           # sparse file: instant, no disk used
                    fh.truncate(300 * 1024 * 1024)
                im = Immich(f"127.0.0.1:{httpd.server_port}", "k")
                tracemalloc.start()
                aid, status = im.upload(big, "big.mov", datetime.now(timezone.utc))
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            self.assertEqual((aid, status), ("abc", "created"))
            # the whole file arrived (plus a few hundred bytes of form fields)
            self.assertGreater(_Sink.received, 300 * 1024 * 1024)
            self.assertLess(_Sink.received, 300 * 1024 * 1024 + 4096)
            self.assertLess(peak, 32 * 1024 * 1024, f"peak {peak / 1048576:.0f} MB")
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
