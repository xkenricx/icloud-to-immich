"""Spike: download an edited photo's original + edited render and stack them in Immich.

Uses the saved iCloud session and the test Immich config. Picks the first
edited still and the first edited video in the library. Also reports the
favorite / hidden flags so we know where they live.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from icloud_spike import BASE, connect, human  # noqa: E402
from pyicloud.services.photos_cloudkit.mappers import (  # noqa: E402
    build_photo_resource, record_field_value)
from pyicloud.services.photos_cloudkit.service import PhotoAsset  # noqa: E402

OUT = BASE / "spike-edits"
cfg = json.loads((BASE / "immich-test.json").read_text())
API = cfg["url"].rstrip("/") + "/api"
S = requests.Session()
S.headers.update({"x-api-key": cfg["api_key"], "Accept": "application/json"})


def edited_resource(asset):
    for key, prefix in (("edited", "resJPEGFull"), ("edited_video", "resVidFull")):
        r = build_photo_resource(
            key=key, prefix=prefix, master_record=asset.asset_record,
            filename=asset.filename, item_type_extensions=PhotoAsset.FILE_TYPE_EXTENSIONS,
            is_live_photo=False, item_type_lookup=PhotoAsset.ITEM_TYPES)
        if r is not None:
            return r
    return None


def fetch(api, url: str) -> bytes:
    return api.photos.private_client.download_asset_bytes(url)


def upload(path: Path, when: str) -> str:
    with path.open("rb") as fh:
        r = S.post(f"{API}/assets", data={"fileCreatedAt": when, "fileModifiedAt": when,
                                          "filename": path.name},
                   files={"assetData": (path.name, fh)})
    r.raise_for_status()
    return r.json()["id"]


api = connect()
OUT.mkdir(parents=True, exist_ok=True)
picked = {}
fav_count = hidden_count = 0
for asset in api.photos.all:
    if record_field_value(asset.asset_record, "isFavorite") == 1:
        fav_count += 1
    if record_field_value(asset.asset_record, "isHidden") == 1:
        hidden_count += 1
    if record_field_value(asset.asset_record, "adjustmentType") is None:
        continue
    res = edited_resource(asset)
    if res is None:
        continue
    kind = "video" if asset.item_type == "movie" else "image"
    picked.setdefault(kind, (asset, res))
print(f"flags across library: isFavorite=1 on {fav_count}, isHidden=1 on {hidden_count}")

for kind, (asset, res) in picked.items():
    when = asset.asset_date.isoformat()
    print(f"\n{kind}: {asset.filename} adjustmentType="
          f"{record_field_value(asset.asset_record, 'adjustmentType')}")
    orig_bytes = asset.download("original")
    edit_bytes = fetch(api, res.url)
    base = Path(asset.filename).stem
    o = OUT / f"{base}{Path(asset.resources['original'].filename).suffix}"
    e = OUT / f"{base}_edited{Path(res.filename).suffix}"
    o.write_bytes(orig_bytes)
    e.write_bytes(edit_bytes)
    print(f"  original {human(len(orig_bytes))} (expected {human(asset.resources['original'].size)})"
          f", edited {human(len(edit_bytes))} (expected {human(res.size)}) type={res.type}")
    oid, eid = upload(o, when), upload(e, when)
    st = S.post(f"{API}/stacks", json={"assetIds": [eid, oid]})
    print(f"  stack create -> {st.status_code}")
    st.raise_for_status()
    stack = st.json()
    print(f"  stack primary is edited: {stack['primaryAssetId'] == eid}; members: {len(stack['assets'])}")
    for aid, data in ((oid, orig_bytes), (eid, edit_bytes)):
        a = S.get(f"{API}/assets/{aid}").json()
        same = a["checksum"] == base64.b64encode(hashlib.sha1(data).digest()).decode()
        print(f"    {a['originalFileName']}: checksum match={same}, stack={bool(a.get('stack'))}")

if not picked:
    print("No edited items with a resJPEGFull/resVidFull render found.")
