"""Immich side: a thin client over the handful of REST endpoints the app needs.

Tested against Immich v3.3.6.
"""

from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path

import requests


class ImmichError(Exception):
    """Raised with a message that is safe to show the user as-is."""


def sha1_b64(sha1_hex: str) -> str:
    return base64.b64encode(bytes.fromhex(sha1_hex)).decode()


class Immich:
    def __init__(self, url: str, api_key: str, timeout: float = 600) -> None:
        url = url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        if url.endswith("/api"):
            url = url[:-4]
        self.url = url
        self.api = url + "/api"
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"x-api-key": api_key.strip(), "Accept": "application/json"})

    def _req(self, method: str, path: str, **kw):
        kw.setdefault("timeout", self.timeout)
        try:
            r = self.s.request(method, self.api + path, **kw)
        except requests.ConnectionError as e:
            raise ImmichError(f"Can't reach Immich at {self.url}. Check the address and that the server is on.") from e
        except requests.Timeout as e:
            raise ImmichError("Immich took too long to answer.") from e
        if r.status_code == 401:
            raise ImmichError("Immich rejected the API key.")
        if r.status_code == 403:
            raise ImmichError("The API key is missing a permission. Create one with all permissions.")
        if r.status_code >= 400:
            raise ImmichError(f"Immich error {r.status_code} on {path}: {r.text[:200]}")
        return r.json() if r.content else None

    # ---------- connection ----------
    def connect(self) -> dict:
        try:
            ping = self.s.get(self.api + "/server/ping", timeout=15)
        except requests.RequestException as e:
            raise ImmichError(f"Can't reach Immich at {self.url}. Check the address and that the server is on.") from e
        if ping.status_code != 200 or "pong" not in ping.text:
            raise ImmichError(f"{self.url} doesn't look like an Immich server.")
        version = self._req("GET", "/server/version")
        me = self._req("GET", "/users/me")
        return {"version": f"{version['major']}.{version['minor']}.{version['patch']}",
                "user": me.get("name") or me.get("email"), "email": me.get("email")}

    # ---------- assets ----------
    def upload(self, path: Path, filename: str, taken: datetime, *, favorite: bool = False,
               visibility: str | None = None, live_video_id: str | None = None) -> tuple[str, str]:
        """Returns (asset id, "created" | "duplicate")."""
        when = taken.isoformat()
        data = {"fileCreatedAt": when, "fileModifiedAt": when, "filename": filename,
                "isFavorite": "true" if favorite else "false"}
        if visibility:
            data["visibility"] = visibility
        if live_video_id:
            data["livePhotoVideoId"] = live_video_id
        with path.open("rb") as fh:
            res = self._req("POST", "/assets", data=data, files={"assetData": (filename, fh)})
        return res["id"], res.get("status", "created")

    def asset(self, asset_id: str) -> dict:
        return self._req("GET", f"/assets/{asset_id}")

    def verify(self, asset_id: str, sha1_hex: str) -> bool:
        """True only if Immich holds this asset, not in trash, with exactly our bytes."""
        a = self.asset(asset_id)
        return a.get("checksum") == sha1_b64(sha1_hex) and not a.get("isTrashed", False)

    def check_present(self, items: list[tuple[str, str]]) -> dict[str, str | None]:
        """For (key, sha1 hex) pairs: Immich asset id holding those bytes (not trashed), else None."""
        out: dict[str, str | None] = {}
        for i in range(0, len(items), 500):
            batch = items[i:i + 500]
            res = self._req("POST", "/assets/bulk-upload-check",
                            json={"assets": [{"id": k, "checksum": sha1_b64(c)} for k, c in batch]})
            for r in res["results"]:
                present = r.get("action") == "reject" and r.get("reason") == "duplicate" \
                    and not r.get("isTrashed", False)
                out[r["id"]] = r.get("assetId") if present else None
        return out

    def link_live(self, image_id: str, video_id: str) -> None:
        self._req("PUT", f"/assets/{image_id}", json={"livePhotoVideoId": video_id})

    def stack(self, primary_id: str, other_id: str) -> None:
        self._req("POST", "/stacks", json={"assetIds": [primary_id, other_id]})

    # ---------- albums ----------
    def albums(self) -> dict[str, str]:
        return {a["albumName"]: a["id"] for a in self._req("GET", "/albums")}

    def create_album(self, name: str, asset_ids: list[str]) -> str:
        return self._req("POST", "/albums", json={"albumName": name, "assetIds": asset_ids})["id"]

    def add_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        for i in range(0, len(asset_ids), 500):
            self._req("PUT", f"/albums/{album_id}/assets", json={"ids": asset_ids[i:i + 500]})
