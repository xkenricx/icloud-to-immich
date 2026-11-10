"""iCloud Photos side: sign-in with 2FA, library listing, streaming download, delete.

Wraps pyicloud (unofficial iCloud web API). Session cookies live in a per-run
temp folder that the app deletes when the user finishes; the password is held
only in memory for the sign-in call and never written anywhere.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

import requests
from pyicloud import PyiCloudService
from pyicloud.exceptions import (
    PyiCloud2FARequiredException,
    PyiCloudAccountLockedException,
    PyiCloudAPIResponseException,
    PyiCloudAuthRequiredException,
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


class ICloudSessionExpired(ICloudError):
    """Apple ended the sign-in. Stop and ask the user to sign in again, rather
    than marking every remaining item as failed."""

    def __init__(self) -> None:
        super().__init__("Your iCloud sign-in has expired. Sign in again in step 2, then click "
                         "Continue. Nothing that's already copied will be copied again.")


_AUTH_ERRORS = (PyiCloudAuthRequiredException, PyiCloud2FARequiredException)
DOWNLOAD_TRIES = 3
# Apple's signed download links stop working after about an hour. Re-read the
# whole library (one pass: ~1 minute per 2,000 items) a little before that,
# instead of fixing links one at a time: pyicloud's single-photo lookup misses
# and falls back to paging through the entire library, i.e. a full pass *per item*.
LINK_MAX_AGE = 40 * 60


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
    # Source-specific extras (Google Takeout: description, GPS, "no info file").
    info: dict = field(default_factory=dict)

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
        self._links_at = 0.0                     # when the download links were last fetched
        self.on_status: Callable[[str], None] | None = None   # engine shows this as "current"

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
        self._links_at = time.time()
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

    def _refresh_all(self) -> None:
        """Fresh download links (and change tags) for every item, in one library pass."""
        assert self.api is not None
        if self.on_status:
            self.on_status("Getting fresh download links from iCloud (about a minute)…")
        fresh: dict[str, PhotoAsset] = {}
        try:
            for asset in self.api.photos.all:
                fresh[asset.id] = asset
            albums = self.api.photos.albums
            if "Hidden" in albums:
                for asset in albums["Hidden"]:
                    fresh.setdefault(asset.id, asset)
        except _AUTH_ERRORS as e:
            raise ICloudSessionExpired() from e
        except PyiCloudAPIResponseException as e:
            if getattr(e, "code", None) in (401, 421):
                raise ICloudSessionExpired() from e
            raise ICloudError("iCloud didn't answer when asked for fresh download links. Nothing "
                              "was lost: click Continue to try again.") from e
        for item_id in list(self._assets):
            if item_id in fresh:
                self._assets[item_id] = fresh[item_id]
        self._links_at = time.time()

    def _ensure_fresh(self, item_id: str) -> None:
        if time.time() - self._links_at > LINK_MAX_AGE:
            self._refresh_all()
        if item_id not in self._assets:
            raise ICloudError("This item is no longer in iCloud (it may have been deleted).")

    def download(self, item_id: str, role: str, dest: Path) -> tuple[int, str]:
        """Stream one part to dest; returns (bytes, sha1 hex).

        Links are refreshed in bulk before they expire. If one still fails (pyicloud
        raises its own errors before we see the response, e.g. on a 410), refresh
        all links once and retry; plain network errors get a short pause.
        """
        assert self.api is not None
        self._ensure_fresh(item_id)
        for attempt in range(1, DOWNLOAD_TRIES + 1):
            url = self._url(item_id, role)
            if not url:
                raise ICloudError(f"iCloud has no download link for the {role.replace('_', ' ')}.")
            try:
                resp = self.api.session.get(url, stream=True, timeout=120)
                if resp.status_code == 200:
                    h, n = hashlib.sha1(), 0
                    with resp, dest.open("wb") as fh:
                        for chunk in resp.iter_content(CHUNK):
                            fh.write(chunk)
                            h.update(chunk)
                            n += len(chunk)
                    return n, h.hexdigest()
                resp.close()
                link_problem = True
            except _AUTH_ERRORS as e:
                raise ICloudSessionExpired() from e
            except PyiCloudAPIResponseException:
                link_problem = True                 # expired/withdrawn link (e.g. 410)
            except requests.RequestException:
                link_problem = False                # network blip
            if attempt == DOWNLOAD_TRIES:
                break
            if link_problem and time.time() - self._links_at > 120:
                self._refresh_all()                 # one pass fixes every link, not just this one
            else:
                time.sleep(2 * attempt)
            if item_id not in self._assets:
                raise ICloudError("This item is no longer in iCloud (it may have been deleted).")
        raise ICloudError(f"iCloud wouldn't give a working download after {DOWNLOAD_TRIES} tries. "
                          "It stays in iCloud; try it again later.")

    def delete(self, item_id: str) -> None:
        self._ensure_fresh(item_id)                 # also refreshes the record's change tag
        for attempt in (1, 2):
            try:
                self._assets[item_id].delete()
                return
            except _AUTH_ERRORS as e:
                raise ICloudSessionExpired() from e
            except PyiCloudAPIResponseException:
                if attempt == 2:
                    raise ICloudError("iCloud refused to delete this item. It's still there.")
                self._refresh_all()                 # stale change tag; reload and retry once

    def close(self) -> None:
        """Forget the session: delete cookies and drop references."""
        self.api = None
        self._assets = {}
        shutil.rmtree(self._dir, ignore_errors=True)
