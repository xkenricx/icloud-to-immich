# iCloud to Immich

Move your photos and videos from iCloud Photos into your own [Immich](https://immich.app)
server. Then, only if you want, free up the space in iCloud and on your iPhone.

It's a tool you **run once, when you need it**: start it, follow the six steps in your
browser, close it. Nothing runs in the background afterwards.

> **Not affiliated with Apple or Immich.** iCloud access uses an *unofficial* web API
> that Apple can change or block at any time. The optional delete step removes photos
> from iCloud and all your Apple devices. The app goes to great lengths to delete only
> what it has verified in Immich, but this is free software with no warranty (see
> [LICENSE](LICENSE)). Keep a backup you trust.

## What it does

1. **Connects to your Immich** with an API key.
2. **Signs in to iCloud** with your Apple ID, password and 2FA code. The password goes
   straight to Apple from your machine and is never saved.
3. **Lets you choose what to move**: everything, items older than N days, or a date range.
4. **Copies and checks every file.** Each one is downloaded from iCloud, uploaded to
   Immich, then checked byte-for-byte (SHA-1) against what Immich stored.
   - **Live Photos** keep their motion: the photo and its short video are linked in Immich.
   - **Edited photos** arrive as a stack, with your edit on top and the untouched original
     underneath.
   - **Albums** are recreated with the same names. Apple's automatic albums (Videos,
     Screenshots…) are skipped because Immich has its own.
   - **Hidden** items go to Immich's Archive. **Favorites** stay favorites.
   - You can stop at any time. Running it again picks up where it left off and never
     uploads duplicates.
5. **Frees up space (optional).** It deletes the copied items from iCloud, which removes
   them from your iPhone, iPad and Mac too. An item can only be deleted if *every* file
   it has (photo, Live Photo video, edited version) is confirmed identical in Immich and
   not in Immich's trash. Each item is checked again just before it's deleted, and you
   type `DELETE` to confirm.
6. **Signs you out** and removes the temporary sign-in data.

> **Getting the space back:** deleted items go to **Recently Deleted** for 30 days, the
> same as when you delete in the Photos app. To free the space now, on your iPhone open
> Photos → Recently Deleted → Select → Delete All.

## Three ways to run it

### Docker (best if you already run Immich)

On the machine that runs Immich (works on x86 and ARM, e.g. a Raspberry Pi):

```sh
docker run --rm -p 8080:8080 -v icloud2immich:/data ghcr.io/xkenricx/icloud-to-immich:latest
```

The log prints an address like `http://SERVER:8080/#t=…`. Open it in your browser, with
`SERVER` replaced by that machine's IP or name. The part after `#t=` is a one-time key, so
don't share the link. Click **Sign out and close** at the end and the container exits.

To build the image yourself instead: `docker build -t icloud-to-immich .`

#### As an always-available service

To keep it on your home server and use it whenever you like, use
[`docker-compose.example.yml`](docker-compose.example.yml). It gives you a fixed,
bookmarkable link (set `ICLOUD2IMMICH_TOKEN`), restarts automatically, and signs out of
iCloud after 20 idle minutes so your Apple session isn't left open between uses. Keep it
on your home network: don't expose it to the internet.

### Windows

Download **iCloudToImmich.exe** from the [latest release](https://github.com/xkenricx/icloud-to-immich/releases/latest)
and double-click it. A small window opens to show the app is running, and
your browser opens the wizard. Keep the window open until you're done.

Windows may warn that the app is from an unknown publisher (it isn't code-signed yet).
Click **More info → Run anyway**.

### Mac

Download **iCloudToImmich-mac.zip** from the [latest release](https://github.com/xkenricx/icloud-to-immich/releases/latest)
(Apple Silicon Macs), unzip it, and open **iCloud to Immich.app**. Your browser opens the wizard.

macOS will say it's from an unidentified developer (it isn't notarized yet). Right-click
the app → **Open** → **Open**. You only need to do this the first time.

## Before you start

- **An Immich API key:** in Immich, profile picture → Account Settings → API Keys →
  New API Key, with all permissions.
- **Advanced Data Protection:** if it's on, enable *Access iCloud Data on the Web*
  (iPhone: Settings → your name → iCloud) or the app can't see your photos.
- **Space on your Immich server:** the wizard shows how much the selection needs.
- **Not supported yet:** hardware security keys for Apple ID, Shared Library and shared
  albums, and the old two-step verification.

## Privacy

Everything runs on your own machine or server. It talks only to Apple (iCloud) and your
Immich server. Nothing is sent to the developer or anywhere else. The only thing saved
is a small progress file (item ids, file names, sizes and checksums, no photos and no
passwords), kept so a stopped run can resume:

- Windows: `%LOCALAPPDATA%\iCloudToImmich\runs`
- Mac: `~/Library/Application Support/iCloudToImmich/runs`
- Docker: the `/data` volume

## How it works (for developers)

- `app/icloud.py`: iCloud Photos through [pyicloud](https://github.com/timlaing/pyicloud),
  an **unofficial** client for Apple's web API. Apple can change that API at any time.
- `app/immich.py`: the Immich REST endpoints used (tested on Immich v3.3.6).
- `app/engine.py`: scope → copy + verify → albums → delete, and the deletion safety rule.
- `app/state.py`: SQLite progress per (Apple ID, Immich server).
- `app/server.py` + `app/static/`: the local wizard. It binds to 127.0.0.1 unless run in
  Docker, and every API call needs the per-launch key.
- `tests/`: engine tests with fake iCloud/Immich, focused on "never delete unless verified".
  Run with `python -m unittest discover -s tests`.
- `spike/`: the original feasibility scripts and [findings](spike/FINDINGS.md).
- Builds: `build/build_windows.ps1`, `build/build_mac.sh`, `Dockerfile`. CI
  (`.github/workflows/build.yml`) builds and smoke-tests all three on every push. Pushing
  a `v*` tag publishes the Docker image and attaches the Windows/Mac downloads to a release.

## Problems and questions

Open an [issue](https://github.com/xkenricx/icloud-to-immich/issues). For security
problems, see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
