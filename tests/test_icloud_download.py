"""Apple's download links expire mid-run; pyicloud raises before we see the 410.

A real run went from seconds per item to a minute per item because each expired
link was fixed with pyicloud's single-photo lookup, which misses and then pages
through the whole library. Links must be refreshed in bulk: one library pass.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pyicloud.exceptions import PyiCloudAuthRequiredException, PyiCloudEndpointGoneException  # noqa: E402

from app import icloud as ic_mod  # noqa: E402
from app.icloud import ICloud, ICloudError, ICloudSessionExpired  # noqa: E402


class _Resp:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def iter_content(self, n):
        yield self.data

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _Asset:
    def __init__(self, aid, gen):
        self.id, self.url = aid, f"https://cvws.example/{gen}/{aid}/${{f}}"

    def download_url(self, version):
        return self.url


class _Library:
    """photos.all: iterating it is the (expensive) full library pass."""

    def __init__(self, ids):
        self.ids, self.gen, self.passes = ids, "fresh", 0
        self.get = mock.Mock(side_effect=AssertionError("single-photo lookup must not be used"))

    def __iter__(self):
        self.passes += 1
        return iter([_Asset(i, self.gen) for i in self.ids])


class LinkRefreshTest(unittest.TestCase):
    def setUp(self):
        self.ids = [f"a{i}" for i in range(20)]
        self.ic = ICloud()
        self.ic.api = mock.MagicMock()
        self.lib = _Library(self.ids)
        self.ic.api.photos.all = self.lib
        self.ic.api.photos.albums = {}
        self.ic._assets = {i: _Asset(i, "stale") for i in self.ids}
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "f"
        mock.patch.object(ic_mod.time, "sleep", lambda s: None).start()

        def get(url, **kw):
            if "/stale/" in url:
                raise PyiCloudEndpointGoneException("cvws.example/stale")     # what Apple sends
            return _Resp(b"bytes-" + url.encode())
        self.ic.api.session.get.side_effect = get

    def tearDown(self):
        mock.patch.stopall()
        self.ic.close()
        self.tmp.cleanup()

    def test_old_links_are_refreshed_once_for_everyone(self):
        self.ic._links_at = time.time() - ic_mod.LINK_MAX_AGE - 1      # scanned > 40 min ago
        for i in self.ids:
            self.ic.download(i, "original", self.dest)
        self.assertEqual(self.lib.passes, 1)                  # one pass, not one per item
        self.lib.get.assert_not_called()

    def test_a_link_that_expires_early_still_costs_one_pass(self):
        self.ic._links_at = time.time() - 300                 # "young", but Apple says gone
        for i in self.ids:
            self.ic.download(i, "original", self.dest)
        self.assertEqual(self.lib.passes, 1)
        self.lib.get.assert_not_called()

    def test_fresh_links_need_no_pass(self):
        self.ic._assets = {i: _Asset(i, "fresh") for i in self.ids}
        self.ic._links_at = time.time()
        self.ic.download("a0", "original", self.dest)
        self.assertEqual(self.lib.passes, 0)

    def test_item_gone_from_icloud_is_explained(self):
        self.ic._links_at = time.time() - ic_mod.LINK_MAX_AGE - 1
        self.lib.ids = self.ids[1:]                           # a0 was deleted in iCloud meanwhile
        self.ic._assets["a0"] = _Asset("a0", "stale")
        with self.assertRaises(ICloudError):
            self.ic.download("a0", "original", self.dest)

    def test_status_is_shown_while_refreshing(self):
        seen = []
        self.ic.on_status = seen.append
        self.ic._links_at = 0
        self.ic.download("a0", "original", self.dest)
        self.assertTrue(seen and "fresh download links" in seen[0])

    def test_ended_sign_in_is_reported_as_such(self):
        self.ic._links_at = 0
        self.ic.api.photos.all = mock.MagicMock()
        self.ic.api.photos.all.__iter__.side_effect = PyiCloudAuthRequiredException("a@b.c", mock.Mock())
        with self.assertRaises(ICloudSessionExpired):
            self.ic.download("a0", "original", self.dest)

    def test_gives_up_with_a_plain_message(self):
        self.ic.api.session.get.side_effect = PyiCloudEndpointGoneException("x")
        self.ic._links_at = 0
        with self.assertRaises(ICloudError) as cm:
            self.ic.download("a0", "original", self.dest)
        self.assertNotIsInstance(cm.exception, ICloudSessionExpired)
        self.assertIn("stays in iCloud", str(cm.exception))


class RecentRateTest(unittest.TestCase):
    def test_estimate_follows_recent_speed_not_the_overall_average(self):
        from app.engine import Progress
        p = Progress(started=time.time() - 3600)
        now = time.time()
        # fast for the first hour (5 GB), then slow: 10 MB over the last 5 minutes
        p.bytes_done = 5_000_000_000
        p.samples = [(now - 300, 5_000_000_000), (now, 5_010_000_000)]
        self.assertLess(p.recent_rate(), 100_000)            # ~33 KB/s, not ~1.4 MB/s


if __name__ == "__main__":
    unittest.main()
