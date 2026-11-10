# Feasibility spike findings (November 2026)

Before building the app, these scripts proved each piece against a real iCloud
library (a few thousand items) and a throwaway Immich v3.3.6 server. The
scripts are kept for reference. They read local, untracked config files and
are not part of the app.

- `icloud_spike.py`: sign in, list, download, delete (iCloud side)
- `immich_spike.py`: upload, checksum verify, Live Photo pairing, albums
- `edits_spike.py`, `probe_edits.py`: edited versions and record fields

Library: [`pyicloud`](https://github.com/timlaing/pyicloud) 2026.10.1, the
maintained fork, using its CloudKit photos service.

## iCloud side: all pass

| Step | Result |
|---|---|
| Sign in (Apple ID + password + 2FA code from a trusted device) | `request_2fa_code()` → `validate_2fa_code()` → `trust_session()` |
| Reuse the session without the password | Works from saved cookies |
| Count / list | `len(photos.all)` in ~2 s; iterating runs at ~35 items/s |
| Download originals | Byte-exact vs Apple's reported size, ~45 MB/s; real JPEG/HEIC/MOV |
| Streaming download | `session.get(url, stream=True)` gives identical SHA-1, so memory stays flat for big videos |
| Delete | `asset.delete()` sets `isDeleted=1`; the item moves to Recently Deleted |

Things the app has to handle:

- **Listing order** is by date added, ascending, not newest first. Date scoping
  must filter rather than truncate.
- **Live Photos are two files**: `original` (HEIC) and `original_video` (MOV).
  Both must reach Immich and be verified before anything is deleted.
- **Edited photos**: pyicloud only exposes the unedited original. The edited
  render lives on the *asset* record (`resJPEGFull*`, `resVidFull*` for
  video), and `build_photo_resource(master_record=asset.asset_record, ...)`
  gives a working URL for it.
- **Checksums**: Apple's `checksum`/fingerprint is its own format, not SHA-1.
  Verification is the SHA-1 computed during download compared with the one
  Immich stores.
- **Albums**: user albums can be recreated; smart albums (Videos, Live,
  Screenshots, …) are skipped. Albums must be recreated *before* deleting,
  because deleting from iCloud also removes items from iCloud albums. Many
  app-created albums (CapCut, Snapchat, …) can exist but be empty.
- **Space isn't freed by the delete**: items wait 30 days in Recently Deleted.
- **Duplicates**: an iCloud library can hold the same file twice. Immich keeps
  one asset and returns `duplicate` for the second, and both iCloud items count
  as verified.

## Immich side: all pass (v3.3.6)

| Check | Result |
|---|---|
| `POST /assets/bulk-upload-check` with our SHA-1 before upload | all `accept` |
| `POST /assets` (multipart; `fileCreatedAt`/`fileModifiedAt` required) | created |
| Stored `checksum` == base64(our SHA-1) | 100% match. This is the delete gate. |
| Live Photo: MOV uploaded with `visibility=hidden`, then HEIC with `livePhotoVideoId` | paired, MOV hidden |
| Re-run `bulk-upload-check` | all `reject`/`duplicate` with the same asset id, so resuming is safe |
| `POST /stacks` with `[edited, original]` | edit becomes the primary |
| `POST /albums` with asset ids | works, including names containing " / " |
| Dates | Immich reads EXIF itself; local time is right with and without an offset |

Notes:

- Immich uses EXIF dates when present, but `fileCreatedAt` is still required,
  so send the iCloud asset date.
- Without machine learning, the Immich server process still wanted more than
  1.5 GB RAM during uploads; 2 GB was comfortable.

## End-to-end wizard test

One month (36 items: 19 Live Photos, 8 edited, 1 video, 1 album photo) through
the real wizard: 36/36 verified, 19/19 Live Photos linked, 8/8 edits stacked
with the edit on top, and the album recreated. One duplicate file collapsed
correctly.

## Not tested yet

- **Favorites / hidden mapping**: the test library had neither, so the
  `isFavorite` / `isHidden` field names are unverified.
- Album folders, and albums with hundreds of items.
- The delete step through the wizard UI. The engine's delete rules are
  unit-tested, and the raw iCloud delete was proven on throwaway items.
- Accounts with Advanced Data Protection, hardware security keys, or SMS-only 2FA.
- iCloud Shared Library and shared albums.
- Very large libraries (tens of thousands of items) and a session expiring mid-run.
