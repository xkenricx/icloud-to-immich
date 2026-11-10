"""Engine tests with fake iCloud + Immich. Focus: nothing is ever deleted unless verified.

Run:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine import Engine, Scope  # noqa: E402
from app.icloud import Item, Part  # noqa: E402
from app.immich import ImmichError  # noqa: E402
from app.state import State  # noqa: E402


def item(i, *, live=False, edited=False, year=2020, albums=()):
    parts = [Part("original", f"IMG_{i}.HEIC", 10)]
    if live:
        parts.append(Part("live_video", f"IMG_{i}.MOV", 10))
    if edited:
        parts.append(Part("edited", f"IMG_{i}_edited.HEIC", 10))
    return Item(id=f"asset{i}", filename=f"IMG_{i}.HEIC", kind="image",
                taken=datetime(year, 1, 1, tzinfo=timezone.utc), parts=parts, albums=list(albums))


class FakeICloud:
    def __init__(self, items):
        self.items = items
        self.deleted = []
        self.corrupt = set()   # (item_id, role) whose download comes back short

    def scan(self, cb):
        cb(len(self.items), len(self.items))
        return list(self.items)

    def download(self, item_id, role, dest):
        data = hashlib.md5(f"{item_id}:{role}".encode()).hexdigest()[:10].encode()  # unique, 10 bytes
        if (item_id, role) in self.corrupt:
            data = data[:5]
        dest.write_bytes(data)
        return len(data), hashlib.sha1(data).hexdigest()

    def delete(self, item_id):
        self.deleted.append(item_id)


class FakeImmich:
    def __init__(self):
        self.assets = {}       # id -> {"sha1", "trashed", "live"}
        self.by_sha = {}
        self.stacks, self.albums_ = [], {}
        self.flip_checksum = set()  # filenames Immich stores with different bytes
        self.n = 0

    def upload(self, path, filename, taken, *, favorite=False, visibility=None, live_video_id=None):
        sha = hashlib.sha1(path.read_bytes()).hexdigest()
        if filename in self.flip_checksum:
            sha = "0" * 40
        if sha in self.by_sha:
            return self.by_sha[sha], "duplicate"
        self.n += 1
        aid = f"im{self.n}"
        self.assets[aid] = {"sha1": sha, "trashed": False, "live": live_video_id,
                            "visibility": visibility}
        self.by_sha[sha] = aid
        return aid, "created"

    def verify(self, aid, sha1):
        a = self.assets.get(aid)
        return bool(a) and a["sha1"] == sha1 and not a["trashed"]

    def check_present(self, items):
        out = {}
        for key, sha in items:
            aid = self.by_sha.get(sha)
            out[key] = aid if aid and not self.assets[aid]["trashed"] else None
        return out

    def link_live(self, image_id, video_id):
        self.assets[image_id]["live"] = video_id

    def stack(self, primary, other):
        self.stacks.append((primary, other))

    def albums(self):
        return dict(self.albums_)

    def create_album(self, name, ids):
        self.albums_[name] = list(ids)
        return name

    def add_to_album(self, album_id, ids):
        self.albums_[album_id].extend(ids)


def wait(engine):
    for _ in range(500):
        if not engine.busy:
            return
        time.sleep(0.01)
    raise AssertionError("engine job did not finish")


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State("a@b.c", "http://x", path=Path(self.tmp.name) / "s.sqlite")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def make(self, items):
        ic, im = FakeICloud(items), FakeImmich()
        eng = Engine(ic, im, self.state)
        eng.start_scan(); wait(eng)
        return eng, ic, im

    def run_transfer(self, eng, scope=Scope()):
        eng.start_transfer(scope); wait(eng)
        self.assertIn(eng.progress.phase, ("done",), eng.progress.log)

    def test_full_flow_live_edited_albums_and_delete(self):
        eng, ic, im = self.make([item(1), item(2, live=True), item(3, edited=True, albums=["Trip"])])
        self.run_transfer(eng)
        s = eng.summary(Scope())
        self.assertEqual(s["verified"], 3)
        # live photo: image linked to its video, video hidden
        img = next(a for a in im.assets.values() if a["live"])
        self.assertEqual(im.assets[img["live"]]["visibility"], "hidden")
        # edited: stacked with edited as primary, album uses the primary
        self.assertEqual(len(im.stacks), 1)
        self.assertEqual(im.albums_["Trip"], [im.stacks[0][0]])
        # delete everything verified
        self.assertEqual(len(eng.deletable(Scope())), 3)
        eng.start_delete(Scope(), 3); wait(eng)
        self.assertEqual(sorted(ic.deleted), ["asset1", "asset2", "asset3"])

    def test_checksum_mismatch_is_never_deleted(self):
        eng, ic, im = self.make([item(1), item(2)])
        im.flip_checksum.add("IMG_2.HEIC")
        self.run_transfer(eng)
        self.assertEqual([i.id for i in eng.deletable(Scope())], ["asset1"])
        eng.start_delete(Scope(), 1); wait(eng)
        self.assertEqual(ic.deleted, ["asset1"])

    def test_live_photo_with_bad_video_is_not_deletable(self):
        eng, ic, im = self.make([item(1, live=True)])
        ic.corrupt.add(("asset1", "live_video"))
        self.run_transfer(eng)
        self.assertEqual(eng.summary(Scope())["failed"], 1)
        self.assertEqual(eng.deletable(Scope()), [])

    def test_trashed_in_immich_after_verify_blocks_delete(self):
        eng, ic, im = self.make([item(1), item(2)])
        self.run_transfer(eng)
        trashed = im.by_sha[self.state.part("asset2", "original")["sha1"]]
        im.assets[trashed]["trashed"] = True
        eng.start_delete(Scope(), 2); wait(eng)
        self.assertEqual(ic.deleted, ["asset1"])
        self.assertEqual(self.state.status("asset2"), "pending")

    def test_delete_refuses_if_count_changed(self):
        eng, ic, im = self.make([item(1), item(2)])
        self.run_transfer(eng)
        with self.assertRaises(RuntimeError):
            eng.start_delete(Scope(), 5)
        self.assertEqual(ic.deleted, [])

    def test_scope_limits_transfer_and_delete(self):
        eng, ic, im = self.make([item(1, year=2015), item(2, year=2024)])
        old = Scope(mode="range", end="2019-12-31")
        self.run_transfer(eng, old)
        self.assertEqual(eng.summary(Scope())["verified"], 1)
        self.assertEqual([i.id for i in eng.deletable(Scope())], ["asset1"])

    def test_rerun_skips_verified_and_uploads_nothing_new(self):
        eng, ic, im = self.make([item(1), item(2, live=True)])
        self.run_transfer(eng)
        before = im.n
        self.run_transfer(eng)
        self.assertEqual(im.n, before)
        self.assertEqual(eng.progress.total, 0)

    def test_immich_error_marks_failed_and_continues(self):
        eng, ic, im = self.make([item(1), item(2)])
        real = im.upload

        def flaky(path, filename, *a, **k):
            if filename == "IMG_1.HEIC":
                raise ImmichError("boom")
            return real(path, filename, *a, **k)
        im.upload = flaky
        self.run_transfer(eng)
        self.assertEqual(self.state.status("asset1"), "failed")
        self.assertEqual(self.state.status("asset2"), "verified")


if __name__ == "__main__":
    unittest.main()


class InterruptionTest(unittest.TestCase):
    """A real run once stopped dead at item 979 on one unexpected error."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State("a@b.c", "http://x", path=Path(self.tmp.name) / "s.sqlite")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def make(self, n):
        ic, im = FakeICloud([item(i) for i in range(n)]), FakeImmich()
        eng = Engine(ic, im, self.state)
        eng.start_scan(); wait(eng)
        return eng, ic, im

    def test_unexpected_error_on_one_item_does_not_stop_the_copy(self):
        eng, ic, im = self.make(5)
        real = ic.download

        def boom(item_id, role, dest):
            if item_id == "asset2":
                raise KeyError("something odd")       # not an ICloudError
            return real(item_id, role, dest)
        ic.download = boom
        eng.start_transfer(Scope()); wait(eng)
        self.assertEqual(eng.progress.phase, "done")
        self.assertEqual(self.state.status("asset2"), "failed")
        self.assertEqual(sum(self.state.status(f"asset{i}") == "verified" for i in range(5)), 4)

    def test_expired_sign_in_stops_cleanly_without_failing_the_rest(self):
        from app.icloud import ICloudSessionExpired
        eng, ic, im = self.make(6)
        real = ic.download

        def expire(item_id, role, dest):
            if item_id == "asset3":
                raise ICloudSessionExpired()
            return real(item_id, role, dest)
        ic.download = expire
        eng.start_transfer(Scope()); wait(eng)
        self.assertEqual(eng.progress.phase, "error")
        self.assertIn("sign in again", eng.progress.message.lower())
        self.assertTrue(eng.session_expired)
        statuses = [self.state.status(f"asset{i}") for i in range(6)]
        self.assertEqual(statuses[:3], ["verified"] * 3)
        self.assertNotIn("failed", statuses)          # the rest are just still pending

    def test_many_failures_in_a_row_pause_instead_of_failing_everything(self):
        from app.engine import MAX_FAILURES_IN_A_ROW
        eng, ic, im = self.make(MAX_FAILURES_IN_A_ROW + 15)
        ic.download = lambda *a: (_ for _ in ()).throw(OSError("network down"))
        eng.start_transfer(Scope()); wait(eng)
        self.assertEqual(eng.progress.phase, "error")
        self.assertIn("in a row", eng.progress.message)
        failed = sum(s == "failed" for s in self.state.statuses().values())
        self.assertEqual(failed, MAX_FAILURES_IN_A_ROW)   # stopped, didn't burn through all 25
