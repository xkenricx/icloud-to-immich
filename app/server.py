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
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .engine import Engine, Scope
from .icloud import ICloud, ICloudError
from .immich import Immich, ImmichError
from .state import State

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

    # ---------- actions ----------
    def connect_immich(self, url: str, key: str) -> dict:
        if self.engine and self.engine.busy:
            raise ValueError("Wait for the current step to finish.")
        im = Immich(url, key)
        info = im.connect()
        self.immich, self.immich_info = im, info
        self._reset_engine()
        return info

    def sign_in(self, apple_id: str, password: str) -> str:
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
def _load_token() -> str:
    """Per-launch random key, or a fixed one from ICLOUD2IMMICH_TOKEN so a
    long-running server keeps the same bookmarkable link across restarts."""
    fixed = os.environ.get("ICLOUD2IMMICH_TOKEN", "").strip()
    if fixed:
        if len(fixed) < 24:
            sys.exit("ICLOUD2IMMICH_TOKEN must be at least 24 characters "
                     "(try: python -c \"import secrets; print(secrets.token_urlsafe(32))\").")
        return fixed
    return secrets.token_urlsafe(24)


TOKEN = _load_token()
TOKEN_FIXED = bool(os.environ.get("ICLOUD2IMMICH_TOKEN", "").strip())
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


def _exit_soon() -> None:
    time.sleep(0.5)
    os._exit(0)


def _watchdog() -> None:
    while True:
        time.sleep(30)
        busy = APP.engine is not None and APP.engine.busy
        if not busy and time.time() - APP.last_seen > IDLE_EXIT_SECONDS:
            print("No activity for a while; signing out and closing.")
            APP.shutdown()
            os._exit(0)


def serve(open_browser: bool = True, port: int = 0, host: str = "127.0.0.1",
          idle_exit: bool = True) -> None:
    global NETWORK_MODE
    NETWORK_MODE = host not in ("127.0.0.1", "localhost")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"iCloud to Immich {__version__} is running.", flush=True)
    if NETWORK_MODE:
        print("Open this address in your browser, replacing SERVER with this machine's IP or name"
              " (and the port, if you mapped a different one):", flush=True)
        if TOKEN_FIXED:  # don't echo a long-lived key into logs
            print(f"  http://SERVER:{httpd.server_port}/#t=<your ICLOUD2IMMICH_TOKEN>", flush=True)
        else:
            print(f"  http://SERVER:{httpd.server_port}/#t={TOKEN}", flush=True)
        print("Anyone with this link can use the app, so don't share it.", flush=True)
    else:
        url = f"http://127.0.0.1:{httpd.server_port}/#t={TOKEN}"
        print(f"If your browser didn't open, paste this address into it:\n  {url}", flush=True)
        print("Keep this window open until you're finished. Closing it stops the app.", flush=True)
    if idle_exit:
        threading.Thread(target=_watchdog, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_port}/#t={TOKEN}"
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        APP.shutdown()
