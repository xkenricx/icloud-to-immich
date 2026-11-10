"""iCloud Photos side: sign-in with 2FA, library listing, streaming download, delete.

Wraps pyicloud (unofficial iCloud web API). Session cookies live in a per-run
temp folder that the app deletes when the user finishes; the password is held
only in memory for the sign-in call and never written anywhere.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from pyicloud import PyiCloudService
from pyicloud.exceptions import (
    PyiCloudAccountLockedException,
    PyiCloudAPIResponseException,
    PyiCloudFailedLoginException,
    PyiCloudServiceNotActivatedException,
)
from pyicloud.services.photos_cloudkit.mappers import build_photo_resource, record_field_value
from pyicloud.services.photos_cloudkit.service import PhotoAsset

# Smart albums Apple builds automatically; Immich has its own equivalents.
SMART_ALBUMS = {
    "Library", "Videos", "Live", "Recently Deleted", "Hidden", "Favorites", "Bursts",
    "Panoramas", "Screenshots", "Slo-mo", "Time-lapse", "Selfies", "Portrait",
    "Long Exposure", "Animated", "Screen Recordings", "RAW", "Spatial", "Recently Saved",
    "Recents", "Imports", "Duplicates",
}

CHUNK = 1 << 20


class ICloudError(Exception):
    """Raised with a message that is safe to show the user as-is."""


@dataclass
class Part:
    """One file belonging to an iCloud item."""
    role: str            # "original" | "live_video" | "edited"
    filename: str
    size: int | None


@dataclass
class Item:
    id: str
    filename: str
    kind: str            # "image" | "video"
    taken: datetime
    parts: list[Part]
    favorite: bool = False
    hidden: bool = False
    albums: list[str] = field(default_factory=list)

    @property
    def total_size(self) -> int:
        return sum(p.size or 0 for p in self.parts)


def _edited_resource(asset: PhotoAsset):
    for prefix in ("resJPEGFull", "resVidFull"):
        res = build_photo_resource(
            key="edited", prefix=prefix, master_record=asset.asset_record,
            filename=asset.filename, item_type_extensions=PhotoAsset.FILE_TYPE_EXTENSIONS,
            is_live_photo=False, item_type_lookup=PhotoAsset.ITEM_TYPES)
        if res is not None and res.url:
            return res
    return None


def _flag(asset: PhotoAsset, name: str) -> bool:
    try:
        return record_field_value(asset.asset_record, name) == 1
    except Exception:  # field shapes vary between record kinds
        return False


class ICloud:
    def __init__(self) -> None:
        self._dir = Path(tempfile.mkdtemp(prefix="icloud2immich-"))
        self.api: PyiCloudService | None = None
        self.apple_id: str | None = None
        self._assets: dict[str, PhotoAsset] = {}

    # ---------- sign-in ----------
    def sign_in(self, apple_id: str, password: str) -> str:
        """Returns "ok" or "needs_2fa"."""
        try:
            self.api = PyiCloudService(apple_id, password, cookie_directory=str(self._dir))
        except PyiCloudAccountLockedException as e:
            raise ICloudError("Apple says this account is locked. Unlock it at iforgot.apple.com, then try again.") from e
        except PyiCloudFailedLoginException as e:
            raise ICloudError("Apple ID or password is wrong.") from e
        except PyiCloudAPIResponseException as e:
            raise ICloudError(f"Apple refused the sign-in: {e}") from e
        self.apple_id = apple_id
        if self.api.requires_2sa and not self.api.requires_2fa:
            raise ICloudError("This account uses Apple's old two-step verification, which isn't supported. "
                              "Upgrade to two-factor authentication in your Apple ID settings.")
        if self.api.requires_2fa:
            if not self.api.request_2fa_code():
                raise ICloudError("This account requires a hardware security key, which isn't supported yet.")
            return "needs_2fa"
        return "ok"

    def delivery_method(self) -> str:
        return getattr(self.api, "two_factor_delivery_method", "unknown") if self.api else "unknown"

    def verify_code(self, code: str) -> bool:
        assert self.api is not None
        if not self.api.validate_2fa_code(code.strip().replace(" ", "")):
            return False
        if not self.api.is_trusted_session:
            self.api.trust_session()
        return True

    def check_photos_access(self) -> int:
        """Touch the Photos service; returns the library size."""
        assert self.api is not None
        try:
            return len(self.api.photos.all)
        except PyiCloudServiceNotActivatedException as e:
            raise ICloudError(
                "iCloud Photos can't be reached from the web for this account. If Advanced Data "
                "Protection is on, turn on 'Access iCloud Data on the Web' in Settings > [your name] "
                "> iCloud on your iPhone, then sign in again.") from e
        except PyiCloudAPIResponseException as e:
            raise ICloudError(f"Couldn't open iCloud Photos: {e}") from e

    # ---------- listing ----------
    def scan(self, progress: Callable[[int, int], None]) -> list[Item]:
        assert self.api is not None
        photos = self.api.photos
        total = len(photos.all)
        items: dict[str, Item] = {}
        self._assets = {}

        def add(asset: PhotoAsset, hidden: bool) -> None:
            if asset.id in items:
                items[asset.id].hidden = items[asset.id].hidden or hidden
                return
            parts = []
            orig = asset.resources.get("original")
            if orig is None or not orig.url:
                return
            parts.append(Part("original", orig.filename, orig.size))
            if asset.is_live_photo:
                vid = asset.resources.get("original_video")
                if vid is not None and vid.url:
                    parts.append(Part("live_video", vid.filename, vid.size))
            edited = _edited_resource(asset)
            if edited is not None:
                stem = Path(orig.filename).stem
                parts.append(Part("edited", f"{stem}_edited{Path(edited.filename).suffix}", edited.size))
            items[asset.id] = Item(
                id=asset.id, filename=asset.filename,
                kind="video" if asset.item_type == "movie" else "image",
                taken=asset.asset_date, parts=parts,
                favorite=_flag(asset, "isFavorite"), hidden=hidden or _flag(asset, "isHidden"))
            self._assets[asset.id] = asset

        for n, asset in enumerate(photos.all, 1):
            add(asset, hidden=False)
            if n % 25 == 0:
                progress(n, total)
        albums = photos.albums
        if "Hidden" in albums:
            for asset in albums["Hidden"]:
                add(asset, hidden=True)
        progress(total, total)

        for album in albums:
            if album.name in SMART_ALBUMS:
                continue
            name = album.name.strip() or "Untitled album"
            for asset in album:
                if asset.id in items and name not in items[asset.id].albums:
                    items[asset.id].albums.append(name)
        return sorted(items.values(), key=lambda i: i.taken)

    # ---------- download / delete ----------
    def _url(self, item_id: str, role: str) -> str | None:
        asset = self._assets[item_id]
        if role == "original":
            return asset.download_url("original")
        if role == "live_video":
            return asset.download_url("original_video")
        res = _edited_resource(asset)
        return res.url if res else None

    def _refresh(self, item_id: str) -> None:
        assert self.api is not None
        fresh = self.api.photos.all.get(item_id)
        if fresh is None:
            raise ICloudError("This item is no longer in iCloud.")
        self._assets[item_id] = fresh

    def download(self, item_id: str, role: str, dest: Path) -> tuple[int, str]:
        """Stream one part to dest; returns (bytes, sha1 hex)."""
        assert self.api is not None
        for attempt in (1, 2):
            url = self._url(item_id, role)
            if not url:
                raise ICloudError(f"No download link for the {role.replace('_', ' ')}.")
            resp = self.api.session.get(url, stream=True, timeout=120)
            if resp.status_code in (401, 403, 404, 410) and attempt == 1:
                resp.close()
                self._refresh(item_id)   # signed download links expire
                continue
            if resp.status_code != 200:
                raise ICloudError(f"iCloud download failed (HTTP {resp.status_code}).")
            h, n = hashlib.sha1(), 0
            with resp, dest.open("wb") as fh:
                for chunk in resp.iter_content(CHUNK):
                    fh.write(chunk)
                    h.update(chunk)
                    n += len(chunk)
            return n, h.hexdigest()
        raise ICloudError("iCloud download link kept expiring.")

    def delete(self, item_id: str) -> None:
        for attempt in (1, 2):
            try:
                self._assets[item_id].delete()
                return
            except PyiCloudAPIResponseException:
                if attempt == 2:
                    raise
                self._refresh(item_id)  # stale change tag; reload and retry once

    def close(self) -> None:
        """Forget the session: delete cookies and drop references."""
        self.api = None
        self._assets = {}
        shutil.rmtree(self._dir, ignore_errors=True)
