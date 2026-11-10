"""Feasibility spike: can we sign in to iCloud Photos, list, download and delete?

Run from the iCloudToImmich folder:

    .venv\\Scripts\\python spike\\icloud_spike.py login
    .venv\\Scripts\\python spike\\icloud_spike.py scan
    .venv\\Scripts\\python spike\\icloud_spike.py download 5
    .venv\\Scripts\\python spike\\icloud_spike.py delete-test

Nothing here is the real app. The Apple ID password is typed at a hidden
prompt and never written to disk; only Apple's session cookies are kept, in
%LOCALAPPDATA%\\iCloudToImmich\\spike-session, so later commands don't need
the password or a new 2FA code.

delete-test ONLY touches photos inside an album named exactly "DELETE TEST"
that you create yourself in the Photos app, and asks you to type YES first.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from pyicloud import PyiCloudService

BASE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "iCloudToImmich"
SESSION_DIR = BASE / "spike-session"
DOWNLOAD_DIR = BASE / "spike-downloads"
ACCOUNT_FILE = SESSION_DIR / "account.json"
TEST_ALBUM = "DELETE TEST"


def human(n: int | None) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def connect(apple_id: str | None = None, password: str | None = None) -> PyiCloudService:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    if apple_id is None:
        if not ACCOUNT_FILE.exists():
            sys.exit("Not signed in yet. Run:  .venv\\Scripts\\python spike\\icloud_spike.py login")
        apple_id = json.loads(ACCOUNT_FILE.read_text())["apple_id"]
    api = PyiCloudService(apple_id, password, cookie_directory=str(SESSION_DIR))
    if password is None and (api.requires_2fa or api.requires_2sa):
        sys.exit("Saved session expired. Run the login command again.")
    return api


def cmd_login() -> None:
    apple_id = input("Apple ID (email): ").strip()
    password = getpass.getpass("Apple ID password (hidden, not saved): ")
    api = connect(apple_id, password)

    if api.requires_2fa:
        if not api.request_2fa_code():
            sys.exit("This account needs a hardware security key; the spike doesn't handle that yet.")
        method = getattr(api, "two_factor_delivery_method", "unknown")
        print("A code was sent by SMS." if method == "sms" else "Check your iPhone/Mac for the 2FA code.")
        for attempt in range(3):
            code = input("2FA code: ").strip()
            if api.validate_2fa_code(code):
                break
            print(f"That code didn't work ({2 - attempt} tries left).")
        else:
            sys.exit("2FA failed.")
        if not api.is_trusted_session:
            api.trust_session()
    elif api.requires_2sa:
        sys.exit("Account uses legacy two-step verification; not handled in the spike.")

    ACCOUNT_FILE.write_text(json.dumps({"apple_id": apple_id}))
    print("\nSigned in. Session saved; you won't need the password for the next steps.")
    # Prove the Photos service is reachable (fails here if ADP blocks web access).
    total = len(api.photos.all)
    print(f"iCloud Photos is reachable: {total} items in the library.")


def cmd_scan() -> None:
    api = connect()
    t0 = time.time()
    library = api.photos.all
    total = len(library)
    print(f"Library: {total} items (count took {time.time() - t0:.1f}s)")

    albums = api.photos.albums
    names = [a.name for a in albums]
    print(f"Albums visible: {len(names)}")
    for name in ("Videos", "Live", "Recently Deleted", "Hidden", TEST_ALBUM):
        if name in albums:
            print(f"  {name}: {len(albums[name])}")
        else:
            print(f"  {name}: (not found)")

    # Walk the newest 200 to see the mix of types and sizes without a full pass.
    sample, kinds, live, size_sum, oldest, newest = 0, {}, 0, 0, None, None
    t0 = time.time()
    for asset in library:
        sample += 1
        kinds[asset.item_type] = kinds.get(asset.item_type, 0) + 1
        live += int(asset.is_live_photo)
        size_sum += asset.size or 0
        d = asset.asset_date
        oldest = d if oldest is None or d < oldest else oldest
        newest = d if newest is None or d > newest else newest
        if sample >= 200:
            break
    rate = sample / max(time.time() - t0, 0.001)
    print(f"\nNewest {sample} items: {kinds}, live photos: {live}, originals total {human(size_sum)}")
    print(f"  dates {oldest:%Y-%m-%d} .. {newest:%Y-%m-%d}; listing speed ~{rate:.0f} items/s")
    if sample:
        print(f"  rough full-library size estimate: {human(int(size_sum / sample * total))}")


def download_one(asset, version: str) -> dict:
    res = asset.resources.get(version)
    if res is None:
        return {}
    t0 = time.time()
    data = asset.download(version)
    secs = time.time() - t0
    if data is None:
        return {"version": version, "ok": False, "error": "no data"}
    name = f"{asset.asset_date:%Y%m%d_%H%M%S}_{res.filename}"
    path = DOWNLOAD_DIR / name
    path.write_bytes(data)
    return {
        "version": version,
        "file": name,
        "bytes": len(data),
        "expected": res.size,
        "size_match": res.size is None or res.size == len(data),
        "sha1": hashlib.sha1(data).hexdigest(),
        "apple_checksum": res.checksum,
        "secs": round(secs, 1),
    }


def cmd_download(count: int) -> None:
    api = connect()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # Newest `count` items, plus (if not already included) the first video and
    # first Live Photo found in the newest 500, so both kinds get exercised.
    picked = []
    for seen, asset in enumerate(api.photos.all):
        has_video = any(a.item_type == "movie" for a in picked)
        has_live = any(a.is_live_photo for a in picked)
        if len(picked) < count:
            picked.append(asset)
        elif not has_video and asset.item_type == "movie":
            picked.append(asset)
        elif not has_live and asset.is_live_photo:
            picked.append(asset)
        if len(picked) >= count and has_video and has_live:
            break
        if seen >= 500:
            break

    print(f"Downloading {len(picked)} items to {DOWNLOAD_DIR}\n")
    results = []
    for asset in picked:
        versions = ["original"] + (["original_video"] if asset.is_live_photo else [])
        print(f"- {asset.filename}  type={asset.item_type} live={asset.is_live_photo} "
              f"versions={sorted(asset.resources)}")
        for v in versions:
            r = download_one(asset, v)
            r["asset_id"] = asset.id
            results.append(r)
            mark = "OK " if r.get("size_match") else "BAD"
            print(f"    [{mark}] {v}: {human(r.get('bytes'))} "
                  f"(expected {human(r.get('expected'))}) in {r.get('secs')}s")
    (DOWNLOAD_DIR / "results.json").write_text(json.dumps(results, indent=2, default=str))
    bad = [r for r in results if not r.get("size_match")]
    print(f"\n{len(results) - len(bad)}/{len(results)} files match Apple's reported size.")


def cmd_delete_test(confirm_names: set[str] | None = None) -> None:
    api = connect()
    albums = api.photos.albums
    if TEST_ALBUM not in albums:
        sys.exit(f'No album named "{TEST_ALBUM}". Create it on your iPhone and add 1-3 throwaway photos.')
    items = list(albums[TEST_ALBUM])
    if not items:
        sys.exit(f'"{TEST_ALBUM}" is empty.')
    if len(items) > 5:
        sys.exit(f'"{TEST_ALBUM}" has {len(items)} items; the test refuses more than 5.')
    rd = albums["Recently Deleted"] if "Recently Deleted" in albums else None
    before = len(rd) if rd is not None else None

    print(f'These {len(items)} item(s) in "{TEST_ALBUM}" will be deleted from iCloud (and your iPhone):')
    for a in items:
        print(f"  - {a.filename}  {a.asset_date:%Y-%m-%d %H:%M}  {human(a.size)}")
    if confirm_names is not None:
        # Pre-confirmed run: only proceed if the album holds exactly these files.
        actual = {a.filename for a in items}
        if actual != confirm_names:
            sys.exit(f"Album contents {sorted(actual)} don't match the confirmed list; nothing deleted.")
        print("Confirmed by filename list.")
    elif input('Type YES to delete them: ').strip().upper() != "YES":
        sys.exit("Cancelled, nothing deleted.")

    for a in items:
        ok = a.delete()
        print(f"  delete {a.filename}: {'sent' if ok else 'FAILED'}")

    time.sleep(5)
    api2 = connect()
    albums2 = api2.photos.albums
    remaining = len(albums2[TEST_ALBUM]) if TEST_ALBUM in albums2 else 0
    after = len(albums2["Recently Deleted"]) if "Recently Deleted" in albums2 else None
    print(f'\n"{TEST_ALBUM}" now has {remaining} item(s).')
    print(f"Recently Deleted: {before} -> {after}")
    print("Now check your iPhone: the photos should be gone from Library and sitting in Recently Deleted.")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    cmd = args[0]
    if cmd == "login":
        cmd_login()
    elif cmd == "scan":
        cmd_scan()
    elif cmd == "download":
        cmd_download(int(args[1]) if len(args) > 1 else 5)
    elif cmd == "delete-test":
        names = None
        if len(args) > 2 and args[1] == "--confirm":
            names = {n.strip() for n in args[2].split(",") if n.strip()}
        cmd_delete_test(names)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
