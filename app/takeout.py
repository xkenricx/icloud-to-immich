"""Google Photos source: reads a Google Takeout export (zips, or an unzipped folder).

Same interface the engine uses for iCloud: scan() -> [Item], download() streams one
file and returns (bytes, sha1). Google offers no way for apps to delete photos, so
can_delete is False and the wizard guides the user through deleting by hand.

Format notes and quirks: docs/google-photos.md.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from .icloud import ICloudError, Item, Part

CHUNK = 1 << 20

IMAGE_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".webp", ".tif", ".tiff",
             ".bmp", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".raf", ".rw2", ".avif"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".3gp", ".avi", ".mkv", ".webm", ".mts", ".m2ts", ".mp"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT

# Google's "-edited" suffix is translated into the account's language.
EDITED_SUFFIXES = ("-edited", "-bearbeitet", "-modifié", "-modifie", "-editado", "-modificato",
                   "-bewerkt", "-redigerad", "-redigeret", "-redigert", "-muokattu", "-edytowane",
                   "-upraveno", "-szerkesztett", "-düzenlendi", "-編集済み", "-已修改", "-편집됨")

SIDECAR_TAIL = ".supplemental-metadata"
DUP_JSON = re.compile(r"^(?P<base>.*?)\((?P<n>\d+)\)\.json$")    # IMG.jpg(1).json
PHOTOS_ROOT = "Google Photos/"


class TakeoutError(ICloudError):
    """Safe to show the user as-is (reuses the engine's handling for source errors)."""


@dataclass
class _Entry:
    container: Path | None     # zip file, or None for an unzipped file on disk
    member: str                # path inside the zip, or the file path on disk
    size: int
    mtime: datetime


@dataclass
class _Media:
    folder: str
    name: str
    entry: _Entry
    used: bool = False


@dataclass
class ScanReport:
    zips: int = 0
    items: int = 0
    trashed_skipped: int = 0
    no_info_file: int = 0
    album_copies_merged: int = 0
    live_pairs: int = 0
    edited: int = 0
    albums: list = field(default_factory=list)


def _stem_ext(name: str) -> tuple[str, str]:
    p = PurePosixPath(name)
    return p.stem, p.suffix


def _is_media(name: str) -> bool:
    return PurePosixPath(name).suffix.lower() in MEDIA_EXT


def _sidecar_title_key(json_name: str) -> str:
    """'IMG_1.jpg.supplemental-metad.json' -> 'IMG_1.jpg' (also old and dup styles)."""
    base = json_name[:-5] if json_name.lower().endswith(".json") else json_name
    m = re.match(r"^(.*?)(\(\d+\))$", base)
    if m:
        base = m.group(1)
    # strip any (truncated) ".supplemental-metadata" tail
    for cut in range(len(SIDECAR_TAIL), 1, -1):
        if base.lower().endswith(SIDECAR_TAIL[:cut]):
            return base[: -cut]
    return base


class TakeoutSource:
    can_delete = False
    kind = "google"

    def __init__(self, folder: str | Path) -> None:
        self.folder = Path(folder).expanduser()
        self.apple_id = None           # the engine/state use this as the account label
        self.report = ScanReport()
        self._entries: dict[str, _Entry] = {}      # "folder/name" under Google Photos/
        self._parts: dict[tuple[str, str], _Entry] = {}
        self._zips: dict[Path, zipfile.ZipFile] = {}

    # ---------- discovery ----------
    def check(self) -> dict:
        """Find the export; raises with a plain message if it isn't one."""
        if not self.folder.is_dir():
            raise TakeoutError(f"Can't find the folder {self.folder}. Check the path.")
        self._index()
        if not self._entries:
            raise TakeoutError("No Google Photos export found here. Put the Takeout .zip files "
                               "(takeout-…-001.zip, -002.zip, …) in this folder, all parts together.")
        return {"zips": self.report.zips, "files": sum(_is_media(k) for k in self._entries)}

    def _add(self, path_in_export: str, entry: _Entry) -> None:
        i = path_in_export.find(PHOTOS_ROOT)
        if i < 0:
            return
        rel = path_in_export[i + len(PHOTOS_ROOT):]
        if rel.count("/") != 1 or rel.endswith("/"):
            return                                   # only <folder>/<file>
        self._entries[rel] = entry

    def _index(self) -> None:
        self._entries.clear()
        zips = sorted(p for p in self.folder.iterdir() if p.suffix.lower() == ".zip")
        self.report.zips = len(zips)
        for z in zips:
            try:
                zf = zipfile.ZipFile(z)
            except zipfile.BadZipFile as e:
                raise TakeoutError(f"{z.name} isn't a complete zip file. It may still be "
                                   "downloading, or the download was cut off: download that "
                                   "part again.") from e
            self._zips[z] = zf
            for info in zf.infolist():
                if info.is_dir():
                    continue
                self._add(info.filename.replace("\\", "/"),
                          _Entry(z, info.filename, info.file_size, datetime(*info.date_time, tzinfo=timezone.utc)))
        # an already-unzipped export works too
        for f in self.folder.rglob("*"):
            if f.is_file() and f.suffix.lower() != ".zip":
                st = f.stat()
                self._add(f.relative_to(self.folder).as_posix(),
                          _Entry(None, str(f), st.st_size, datetime.fromtimestamp(st.st_mtime, timezone.utc)))

    def _read_json(self, entry: _Entry) -> dict | None:
        try:
            if entry.container is None:
                raw = Path(entry.member).read_bytes()
            else:
                raw = self._zips[entry.container].read(entry.member)
            data = json.loads(raw.decode("utf-8-sig"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError, KeyError):
            return None

    # ---------- scan ----------
    def scan(self, progress: Callable[[int, int], None]) -> list[Item]:
        if not self._entries:
            self._index()
        rep = self.report = ScanReport(zips=self.report.zips)
        folders: dict[str, dict[str, _Entry]] = {}
        for rel, e in self._entries.items():
            folder, name = rel.split("/", 1)
            folders.setdefault(folder, {})[name] = e

        merged: dict[tuple, Item] = {}
        total = len(folders)
        for n, (folder, files) in enumerate(sorted(folders.items()), 1):
            progress(n, total)
            album = None
            meta = files.get("metadata.json")
            if meta is not None:
                album = ((self._read_json(meta) or {}).get("title") or folder).strip() or folder
                rep.albums.append(album)
            media = {name: _Media(folder, name, e) for name, e in files.items() if _is_media(name)}
            sidecars = [(name, e) for name, e in files.items()
                        if name.lower().endswith(".json") and name != "metadata.json"]
            for item in self._folder_items(folder, media, sidecars):
                key = (item.info.get("title", item.filename).lower(),
                       int(item.taken.timestamp()), item.parts[0].size)
                if key in merged:                       # same photo in a year folder and an album
                    rep.album_copies_merged += 1
                    target = merged[key]
                    # Keep every file either copy found (the edit or Live video may only
                    # sit next to one of them). Same id, so downloads already resolve.
                    have = {p.role for p in target.parts}
                    target.parts += [p for p in item.parts if p.role not in have]
                    target.favorite = target.favorite or item.favorite
                    target.hidden = target.hidden or item.hidden
                    for k, v in item.info.items():
                        target.info.setdefault(k, v)
                else:
                    target = merged[key] = item
                if album and album not in target.albums:
                    target.albums.append(album)
        items = sorted(merged.values(), key=lambda i: i.taken)
        rep.items = len(items)
        return items

    def _folder_items(self, folder: str, media: dict[str, _Media], sidecars) -> list[Item]:
        rep = self.report
        out: list[Item] = []
        # Photos before videos, so a Live Photo's image claims its .MOV before the
        # .MOV's own sidecar (if any) turns it into a separate video.
        def photo_first(s):
            return (PurePosixPath(_sidecar_title_key(s[0])).suffix.lower() in VIDEO_EXT, s[0])
        for json_name, entry in sorted(sidecars, key=photo_first):
            data = self._read_json(entry)
            if not data or "title" not in data:
                continue
            m = self._match(folder, json_name, data["title"], media)
            if m is None:
                continue
            if data.get("trashed"):
                m.used = True
                rep.trashed_skipped += 1
                continue
            out.append(self._make_item(m, data, media))
        # files with no sidecar at all: still copied, never silently dropped
        for m in media.values():
            if m.used:
                continue
            m.used = True
            rep.no_info_file += 1
            it = self._make_item(m, None, media)
            it.info["no_info_file"] = True
            out.append(it)
        return out

    def _match(self, folder: str, json_name: str, title: str, media: dict[str, _Media]) -> _Media | None:
        stem, ext = _stem_ext(title)
        dup = DUP_JSON.match(json_name)
        want = f"{stem}({dup.group('n')}){ext}" if dup else title
        hit = media.get(want)
        if hit and not hit.used:
            hit.used = True
            return hit
        # Long names are cut short in the export (around 47 characters): match a
        # truncated media name. Only names that long qualify, so a short
        # "IMG_1.jpg" can never be mistaken for "IMG_1 copy.jpg".
        wstem, wext = _stem_ext(want)
        for name, m in media.items():
            if m.used or len(name) < 40:
                continue
            s, e = _stem_ext(name)
            if e.lower() == wext.lower() and wstem.startswith(s) and not _edited_base(name):
                m.used = True
                return m
        return None

    def _make_item(self, m: _Media, data: dict | None, media: dict[str, _Media]) -> Item:
        rep = self.report
        stem, ext = _stem_ext(m.name)
        parts = [Part("original", m.name, m.entry.size)]
        self._parts[(self._id_hint(m, data), "original")] = m.entry

        # edited copy: IMG_1-edited.jpg next to IMG_1.jpg
        for suffix in EDITED_SUFFIXES:
            ed = media.get(f"{stem}{suffix}{ext}")
            if ed and not ed.used:
                ed.used = True
                parts.append(Part("edited", ed.name, ed.entry.size))
                self._parts[(self._id_hint(m, data), "edited")] = ed.entry
                rep.edited += 1
                break
        # iPhone Live Photo uploaded to Google: IMG_1.HEIC + IMG_1.MOV
        if ext.lower() in IMAGE_EXT:
            for name, v in media.items():
                vs, ve = _stem_ext(name)
                if vs == stem and ve.lower() in VIDEO_EXT and not v.used:
                    v.used = True
                    parts.append(Part("live_video", v.name, v.entry.size))
                    self._parts[(self._id_hint(m, data), "live_video")] = v.entry
                    rep.live_pairs += 1
                    break

        taken = m.entry.mtime
        info: dict = {"title": (data or {}).get("title") or m.name, "folder": m.folder}
        if data:
            ts = (data.get("photoTakenTime") or {}).get("timestamp") \
                or (data.get("creationTime") or {}).get("timestamp")
            if ts:
                try:
                    taken = datetime.fromtimestamp(int(ts), timezone.utc)
                except (ValueError, OverflowError, OSError):
                    pass
            if data.get("description"):
                info["description"] = data["description"]
            geo = data.get("geoData") or {}
            if geo.get("latitude") or geo.get("longitude"):
                info["latitude"], info["longitude"] = geo.get("latitude"), geo.get("longitude")
        kind = "video" if ext.lower() in VIDEO_EXT else "image"
        item = Item(id=self._id_hint(m, data), filename=m.name, kind=kind, taken=taken, parts=parts,
                    favorite=bool((data or {}).get("favorited")),
                    hidden=bool((data or {}).get("archived")), info=info)
        return item

    def _id_hint(self, m: _Media, data: dict | None) -> str:
        """Stable across re-exports: original name + date + size, not which zip it was in."""
        title = (data or {}).get("title") or m.name
        ts = ((data or {}).get("photoTakenTime") or {}).get("timestamp") or ""
        return "g" + hashlib.sha1(f"{title}|{ts}|{m.entry.size}".encode()).hexdigest()[:23]

    # ---------- download ----------
    def download(self, item_id: str, role: str, dest: Path) -> tuple[int, str]:
        entry = self._parts.get((item_id, role))
        if entry is None:
            raise TakeoutError("This file isn't in the export any more. Was a zip moved or deleted?")
        h, n = hashlib.sha1(), 0
        try:
            if entry.container is None:
                src = open(entry.member, "rb")
            else:
                src = self._zips[entry.container].open(entry.member)
            with src, dest.open("wb") as out:
                while chunk := src.read(CHUNK):
                    out.write(chunk)
                    h.update(chunk)
                    n += len(chunk)
        except (OSError, zipfile.BadZipFile, KeyError) as e:
            raise TakeoutError(f"Couldn't read this file from the export ({e}). The zip may be "
                               "damaged: download that part again.") from e
        return n, h.hexdigest()

    def delete(self, item_id: str) -> None:
        raise TakeoutError("Google doesn't let apps delete photos. Delete them in Google Photos instead.")

    def close(self) -> None:
        for zf in self._zips.values():
            zf.close()
        self._zips.clear()


def _edited_base(name: str) -> bool:
    stem, _ = _stem_ext(name)
    return any(stem.endswith(s) for s in EDITED_SUFFIXES)
