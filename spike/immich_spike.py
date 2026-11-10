"""Feasibility spike, Immich half: upload what icloud_spike.py downloaded, then prove
  1. checksums Immich stores == SHA-1 we computed during the iCloud download
  2. a Live Photo's HEIC + MOV end up paired as one asset
  3. re-running is safe (bulk-upload-check reports duplicates, no second copy)
  4. an album can be recreated with the uploaded assets

Reads %LOCALAPPDATA%\\iCloudToImmich\\immich-test.json  ({"url", "api_key"})
and   %LOCALAPPDATA%\\iCloudToImmich\\spike-downloads\\results.json
"""

from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "iCloudToImmich"
DL = BASE / "spike-downloads"
cfg = json.loads((BASE / "immich-test.json").read_text())
API = cfg["url"].rstrip("/") + "/api"
S = requests.Session()
S.headers["x-api-key"] = cfg["api_key"]
S.headers["Accept"] = "application/json"


def b64(sha1_hex: str) -> str:
    return base64.b64encode(bytes.fromhex(sha1_hex)).decode()


def check(items: list[tuple[str, str]]) -> dict[str, dict]:
    r = S.post(f"{API}/assets/bulk-upload-check",
               json={"assets": [{"id": i, "checksum": c} for i, c in items]})
    r.raise_for_status()
    return {x["id"]: x for x in r.json()["results"]}


def upload(entry: dict, live_video_id: str | None = None, visibility: str | None = None) -> dict:
    path = DL / entry["file"]
    stamp = path.name[:15]  # YYYYmmdd_HHMMSS prefix written by icloud_spike.py
    when = datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc).isoformat()
    original_name = path.name[16:]
    data = {"fileCreatedAt": when, "fileModifiedAt": when, "filename": original_name}
    if live_video_id:
        data["livePhotoVideoId"] = live_video_id
    if visibility:
        data["visibility"] = visibility
    with path.open("rb") as fh:
        r = S.post(f"{API}/assets", data=data, files={"assetData": (original_name, fh)})
    if r.status_code >= 400:
        sys.exit(f"upload {path.name} failed {r.status_code}: {r.text[:300]}")
    return r.json()


def main() -> None:
    results = [r for r in json.loads((DL / "results.json").read_text()) if r.get("size_match")]
    by_asset: dict[str, dict[str, dict]] = {}
    for r in results:
        by_asset.setdefault(r["asset_id"], {})[r["version"]] = r

    pre = check([(r["file"], r["sha1"]) for r in results])
    print("Before upload, bulk-upload-check:",
          {k: v["action"] for k, v in pre.items()})

    uploaded: dict[str, str] = {}  # file -> immich id
    stills: list[str] = []         # ids that should appear in the album/timeline
    for asset_id, versions in by_asset.items():
        orig = versions["original"]
        if "original_video" in versions:
            vid = versions["original_video"]
            vres = upload(vid, visibility="hidden")
            uploaded[vid["file"]] = vres["id"]
            ires = upload(orig, live_video_id=vres["id"])
            uploaded[orig["file"]] = ires["id"]
            print(f"  live  {orig['file']} -> {ires['id'][:8]} (+video {vres['id'][:8]}) {ires['status']}")
        else:
            ires = upload(orig)
            uploaded[orig["file"]] = ires["id"]
            print(f"  plain {orig['file']} -> {ires['id'][:8]} {ires['status']}")
        stills.append(ires["id"])

    # 1. checksum verify: what Immich stored vs what we hashed while downloading
    print("\nChecksum verify (Immich stored vs our SHA-1):")
    ok = 0
    for r in results:
        a = S.get(f"{API}/assets/{uploaded[r['file']]}").json()
        match = a.get("checksum") == b64(r["sha1"])
        ok += match
        print(f"  [{'OK ' if match else 'BAD'}] {r['file']}  type={a.get('type')} "
              f"visibility={a.get('visibility')} live={bool(a.get('livePhotoVideoId'))}")
    print(f"  {ok}/{len(results)} match")

    # 2. live pairing
    print("\nLive Photo pairing:")
    for asset_id, versions in by_asset.items():
        if "original_video" in versions:
            img = S.get(f"{API}/assets/{uploaded[versions['original']['file']]}").json()
            want = uploaded[versions["original_video"]["file"]]
            print(f"  {versions['original']['file']}: livePhotoVideoId "
                  f"{'== uploaded MOV' if img.get('livePhotoVideoId') == want else repr(img.get('livePhotoVideoId'))}")

    # 3. idempotency
    post = check([(r["file"], r["sha1"]) for r in results])
    dupes = sum(1 for r in results
                if post[r["file"]]["action"] == "reject" and post[r["file"]].get("assetId") == uploaded[r["file"]])
    print(f"\nRe-run check: {dupes}/{len(results)} reported as already present with the same Immich id")

    # 4. album
    r = S.post(f"{API}/albums", json={"albumName": "Spike / Test Album", "assetIds": stills})
    r.raise_for_status()
    alb = S.get(f"{API}/albums/{r.json()['id']}").json()
    print(f"\nAlbum '{alb['albumName']}': {alb.get('assetCount')} assets (expected {len(stills)})")

    stats = S.get(f"{API}/assets/statistics").json()
    print("Timeline statistics:", stats)


if __name__ == "__main__":
    main()
