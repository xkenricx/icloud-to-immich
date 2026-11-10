# Google Photos as a source (design)

Status: **in progress.** The Takeout reader is being built against Google's
documented format and known quirks. It will be checked against a real Takeout
before release.

## Goal

The same wizard, with a choice at the start: **iCloud** or **Google Photos**.
Everything after that works the same way: copy, verify every file byte-for-byte,
recreate albums, Continue where you left off, and a private session per person.
A household with some iPhone users and some Google users can all use one app.

## Why Google Takeout (and not an API)

Since **31 March 2025** the Google Photos Library API only lets an app read media
**that app uploaded itself**. The read-everything scopes (`photoslibrary.readonly`,
`photoslibrary`, `photoslibrary.sharing`) were removed. The replacement Picker API
makes the user hand-pick items in a Google window, which isn't workable for a whole
library. Google has also never offered an API to **delete** photos.

So the only practical way to get a whole library out is **Google Takeout**: Google
packages the library into zip files the user downloads.

## User flow

| Step | iCloud (today) | Google Photos |
|---|---|---|
| 0. Source | choose iCloud | choose Google Photos |
| 1. Immich | sign in | sign in (same) |
| 2. Get the library | sign in to iCloud with 2FA | Takeout walkthrough, then point the app at the zips |
| 3. Choose | everything, last 30 days, older than, a period | same, using Takeout's dates |
| 4. Copy + verify | same engine | same engine, reading files straight out of the zips |
| 5. Free up space | automatic, gated by verification | **manual, guided**: the app lists which months are fully confirmed in Immich and gives the Google Photos steps to delete those, then empty Trash |

### Takeout walkthrough (shown in the app)

1. takeout.google.com → **Deselect all** → tick only **Google Photos**.
2. Leave **All photo albums included**, or pick some for a first test.
3. **Send download link via email**, **Export once**, **.zip**, **50 GB** (fewer files).
4. Wait for Google's email (minutes to days), then download every part into one folder.

### Giving the app the zips

- **Windows / Mac:** choose the folder that holds the zips.
- **Server (Docker):** a folder mounted into the container, for example a NAS share
  at `/takeout`. The app lists what it finds there.

The zips are read in place. Nothing needs unzipping, and nothing is copied to the
app's own disk except one file at a time while it's being uploaded.

## Takeout format and quirks the reader handles

Layout: `Takeout/Google Photos/<folder>/<files>`, spread across `takeout-…-001.zip`,
`-002.zip`, … A folder is either a year folder ("Photos from 2024") or an album.

- **Info files.** Each photo has a JSON sidecar holding the real date
  (`photoTakenTime.timestamp`), place (`geoData`), `description`, `favorited`,
  `archived` and `trashed`, plus the original file name in `title`.
- **Sidecar naming changed in 2024**: `IMG_1.jpg.json` became
  `IMG_1.jpg.supplemental-metadata.json`.
- **Long names are cut short**: the sidecar name is truncated (to about 51
  characters), e.g. `…jpg.supplemental-metad.json` or `…jpg.suppl.json`. Long media
  names get cut too. The sidecar's `title` keeps the full original name, so
  matching goes by `title` first.
- **Duplicates**: a second `IMG_1.jpg` becomes `IMG_1(1).jpg`, but its sidecar is
  `IMG_1.jpg(1).json` (the counter moves after the extension).
- **Edited photos** are an extra `IMG_1-edited.jpg` that shares the original's
  sidecar. It's uploaded as a stack with the edit on top, like iCloud edits. The
  suffix is localized ("-bearbeitet", "-modifié", …), so known variants are matched.
- **Albums contain copies**: a photo in an album also appears in its year folder.
  Copies are merged (same original name, same date, same size) into one item that
  belongs to those albums. An album folder has a `metadata.json` with its title.
- **iPhone Live Photos** uploaded to Google arrive as `IMG_1.HEIC` + `IMG_1.MOV`,
  which are paired like iCloud Live Photos. **Pixel motion photos** are a single
  `.MP.jpg` with the video inside, uploaded as-is (Immich understands them).
  Google warns that Takeout sometimes drops the motion part, so the app reports how
  many Live/motion photos arrived without their video.
- **Trash** (`trashed: true`) is skipped. **Archived** goes to Immich's Archive.
  **Favorites** stay favorites.
- **No sidecar**: still copied, using the date in the file itself (Immich reads it)
  and listed as "no info file" so nothing silently disappears.
- **Not in Takeout at all**: shared albums, and comments and likes from others.

## Verification and resume

Unchanged: each file's SHA-1 is computed while it's read out of the zip and must
match the checksum Immich stores. An item's id is stable across re-exports (from
its original name, date and size, not from which zip it was in), so a second
Takeout continues where the first left off.

## Free up space (manual)

No app can delete from Google Photos. Instead the app:

1. Shows which **months** are 100% confirmed in Immich (every item in that month).
2. Explains how to delete them in Google Photos: photos.google.com → scroll to the
   month → tick the date header to select the day or month → **Delete** (moves to
   Trash).
3. Reminds the user to empty **Trash** (Google keeps it for 60 days).
4. Warns about **Partner sharing / shared albums**: deleting in Google can remove
   items from other people's view.

## Still to decide

- Whether to set GPS, description and date in Immich from the sidecar when the file
  lacks them (`PUT /assets/{id}` supports these). Plan: description and GPS yes;
  date only when Immich found no date in the file.
- A size limit for very large single zips on small servers. Reading in place
  streams, so memory isn't the issue, but slow disks are.
