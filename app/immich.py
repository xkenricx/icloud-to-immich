"""Immich side: a thin client over the handful of REST endpoints the app needs.

Tested against Immich v3.3.6.
"""

from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests

UNREACHABLE = ("Can't reach Immich at {url}. Check that you typed the same address you use to "
               "open Immich in your browser, including the number after the colon (the port), "
               "and that the server is turned on.")
SSL_PROBLEM = ("Immich at {url} uses a security certificate this app doesn't trust. Try the same "
               "address starting with http:// instead of https://, or the address you use on your "
               "home network.")


class ImmichError(Exception):
    """Raised with a message that is safe to show the user as-is."""


def sha1_b64(sha1_hex: str) -> str:
    return base64.b64encode(bytes.fromhex(sha1_hex)).decode()


class Immich:
    def __init__(self, url: str, api_key: str, timeout: float = 600) -> None:
        url = url.strip()
        if "://" not in url:
            url = "http://" + url
        # People paste whatever is in their address bar (".../photos", ".../albums/123",
        # ".../api"). Immich is always served at the root, so keep scheme + host + port.
        parts = urlsplit(url)
        if not parts.hostname:
            raise ImmichError("That doesn't look like a web address. It should look like 192.168.1.10:2283.")
        self.url = f"{parts.scheme}://{parts.netloc}"
        self.api = self.url + "/api"
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"x-api-key": api_key.strip(), "Accept": "application/json"})

    def _req(self, method: str, path: str, **kw):
        kw.setdefault("timeout", self.timeout)
        try:
            r = self.s.request(method, self.api + path, **kw)
        except requests.exceptions.SSLError as e:
            raise ImmichError(SSL_PROBLEM.format(url=self.url)) from e
        except requests.ConnectionError as e:
            raise ImmichError(UNREACHABLE.format(url=self.url)) from e
        except requests.Timeout as e:
            raise ImmichError("Immich took too long to answer.") from e
        if r.status_code == 401:
            raise ImmichError("Immich didn't accept that API key. Copy it again from Immich (Account "
                              "Settings → API Keys). If you can't see it any more, create a new one.")
        if r.status_code == 403:
            raise ImmichError("That API key is missing permissions. In Immich, create a new API key "
                              "and leave every permission switched on.")
        if r.status_code >= 400:
            raise ImmichError(f"Immich error {r.status_code} on {path}: {r.text[:200]}")
        return r.json() if r.content else None

    # ---------- connection ----------
    def connect(self) -> dict:
        try:
            ping = self.s.get(self.api + "/server/ping", timeout=15)
        except requests.exceptions.SSLError as e:
            raise ImmichError(SSL_PROBLEM.format(url=self.url)) from e
        except requests.RequestException as e:
            raise ImmichError(UNREACHABLE.format(url=self.url)) from e
        if ping.status_code != 200 or "pong" not in ping.text:
            raise ImmichError(f"Something answered at {self.url}, but it isn't Immich. Check the "
                              "number after the colon: it should be the port Immich uses "
                              "(2283 is the usual one; on TrueNAS it's often 30041).")
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
