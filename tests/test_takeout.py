"""Google Takeout reader, against fake exports built to Google's documented format."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.takeout import TakeoutError, TakeoutSource  # noqa: E402

ROOT = "Takeout/Google Photos/"


def side(title, ts, **extra):
    d = {"title": title, "description": extra.pop("description", ""),
         "photoTakenTime": {"timestamp": str(ts), "formatted": "x"},
         "creationTime": {"timestamp": str(ts + 99), "formatted": "x"},
         "geoData": {"latitude": extra.pop("lat", 0.0), "longitude": extra.pop("lon", 0.0)}}
    d.update(extra)
    return json.dumps(d).encode()


def body(name):        # unique bytes per file so checksums are meaningful
    return hashlib.sha256(name.encode()).digest() * 8


LONG = "Screenshot_20240512-101112_Some Really Long App Name Here.jpg"   # 61 chars
LONG_CUT = LONG[:43] + ".jpg"                                             # what Takeout writes


def build(folder: Path, split=True):
    """Two zips; a sidecar and its photo may land in different parts."""
    a = {
        ROOT + "Photos from 2024/IMG_0001.HEIC": body("1"),
        ROOT + "Photos from 2024/IMG_0001.HEIC.supplemental-metadata.json":
            side("IMG_0001.HEIC", 1714000000, favorited=True, lat=47.6, lon=-122.3),
        ROOT + "Photos from 2024/IMG_0001.MOV": body("1v"),                       # iPhone Live Photo
        ROOT + "Photos from 2024/IMG_0002.jpg.json": side("IMG_0002.jpg", 1714100000),  # pre-2024 naming
        ROOT + "Photos from 2024/IMG_0003.jpg": body("3"),
        ROOT + "Photos from 2024/IMG_0003-edited.jpg": body("3e"),
        ROOT + "Photos from 2024/IMG_0003.jpg.supplemental-metadata.json":
            side("IMG_0003.jpg", 1714200000, description="Beach day"),
        ROOT + "Photos from 2024/IMG_0004(1).jpg": body("4dup"),                 # second IMG_0004
        ROOT + "Photos from 2024/IMG_0004.jpg.supplemental-metadata(1).json":
            side("IMG_0004.jpg", 1714300000),
        ROOT + "Photos from 2024/IMG_0004.jpg": body("4"),
        ROOT + "Photos from 2024/IMG_0004.jpg.supplemental-metadata.json": side("IMG_0004.jpg", 1714290000),
        ROOT + f"Photos from 2024/{LONG_CUT}": body("long"),
        ROOT + f"Photos from 2024/{LONG[:40]}.supp.json": side(LONG, 1714400000),  # truncated sidecar
        ROOT + "Photos from 2024/deleted.jpg": body("trash"),
        ROOT + "Photos from 2024/deleted.jpg.supplemental-metadata.json": side("deleted.jpg", 1714500000, trashed=True),
        ROOT + "Photos from 2024/old.jpg": body("arch"),
        ROOT + "Photos from 2024/old.jpg.supplemental-metadata.json": side("old.jpg", 1600000000, archived=True),
        ROOT + "Photos from 2024/orphan.png": body("orphan"),                     # no sidecar
        ROOT + "Photos from 2024/video.mp4": body("vid"),
        ROOT + "Photos from 2024/video.mp4.supplemental-metadata.json": side("video.mp4", 1714600000),
    }
    b = {
        ROOT + "Photos from 2024/IMG_0002.jpg": body("2"),                       # split from its sidecar
        ROOT + "Beach Trip/metadata.json": json.dumps({"title": "Beach Trip"}).encode(),
        ROOT + "Beach Trip/IMG_0003.jpg": body("3"),                             # album copy
        ROOT + "Beach Trip/IMG_0003.jpg.supplemental-metadata.json":
            side("IMG_0003.jpg", 1714200000, description="Beach day"),
        "Takeout/archive_browser.html": b"<html></html>",
    }
    parts = [a, b] if split else [{**a, **b}]
    for i, files in enumerate(parts, 1):
        with zipfile.ZipFile(folder / f"takeout-20261110T000000Z-{i:03d}.zip", "w") as zf:
            for name, data in files.items():
                zf.writestr(name, data)


class TakeoutReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        build(self.dir)
        self.src = TakeoutSource(self.dir)
        self.src.check()
        self.items = {i.info["title"]: i for i in self.src.scan(lambda n, t: None)}

    def tearDown(self):
        self.src.close()
        self.tmp.cleanup()

    def roles(self, title):
        return sorted(p.role for p in self.items[title].parts)

    def test_finds_everything_except_trash(self):
        self.assertEqual(sorted(self.items), sorted([
            "IMG_0001.HEIC", "IMG_0002.jpg", "IMG_0003.jpg", "IMG_0004.jpg", LONG,
            "old.jpg", "orphan.png", "video.mp4"]) + [])
        # IMG_0004 exists twice (two different photos with the same name)
        both = [i for i in self.src.scan(lambda n, t: None) if i.info["title"] == "IMG_0004.jpg"]
        self.assertEqual(len(both), 2)
        self.assertEqual(self.src.report.trashed_skipped, 1)

    def test_dates_and_details_come_from_the_info_files(self):
        one = self.items["IMG_0001.HEIC"]
        self.assertEqual(int(one.taken.timestamp()), 1714000000)
        self.assertTrue(one.favorite)
        self.assertAlmostEqual(one.info["latitude"], 47.6)
        self.assertEqual(self.items["IMG_0003.jpg"].info["description"], "Beach day")
        self.assertTrue(self.items["old.jpg"].hidden)          # archived -> Immich Archive

    def test_quirks(self):
        self.assertEqual(self.roles("IMG_0001.HEIC"), ["live_video", "original"])   # Live Photo pair
        self.assertEqual(self.roles("IMG_0003.jpg"), ["edited", "original"])         # -edited stacked
        self.assertEqual(self.items[LONG].filename, LONG_CUT)                         # truncated names
        self.assertEqual(int(self.items["IMG_0002.jpg"].taken.timestamp()), 1714100000)  # old naming, split zips
        self.assertTrue(self.items["orphan.png"].info.get("no_info_file"))           # kept, flagged
        self.assertEqual(self.items["video.mp4"].kind, "video")

    def test_album_copy_is_merged_into_one_item_in_that_album(self):
        three = [i for i in self.src.scan(lambda n, t: None) if i.info["title"] == "IMG_0003.jpg"]
        self.assertEqual(len(three), 1)
        self.assertEqual(three[0].albums, ["Beach Trip"])
        self.assertEqual(self.src.report.album_copies_merged, 1)

    def test_download_streams_exact_bytes_out_of_the_zip(self):
        it = self.items["IMG_0003.jpg"]
        dest = self.dir / "out.bin"
        for role, want in (("original", body("3")), ("edited", body("3e"))):
            n, sha1 = self.src.download(it.id, role, dest)
            self.assertEqual(dest.read_bytes(), want)
            self.assertEqual(sha1, hashlib.sha1(want).hexdigest())

    def test_ids_are_stable_across_a_new_export(self):
        ids = {t: i.id for t, i in self.items.items()}
        with tempfile.TemporaryDirectory() as d2:
            build(Path(d2), split=False)                       # re-exported into a single zip
            s2 = TakeoutSource(d2)
            again = {i.info["title"]: i.id for i in s2.scan(lambda n, t: None)}
            s2.close()
        self.assertEqual(again, ids)

    def test_unzipped_export_works_too(self):
        with tempfile.TemporaryDirectory() as d2:
            build(Path(d2), split=False)
            z = next(Path(d2).glob("*.zip"))
            with zipfile.ZipFile(z) as zf:
                zf.extractall(d2)
            z.unlink()
            s2 = TakeoutSource(d2)
            s2.check()
            titles = {i.info["title"] for i in s2.scan(lambda n, t: None)}
            s2.close()
        self.assertEqual(titles, set(self.items))


class TakeoutProblemsTest(unittest.TestCase):
    def test_empty_folder_says_what_to_do(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(TakeoutError, "Takeout .zip files"):
                TakeoutSource(d).check()

    def test_half_downloaded_zip_is_explained(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "takeout-001.zip").write_bytes(b"PK\x03\x04 not really a zip")
            with self.assertRaisesRegex(TakeoutError, "download that part again"):
                TakeoutSource(d).check()

    def test_deleting_is_refused_politely(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(TakeoutSource(d).can_delete)
            with self.assertRaisesRegex(TakeoutError, "Google Photos"):
                TakeoutSource(d).delete("x")


if __name__ == "__main__":
    unittest.main()


class TakeoutThroughEngineTest(unittest.TestCase):
    """A fake Takeout copied end to end by the real engine into a fake Immich."""

    def test_copy_verify_albums_and_no_delete(self):
        from app.engine import Engine, Scope
        from app.state import State
        from test_engine import FakeImmich, wait
        with tempfile.TemporaryDirectory() as d:
            build(Path(d))
            src = TakeoutSource(d)
            state = State("google", "http://x", path=Path(d) / "s.sqlite")
            try:
                im = FakeImmich()
                eng = Engine(src, im, state)
                eng.start_scan(); wait(eng)
                eng.start_transfer(Scope()); wait(eng)
                self.assertEqual(eng.progress.phase, "done", eng.progress.log)
                s = eng.summary(Scope())
                # 9 items: 8 names, and IMG_0004.jpg is two different photos
                self.assertEqual((s["verified"], s["failed"], s["pending"]), (9, 0, 0))
                self.assertEqual(len(im.stacks), 1)                       # the -edited photo
                self.assertIn("Beach Trip", im.albums_)
                self.assertTrue(any(a["live"] for a in im.assets.values()))  # Live Photo linked
                self.assertEqual(eng.deletable(Scope()), [])              # Google: never deletes
            finally:
                state.close(); src.close()
