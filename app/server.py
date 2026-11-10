"""Local-only web server that drives the wizard page.

Binds to 127.0.0.1 on a random port. Every API call must carry the per-launch
token from the URL the app opened, and the Host header must be the loopback
address, so other web pages open in the same browser can't drive it.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .engine import Engine, Scope
from .icloud import ICloud, ICloudError
from .immich import Immich, ImmichError
from .state import State, data_dir, past_runs

IDLE_EXIT_SECONDS = 20 * 60


def static_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "app" / "static"


class App:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.immich: Immich | None = None
        self.immich_info: dict | None = None
        self.icloud: ICloud | None = None
        self.icloud_status = "signed_out"   # signed_out | needs_2fa | signed_in
        self.library_count: int | None = None
        self.engine: Engine | None = None
        self.state: State | None = None
        self.scope = Scope()
        self.scanned = False
        self.last_seen = time.time()
        self.mode = "desktop"               # desktop | server (set in serve())

    # ---------- actions ----------
    def connect_immich(self, url: str, key: str) -> dict:
        if self.engine and self.engine.busy:
            raise ValueError("Wait for the current step to finish.")
        if not url.strip():
            raise ValueError("Enter your Immich address: the one you type in your browser to open Immich.")
        if not key.strip():
            raise ValueError("Paste your Immich API key. Open 'How do I get an API key?' below for the steps.")
        host = (urllib.parse.urlsplit(url if "://" in url else "http://" + url).hostname or "").lower()
        if self.mode == "server" and host in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Use your Immich server's network address (like 192.168.1.10:2283), not "
                             "'localhost'. This app runs in its own container, so 'localhost' "
                             "means the app itself, not Immich.")
        im = Immich(url, key)
        info = im.connect()
        self.immich, self.immich_info = im, info
        self._reset_engine()
        return info

    def sign_in(self, apple_id: str, password: str) -> str:
        if not apple_id.strip() or not password:
            raise ValueError("Enter both your Apple ID (usually your email address) and your password.")
        if self.icloud:
            self.icloud.close()
        self.icloud = ICloud()
        result = self.icloud.sign_in(apple_id.strip(), password)
        if result == "ok":
            self._signed_in()
        else:
            self.icloud_status = "needs_2fa"
        return result

    def code(self, code: str) -> bool:
        if not self.icloud or self.icloud_status != "needs_2fa":
            raise ValueError("Sign in first.")
        if not self.icloud.verify_code(code):
            return False
        self._signed_in()
        return True

    def _signed_in(self) -> None:
        assert self.icloud is not None
        self.library_count = self.icloud.check_photos_access()
        self.icloud_status = "signed_in"
        self._reset_engine()

    def _reset_engine(self) -> None:
        if not (self.immich and self.icloud and self.icloud_status == "signed_in"):
            return
        if self.state:
            self.state.close()
        self.state = State(self.icloud.apple_id or "", self.immich.url)
        self.engine = Engine(self.icloud, self.immich, self.state)
        self.scanned = False

    def need_engine(self) -> Engine:
        if not self.engine:
            raise ValueError("Connect Immich and sign in to iCloud first.")
        return self.engine

    def shutdown(self) -> None:
        if self.engine:
            self.engine.stop()
        if self.icloud:
            self.icloud.close()
        if self.state:
            self.state.close()

    # ---------- view ----------
    def snapshot(self) -> dict:
        eng = self.engine
        out = {
            "version": __version__,
            "mode": self.mode,
            # earlier progress, shown as "welcome back" until iCloud is signed in
            "past": past_runs() if self.icloud_status != "signed_in" else [],
            "immich": {"connected": bool(self.immich_info), **(self.immich_info or {}),
                       "url": self.immich.url if self.immich else None},
            "icloud": {"status": self.icloud_status,
                       "apple_id": self.icloud.apple_id if self.icloud else None,
                       "library": self.library_count,
                       "delivery": self.icloud.delivery_method() if self.icloud else None},
            "scope": vars(self.scope),
            "scanned": self.scanned,
            "busy": bool(eng and eng.busy),
            "progress": None, "summary": None, "failures": [],
        }
        if eng:
            p = eng.progress
            elapsed = max(time.time() - p.started, 0.001) if p.started else 0
            out["progress"] = {**vars(p), "log": p.log[-60:], "elapsed": elapsed,
                               "rate": p.bytes_done / elapsed if elapsed else 0}
            if p.job == "scanning" and p.phase == "done" and not self.scanned and eng.items:
                self.scanned = True
            out["scanned"] = self.scanned
            if self.scanned:
                out["summary"] = eng.summary(self.scope)
                out["failures"] = self.state.failures(50) if self.state else []
        return out


APP = App()


def _load_token(network: bool) -> str:
    """The access key that every page request must carry.

    - ICLOUD2IMMICH_TOKEN set: use it (lets an admin choose the key).
    - Server/Docker mode: create one on first start and keep it in the data
      folder, so the link printed in the log stays the same across restarts.
    - Desktop mode: a fresh key each launch; the app opens the browser itself.
    """
    fixed = os.environ.get("ICLOUD2IMMICH_TOKEN", "").strip()
    if fixed:
        if len(fixed) < 24:
            sys.exit("ICLOUD2IMMICH_TOKEN must be at least 24 characters long.")
        return fixed
    if network:
        f = data_dir() / "access-key.txt"
        try:
            saved = f.read_text().strip()
            if len(saved) >= 24:
                return saved
        except OSError:
            pass
        key = secrets.token_urlsafe(32)
        f.write_text(key + "\n")
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
        return key
    return secrets.token_urlsafe(24)


TOKEN = ""  # set in serve()
# True when serving on a network interface (Docker). The token is then the only
# gate, so the Host-header loopback check is skipped.
NETWORK_MODE = False


class Handler(BaseHTTPRequestHandler):
    server_version = "iCloudToImmich"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # ---------- helpers ----------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def _host_ok(self) -> bool:
        if NETWORK_MODE:
            return True
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _authed(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    # ---------- routes ----------
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        path = self.path.split("?")[0]
        if path == "/api/state":
            if not self._authed():
                return self._json(403, {"error": "bad token"})
            APP.last_seen = time.time()
            with APP.lock:
                return self._json(200, APP.snapshot())
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        f = (static_dir() / name).resolve()
        if static_dir().resolve() not in f.parents or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype)

    def do_POST(self):
        if not self._host_ok() or not self._authed():
            return self._json(403, {"error": "forbidden"})
        APP.last_seen = time.time()
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})
        route = self.path.split("?")[0]
        try:
            with APP.lock:
                result = self.dispatch(route, body)
            self._json(200, result if result is not None else {"ok": True})
        except (ImmichError, ICloudError, ValueError, RuntimeError) as e:
            self._json(400, {"error": str(e)})
        except Exception as e:  # pragma: no cover - last-resort guard
            self._json(500, {"error": f"Unexpected error: {e}"})
        if route == "/api/quit":
            threading.Thread(target=_exit_soon, daemon=True).start()

    def dispatch(self, route: str, b: dict):
        if route == "/api/immich":
            return APP.connect_immich(b.get("url", ""), b.get("api_key", ""))
        if route == "/api/icloud/signin":
            return {"result": APP.sign_in(b.get("apple_id", ""), b.get("password", ""))}
        if route == "/api/icloud/code":
            if not APP.code(str(b.get("code", ""))):
                raise ValueError("That code didn't work. Check it and try again.")
            return {"result": "ok"}
        if route == "/api/scan":
            APP.scanned = False
            APP.need_engine().start_scan()
            return None
        if route == "/api/scope":
            s = Scope(mode=b.get("mode", "all"), days=int(b.get("days") or 90),
                      start=b.get("start") or None, end=b.get("end") or None)
            if s.mode not in ("all", "older_than", "range"):
                raise ValueError("Unknown scope.")
            APP.scope = s
            return APP.need_engine().summary(s)
        if route == "/api/transfer":
            APP.need_engine().start_transfer(APP.scope)
            return None
        if route == "/api/stop":
            APP.need_engine().stop()
            return None
        if route == "/api/delete/preview":
            items = APP.need_engine().deletable(APP.scope)
            return {"count": len(items), "bytes": sum(i.total_size for i in items)}
        if route == "/api/delete":
            if str(b.get("confirm", "")).strip().upper() != "DELETE":
                raise ValueError('Type DELETE to confirm.')
            APP.need_engine().start_delete(APP.scope, int(b.get("count", -1)))
            return None
        if route == "/api/quit":
            APP.shutdown()
            return None
        raise ValueError("Unknown action.")


# ---------- desktop single-instance ----------
# Double-clicking the app again (e.g. after closing the browser tab) should reopen
# the running copy, not start a second one with its own sign-in.

def _running_file() -> Path:
    return data_dir() / "running.json"


def _existing_instance() -> str | None:
    """URL of an already-running desktop copy that answers, else None."""
    try:
        info = json.loads(_running_file().read_text())
        url = f"http://127.0.0.1:{int(info['port'])}"
        req = urllib.request.Request(url + "/api/state", headers={"X-Token": info["token"]})
        with urllib.request.urlopen(req, timeout=2) as r:
            if r.status == 200:
                return f"{url}/#t={info['token']}"
    except Exception:
        pass
    return None


def _forget_instance() -> None:
    if not NETWORK_MODE:
        try:
            _running_file().unlink()
        except OSError:
            pass


def _exit_now() -> None:
    APP.shutdown()
    _forget_instance()
    os._exit(0)


def _exit_soon() -> None:
    time.sleep(0.5)
    _exit_now()


def _watchdog() -> None:
    while True:
        time.sleep(30)
        busy = APP.engine is not None and APP.engine.busy
        if not busy and time.time() - APP.last_seen > IDLE_EXIT_SECONDS:
            print("No activity for 20 minutes: signed out of iCloud"
                  + (" and restarting." if NETWORK_MODE else " and closed."), flush=True)
            _exit_now()


def _say(*lines: str) -> None:
    for line in lines:
        print(line, flush=True)


def serve(open_browser: bool = True, port: int = 0, host: str = "127.0.0.1",
          idle_exit: bool = True, server_mode: bool | None = None) -> None:
    """server_mode defaults to "listening beyond this computer" (Docker)."""
    global NETWORK_MODE, TOKEN
    NETWORK_MODE = host not in ("127.0.0.1", "localhost") if server_mode is None else server_mode
    APP.mode = "server" if NETWORK_MODE else "desktop"

    if not NETWORK_MODE:
        existing = _existing_instance()
        if existing:
            _say("iCloud to Immich is already running. Opening it in your browser.")
            if open_browser:
                webbrowser.open(existing)
            return

    TOKEN = _load_token(NETWORK_MODE)
    httpd = ThreadingHTTPServer((host, port), Handler)
    bar = "=" * 70
    if NETWORK_MODE:
        base = os.environ.get("ICLOUD2IMMICH_URL", "").strip().rstrip("/")
        link = f"{base}/#t={TOKEN}" if base else f"http://YOUR-SERVER-ADDRESS:PORT/#t={TOKEN}"
        _say(bar, f"  iCloud to Immich {__version__} is running.", "",
             "  Open this link in your web browser:", "", f"  {link}", "")
        if not base:
            _say("  Replace YOUR-SERVER-ADDRESS:PORT with this server's address and the",
                 "  port you chose (for example 192.168.1.10:8120).", "")
        _say("  Bookmark it. The link stays the same every time, and it works like a",
             "  password: anyone on your network with it can use the app.", bar)
    else:
        url = f"http://127.0.0.1:{httpd.server_port}/#t={TOKEN}"
        try:
            _running_file().write_text(json.dumps({"port": httpd.server_port, "token": TOKEN}))
        except OSError:
            pass
        _say(bar, f"  iCloud to Immich {__version__} is running.", "",
             "  Your web browser should open by itself. If it doesn't, copy this",
             "  link into your browser:", "", f"  {url}", "",
             "  Leave this window open while you use the app.",
             "  When you're finished, click 'Sign out and close' on the web page.", bar)
        if open_browser:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    if idle_exit:
        threading.Thread(target=_watchdog, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        APP.shutdown()
        _forget_instance()
