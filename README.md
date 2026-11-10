# iCloud to Immich

Move your photos and videos from **iCloud** into your own **[Immich](https://immich.app)**
photo server. Every file is checked to be an exact copy. Afterwards, only if you want
to, the app can free up the space in iCloud and on your iPhone.

You don't need to be technical. You follow six steps on a web page, and each one tells
you what to do.

> **Not affiliated with Apple or Immich.** The app reaches iCloud through an
> *unofficial* connection that Apple could change at any time. The optional last step
> deletes photos from iCloud and all your Apple devices. The app only deletes what it
> has confirmed is safely in Immich, but this is free software with no warranty (see
> [LICENSE](LICENSE)). Keep a backup you trust.

---

## Which version should I use?

| If… | Use |
|---|---|
| Your Immich runs on a home server, NAS or Raspberry Pi (Docker, TrueNAS, Synology, Unraid, Portainer…) and you'd like the app to live there too | **[On your server](#on-your-server)** (recommended) |
| You just want to run it once from your computer | **[Windows](#windows)** or **[Mac](#mac)** |

All versions do exactly the same thing. Your photos go straight from Apple to your own
Immich and never pass through anyone else's computer.

---

## What you'll need

1. **Your Immich address**: what you type in your browser to open Immich, for example
   `192.168.1.10:2283`. The app remembers it after the first time.
2. **Your Immich email and password**: the ones you sign in to Immich with. Your photos
   go into *your own* Immich library.
   - *If you sign in to Immich with Google or another single-sign-on service* (so you have
     no Immich password), use an **API key** instead. In Immich: profile picture →
     **Account Settings** → **API Keys** → **New API Key**, leave all permissions on, and
     copy the key it shows (only once). In the app, click **Use an API key**.
3. **Your Apple ID and password**, and your iPhone nearby for the 6-digit code Apple
   sends.

> **If you use Advanced Data Protection** on your Apple account, turn on
> **Access iCloud Data on the Web** first (iPhone: Settings → your name → iCloud).
> Otherwise the app can't see your photos.

### About the "access key" (the `#t=…` part of the link)

The app's own link ends in `#t=` followed by a long code. That code is the app's
**access key**. It keeps other people on your network from using the app.

It's **not** your Immich password or your Apple password. The app makes it for you, it
goes at the end of the app's link, and everyone in your household can use the same link.

If you ever open the app without its access key, it asks for it and tells you where to
find it. After that your browser remembers it.

---

## On your server

This runs the app on the same machine as your Immich, so it's always there when you
need it.

1. **Add the app.** Copy the contents of [`docker-compose.example.yml`](docker-compose.example.yml)
   and paste it into your server's screen for adding an app from "compose" or "YAML":
   - **TrueNAS SCALE:** Apps → Discover Apps → **⋮** (three dots) → **Install via YAML**
   - **Portainer:** Stacks → **Add stack** → paste into the web editor → **Deploy the stack**
   - **Synology:** Container Manager → **Project** → **Create** → paste it in
   - **Anything with a terminal:** save it as `docker-compose.yml` and run `docker compose up -d`
2. **Find your link.** Open the app's **logs**:
   - TrueNAS: Apps → click **icloud-to-immich** → the logs icon
   - Portainer: Containers → **icloud-to-immich** → **Logs**
   - Synology: Container Manager → Container → **icloud-to-immich** → **Log**
   - Terminal: `docker logs icloud-to-immich`

   Look for the box made of `=====` lines. Inside it is a link like
   `http://YOUR-SERVER-ADDRESS:PORT/#t=…`.
3. **Open it.** Replace `YOUR-SERVER-ADDRESS:PORT` with your server's address and
   `8120`, for example `http://192.168.1.10:8120/#t=…`, and keep everything after `#t=`
   as it is. **Bookmark it.** The link stays the same, even after restarts.

When you're done, click **Sign out**. The app stays on your server for next time, and
it also signs you out by itself after 20 minutes with the page closed. You can close the
page while it's copying: the copy keeps going on the server.

### Sharing it with your household

Everyone can use the same link at the same time. Each person's browser gets its own
private session: they sign in to **their own** Immich account and **their own** iCloud,
and their photos go into their own Immich library. Nobody sees anyone else's sign-ins,
photos or progress, and signing out only signs *you* out.

Tip: to save everyone typing the Immich address, add this to the compose file and it's
filled in for them:

```yaml
    environment:
      ICLOUD2IMMICH_IMMICH_URL: "http://192.168.1.10:2283"   # your Immich address
```

To update to a newer version later, use your server's "pull / update" button for the
app. In a terminal: `docker compose pull && docker compose up -d`.

---

## Windows

1. Download **iCloudToImmich.exe** from the
   [latest release](https://github.com/xkenricx/icloud-to-immich/releases/latest).
2. Double-click it. Windows may say *"Windows protected your PC"*, because the app isn't
   signed by a paid certificate yet. Click **More info** → **Run anyway**.
3. A small black window opens (leave it open) and your web browser opens the app.
   That's it.

If you close the browser tab by accident, double-click the app again and it reopens the
same page. When you're finished, click **Sign out and close** on the page. Keep your
computer awake while it copies.

---

## Mac

1. Download **iCloudToImmich-mac.zip** from the
   [latest release](https://github.com/xkenricx/icloud-to-immich/releases/latest).
   (For Macs with Apple Silicon: M1 or newer.)
2. Double-click the zip to unpack it, then drag **iCloud to Immich** into your
   **Applications** folder.
3. Open it. The first time, macOS says it *"can't be opened"* because it isn't from the
   App Store or a paid developer account. To allow it:
   - Open **System Settings** → **Privacy & Security**, scroll down, and click
     **Open Anyway** next to "iCloud to Immich". Confirm with your password.
   - (On older macOS versions you can instead right-click the app → **Open** → **Open**.)
4. Your web browser opens the app. If you close the tab, open the app again.

When you're finished, click **Sign out and close** on the page. Keep your Mac awake
while it copies.

---

## Using the app

1. **Sign in to your Immich**: your Immich address, email and password.
2. **Sign in to iCloud**: your Apple ID, password, then the 6-digit code.
3. **Choose what to move.** The first time, it suggests **"Try it first: just the last
   30 days"**. Do that, then look at the result in Immich before moving everything.
   You can also choose **Everything**, **Only older photos** (keep recent ones on your
   phone), or **A specific period**.
4. **Copy to Immich.** Nothing is deleted in this step. You can pause and continue later.
   It never makes duplicates.
   - **Live Photos** keep their motion.
   - **Edited photos** arrive with your edit on top and the untouched original underneath.
   - **Albums** are recreated with the same names.
   - **Favorites** stay favorites, and **hidden** photos go to Immich's Archive.
5. **Free up space (optional).** Deletes the copied photos from iCloud and your Apple
   devices. You see exactly how many first and type `DELETE` to confirm. Anything not
   confirmed as an exact copy in Immich is never deleted.
6. **Finish**: sign out.

> **Getting the space back:** deleted photos wait in **Recently Deleted** for 30 days,
> just like when you delete them in the Photos app. To free the space right away, on
> your iPhone open Photos → **Recently Deleted** → **Select** → **Delete All**.

### How long will it take?

Plan on roughly **an hour or two for every 20–25 GB** of photos and videos. The app
shows how much your selection is in step 3 and an estimate of the time left while it
copies.

It can seem slow, and that's expected. Here's why:

- **It copies one photo at a time, carefully.** For each one it downloads the file from
  Apple, uploads it to your Immich, then asks Immich to confirm it's an exact copy before
  moving on. A Live Photo is two files (the photo and its short video), and an edited
  photo is two more (your edit and the original). Most of the time goes on waiting for
  these back-and-forth trips, not on actually moving data.
- **Apple sets the download speed.** iCloud sends files at whatever pace it chooses, and
  that varies from day to day. Nothing on your side can make Apple go faster.
- **Lots of small photos take longer than a few big videos of the same total size**,
  because every photo has the same trips to make, however small it is.
- **Your Immich keeps working after each upload.** It makes thumbnails and reads the
  photo's details, which can slow uploads a little on a small server or a Raspberry Pi.

Things that **don't** make it faster: giving the app more memory (it only needs a little,
even for huge videos) or a faster computer to run it on.

What helps: start with a small test (**Try it first**), then run **Everything** and let it
work. On a server you can close the page while it copies, and it carries on. On Windows or
Mac, keep the computer awake. If it's interrupted for any reason, start it again: nothing
that's already copied is copied twice.

> Copying several photos at the same time would make it noticeably faster, and it's
> planned for a future version.

### Not supported yet

Apple ID hardware security keys, iCloud Shared Library and shared albums, and Apple's
old "two-step verification".

---

## Common problems

| You see | What to do |
|---|---|
| **"This page needs its access key"** | Open the link from the app's logs (server) or reopen the app (Windows/Mac). See [About the access key](#about-the-access-key-the-t-part-of-the-link). |
| **"This iCloud account is already being moved in another browser"** | The same Apple ID is signed in on another device or browser. Carry on there, or click **Sign out** there first. |
| **"Immich didn't accept that email and password"** | Use the ones you sign in to Immich with. If you use Google or another single-sign-on service for Immich, click **Use an API key** instead. |
| **"Can't reach Immich"** | Use exactly the address you use to open Immich in your browser, including the number after the colon. |
| **"…not 'localhost'"** | On a server, use your Immich server's network address (like `192.168.1.10:2283`) instead of `localhost`. |
| **"Immich didn't accept that API key"** | Create a new API key in Immich and paste it again. Immich only shows a key once. Or sign in with your Immich email and password instead. |
| **"Apple ID or password is wrong"** | Check them at [appleid.apple.com](https://appleid.apple.com). |
| **"iCloud Photos can't be reached from the web"** | Turn on *Access iCloud Data on the Web* (see [What you'll need](#what-youll-need)). |
| **It seems slow** | That's normal: it copies and checks one photo at a time, and Apple sets the download speed. See [How long will it take?](#how-long-will-it-take) |
| **Some items "didn't copy"** | Click the button to try them again. They stay safely in iCloud until they copy. |

Still stuck? [Open an issue](https://github.com/xkenricx/icloud-to-immich/issues) and
describe what you see. For security problems, see [SECURITY.md](SECURITY.md).

## Privacy

Everything runs on your own computer or server. The app only talks to Apple (iCloud)
and to your Immich. Nothing is sent to the developer or anyone else. It only saves a
small progress file (photo IDs, file names, sizes and checksums: no photos and no
passwords) so a paused copy can carry on.

## For developers

See [BUILDING.md](BUILDING.md).

## License

[MIT](LICENSE)
