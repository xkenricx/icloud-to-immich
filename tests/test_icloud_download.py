"""Apple's download links expire mid-run; pyicloud raises before we see the 410."""

from __future__ import annotations

import sys
import tempfile
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
    def __init__(self, url):
        self.url = url

    def download_url(self, version):
        return self.url


class DownloadRetryTest(unittest.TestCase):
    def setUp(self):
        self.ic = ICloud()
        self.ic.api = mock.MagicMock()
        self.ic._assets = {"a1": _Asset("https://cvws.example/old-link/${f}")}
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "f"
        mock.patch.object(ic_mod.time, "sleep", lambda s: None).start()

    def tearDown(self):
        mock.patch.stopall()
        self.ic.close()
        self.tmp.cleanup()

    def test_expired_link_is_refreshed_and_retried(self):
        def get(url, **kw):
            if "old-link" in url:
                raise PyiCloudEndpointGoneException("cvws.example/old-link")   # what Apple sent
            return _Resp(b"photo-bytes")
        self.ic.api.session.get.side_effect = get
        self.ic.api.photos.all.get.return_value = _Asset("https://cvws.example/new-link/${f}")
        n, sha1 = self.ic.download("a1", "original", self.dest)
        self.assertEqual(n, len(b"photo-bytes"))
        self.assertEqual(self.dest.read_bytes(), b"photo-bytes")
        self.ic.api.photos.all.get.assert_called_once_with("a1")

    def test_gives_up_with_a_plain_message_after_a_few_tries(self):
        self.ic.api.session.get.side_effect = PyiCloudEndpointGoneException("x")
        self.ic.api.photos.all.get.return_value = _Asset("https://cvws.example/still-bad")
        with self.assertRaises(ICloudError) as cm:
            self.ic.download("a1", "original", self.dest)
        self.assertNotIsInstance(cm.exception, ICloudSessionExpired)
        self.assertIn("stays in iCloud", str(cm.exception))
        self.assertEqual(self.ic.api.session.get.call_count, ic_mod.DOWNLOAD_TRIES)

    def test_ended_sign_in_is_reported_as_such(self):
        self.ic.api.session.get.side_effect = PyiCloudEndpointGoneException("x")
        self.ic.api.photos.all.get.side_effect = PyiCloudAuthRequiredException("a@b.c", mock.Mock())
        with self.assertRaises(ICloudSessionExpired):
            self.ic.download("a1", "original", self.dest)


if __name__ == "__main__":
    unittest.main()
