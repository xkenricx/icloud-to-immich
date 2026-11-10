# Building and developing

Most people should download a ready-made version instead; see the [README](README.md).

## Try the app without real accounts

A demo runs the real wizard against a fake iCloud and a fake Immich (120 made-up photos).
Any Immich address/key and Apple ID/password work, and the 2FA code is `123456`:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip
.venv/bin/python tests/demo_server.py            # desktop behavior, http://localhost:8765
.venv/bin/python tests/demo_server.py --server   # server behavior (access-key screen)
```

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

`tests/test_engine.py` covers the deletion safety rules with fake iCloud/Immich, and
`tests/test_setup.py` covers first-run behavior: the access key, Immich address
clean-up and friendly errors.

## Builds

You don't normally need to build anything. GitHub Actions
(`.github/workflows/build.yml`) builds and smoke-tests the Windows `.exe`, the Mac `.app`
and the Docker image on every push. Pushing a tag like `v0.1.2` publishes the Docker
image to GHCR (amd64 + arm64) and attaches the downloads to a GitHub release.

To build locally:

| Target | Command (run from this folder) |
|---|---|
| Windows `.exe` | `powershell -ExecutionPolicy Bypass -File build\build_windows.ps1` (needs [uv](https://docs.astral.sh/uv/)) |
| Mac `.app` | `sh build/build_mac.sh` (needs uv) |
| Docker image | `docker build -t icloud-to-immich .` |

## How it works

- `app/icloud.py`: iCloud Photos through [pyicloud](https://github.com/timlaing/pyicloud),
  an **unofficial** client for Apple's web API.
- `app/immich.py`: the Immich REST endpoints used (tested on Immich v3.3.6).
- `app/engine.py`: choose → copy + verify (SHA-1) → albums → delete. An item is only
  deletable when every one of its files is verified in Immich, and it's checked again
  right before deleting.
- `app/state.py`: SQLite progress per (Apple ID, Immich server), so runs resume.
- `app/server.py` + `app/static/`: the wizard. Desktop mode binds to 127.0.0.1 with a
  per-launch key and reopens an already-running copy. Server mode (Docker) creates a
  key once, keeps it in the data folder, and prints the link in the logs.
  `ICLOUD2IMMICH_TOKEN` overrides the key; `ICLOUD2IMMICH_URL` makes the log show the
  exact link; `ICLOUD2IMMICH_IMMICH_URL` pre-fills the Immich address for everyone.
- Sessions: each browser sends a random `X-Session` id and gets its own `App` (Immich +
  iCloud sign-in, engine, idle timer). One iCloud account can only be active in one
  session at a time. Idle sessions are signed out after 20 minutes, never mid-copy.
  `GET /api/overview` gives household-wide counts (no details) for update scripts.
- `spike/`: the original feasibility scripts and [findings](spike/FINDINGS.md).
