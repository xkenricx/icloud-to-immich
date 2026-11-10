"""The web server that drives the wizard page.

Desktop mode binds to 127.0.0.1 with a per-launch access key. Server mode
(Docker) listens on the network with a long-lived access key.

Every browser gets its own private *session* (an `App`): its own Immich
sign-in, iCloud sign-in, copy and idle timer. So several people in a household
can use one server at the same time without seeing or touching each other's
accounts. The browser keeps a random session id and sends it as X-Session.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
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

IDLE_EXIT_SECONDS = 20 * 60        # a session with nobody watching and nothing running is signed out
MAX_SESSIONS = 25                  # plenty for a household; stops runaway session creation
SID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def static_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "app" / "static"


# One iCloud account can only be moved by one session at a time: two copies of
# the same library racing each other would make a mess of the progress file.
RUN_OWNERS: dict[str, str] = {}    # run id -> session id
RUN_LOCK = threading.Lock()


class App:
    """One person's session."""

    def __init__(self, sid: str = "local") -> None:
        self.sid = sid
        self.lock = threading.Lock()
        self.immich: Immich | None = None
        self.immich_info: dict | None = None
        self.icloud: ICloud | None = None
        self.icloud_status = "signed_out"   # signed_out | needs_2fa | signed_in
        self.library_count: int | None = None
        self.engine: Engine | None = None
        self.state: State | None = None
        self.run_id: str | None = None
        self.scope = Scope()
        self.scanned = False
        self.last_seen = time.time()
        self.mode = MODE

    # ---------- actions ----------
    def connect_immich(self, url: str, key: str = "", email: str = "", password: str = "") -> dict:
        if self.engine and self.engine.busy:
            raise ValueError("Wait for the current step to finish.")
        if not url.strip():
            raise ValueError("Enter your Immich address: the one you type in your browser to open Immich.")
        use_key = bool(key.strip())
        if not use_key and not (email.strip() and password):
            raise ValueError("Enter the email and password you use to sign in to Immich.")
        host = (urllib.parse.urlsplit(url if "://" in url else "http://" + url).hostname or "").lower()
        if self.mode == "server" and host in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Use your Immich server's network address (like 192.168.1.10:2283), not "
                             "'localhost'. This app runs in its own container, so 'localhost' "
                             "means the app itself, not Immich.")
        im = Immich(url, key if use_key else None)
        if not use_key:
            im.login(email, password)
        try:
            info = im.connect()
        except Exception:
            im.logout()
            raise
        if self.immich and self.immich is not im:
            self.immich.logout()             # switching accounts: end the old Immich session
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
        try:
            self._reset_engine()
        except ValueError:
            # Same iCloud account already being moved in another browser.
            self.icloud.close()
            self.icloud, self.icloud_status = None, "signed_out"
            raise

    def _reset_engine(self) -> None:
        if not (self.immich and self.icloud and self.icloud_status == "signed_in"):
            return
        state = State(self.icloud.apple_id or "", self.immich.url)
        run_id = state.path.stem
        with RUN_LOCK:
            owner = RUN_OWNERS.get(run_id)
            if owner and owner != self.sid and owner in SESSIONS:
                state.close()
                raise ValueError("This iCloud account is already being moved in another browser. "
                                 "Use that browser, or sign out there first.")
            self._release_run()
            RUN_OWNERS[run_id] = self.sid
        if self.state:
            self.state.close()
        self.state, self.run_id = state, run_id
        self.engine = Engine(self.icloud, self.immich, self.state)
        self.scanned = False

    def _release_run(self) -> None:
        if self.run_id and RUN_OWNERS.get(self.run_id) == self.sid:
            RUN_OWNERS.pop(self.run_id, None)

    def need_engine(self) -> Engine:
        if not self.engine:
            raise ValueError("Connect Immich and sign in to iCloud first.")
        return self.engine

    @property
    def busy(self) -> bool:
        return bool(self.engine and self.engine.busy)

    def shutdown(self) -> None:
        """Sign this session out of everything and forget it."""
        if self.engine:
            self.engine.stop()
        if self.icloud:
            self.icloud.close()
        if self.immich:
            self.immich.logout()
        if self.state:
            self.state.close()
        with RUN_LOCK:
            self._release_run()
        self.icloud_status = "signed_out"

    # ---------- view ----------
    def snapshot(self, my_runs: set[str] | None = None) -> dict:
        eng = self.engine
        if eng and eng.session_expired and not eng.busy and self.icloud_status == "signed_in":
            # Apple ended the sign-in mid-copy: show the sign-in form again. The
            # engine's message ("sign in again, then Continue") stays in the log.
            self.icloud_status = "signed_out"
        # Welcome back: desktop shows this computer's runs; a shared server only
        # the runs this browser took part in, so nobody sees anyone else's.
        past = []
        if self.icloud_status != "signed_in":
            past = past_runs(None if self.mode == "desktop" else (my_runs or set()))
        out = {
            "version": __version__,
            "mode": self.mode,
            "past": past,
            "run_id": self.run_id,
            # Optional household default so nobody has to type the Immich address.
            "default_immich_url": os.environ.get("ICLOUD2IMMICH_IMMICH_URL", "").strip(),
            "immich": {"connected": bool(self.immich_info), **(self.immich_info or {}),
                       "url": self.immich.url if self.immich else None,
                       "with_password": bool(self.immich and self.immich.signed_in_with_password)},
            "icloud": {"status": self.icloud_status,
                       "apple_id": self.icloud.apple_id if self.icloud else None,
                       "library": self.library_count,
                       "delivery": self.icloud.delivery_method() if self.icloud else None},
            "scope": vars(self.scope),
            "scanned": self.scanned,
            "busy": self.busy,
            "progress": None, "summary": None, "failures": [],
        }
        if eng:
            p = eng.progress
            elapsed = max(time.time() - p.started, 0.001) if p.started else 0
            out["progress"] = {**{k: v for k, v in vars(p).items() if k != "samples"},
                               "log": p.log[-60:], "elapsed": elapsed, "rate": p.recent_rate()}
            if p.job == "scanning" and p.phase == "done" and not self.scanned and eng.items:
                self.scanned = True
            out["scanned"] = self.scanned
            if self.scanned:
                out["summary"] = eng.summary(self.scope)
                out["failures"] = self.state.failures(50) if self.state else []
        return out


# ---------- sessions ----------
MODE = "desktop"                    # desktop | server, set in serve()
SESSIONS: dict[str, App] = {}
SESSIONS_LOCK = threading.RLock()   # re-entrant: get_session() may prune while holding it


def get_session(sid: str) -> App:
    if not SID_RE.match(sid or ""):
        raise PermissionError("missing session")
    with SESSIONS_LOCK:
        app = SESSIONS.get(sid)
        if app is None:
            if len(SESSIONS) >= MAX_SESSIONS:
                _prune(force_oldest=True)
            app = SESSIONS[sid] = App(sid)
        app.last_seen = time.time()
        return app


def end_session(sid: str) -> None:
    with SESSIONS_LOCK:
        app = SESSIONS.pop(sid, None)
    if app:
        app.shutdown()


def _prune(force_oldest: bool = False) -> int:
    """Sign out sessions nobody has looked at for a while (never mid-copy)."""
    now = time.time()
    with SESSIONS_LOCK:
        idle = [s for s, a in SESSIONS.items() if not a.busy and now - a.last_seen > IDLE_EXIT_SECONDS]
        if force_oldest and not idle:
            quiet = [(a.last_seen, s) for s, a in SESSIONS.items() if not a.busy]
            idle = [min(quiet)[1]] if quiet else []
        apps = [SESSIONS.pop(s) for s in idle]
    for a in apps:
        a.shutdown()
    return len(apps)


def overview() -> dict:
    """What's going on overall, without anyone's details (used by update scripts)."""
    with SESSIONS_LOCK:
        apps = list(SESSIONS.values())
    return {"version": __version__, "sessions": len(apps),
            "signed_in": sum(a.icloud_status == "signed_in" for a in apps),
            "busy": sum(a.busy for a in apps)}


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
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path
        if path == "/api/overview":
            if not self._authed():
                return self._json(403, {"error": "bad token"})
            return self._json(200, overview())
        if path == "/api/state":
            if not self._authed():
                return self._json(403, {"error": "bad token"})
            try:
                app = get_session(self.headers.get("X-Session", ""))
            except PermissionError:
                return self._json(400, {"error": "missing session"})
            q = urllib.parse.parse_qs(parts.query)
            mine = {r for r in (q.get("runs", [""])[0]).split(",") if re.fullmatch(r"[0-9a-f]{16}", r)}
            with app.lock:
                return self._json(200, app.snapshot(mine))
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
        try:
            app = get_session(self.headers.get("X-Session", ""))
        except PermissionError:
            return self._json(400, {"error": "missing session"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})
        route = urllib.parse.urlsplit(self.path).path
        if route == "/api/quit":
            end_session(app.sid)
            self._json(200, {"ok": True})
            if MODE == "desktop":   # desktop: one person, one window -> close the app
                threading.Thread(target=_exit_soon, daemon=True).start()
            return
        try:
            with app.lock:
                result = self.dispatch(app, route, body)
            self._json(200, result if result is not None else {"ok": True})
        except (ImmichError, ICloudError, ValueError, RuntimeError) as e:
            self._json(400, {"error": str(e)})
        except Exception as e:  # pragma: no cover - last-resort guard
            self._json(500, {"error": f"Unexpected error: {e}"})

    def dispatch(self, app: App, route: str, b: dict):
        if route == "/api/immich":
            return app.connect_immich(b.get("url", ""), b.get("api_key", ""),
                                      b.get("email", ""), b.get("password", ""))
        if route == "/api/icloud/signin":
            return {"result": app.sign_in(b.get("apple_id", ""), b.get("password", ""))}
        if route == "/api/icloud/code":
            if not app.code(str(b.get("code", ""))):
                raise ValueError("That code didn't work. Check it and try again.")
            return {"result": "ok"}
        if route == "/api/scan":
            app.scanned = False
            app.need_engine().start_scan()
            return None
        if route == "/api/scope":
            s = Scope(mode=b.get("mode", "all"), days=int(b.get("days") or 90),
                      start=b.get("start") or None, end=b.get("end") or None)
            if s.mode not in ("all", "older_than", "range"):
                raise ValueError("Unknown scope.")
            app.scope = s
            return app.need_engine().summary(s)
        if route == "/api/transfer":
            app.need_engine().start_transfer(app.scope)
            return None
        if route == "/api/stop":
            app.need_engine().stop()
            return None
        if route == "/api/delete/preview":
            items = app.need_engine().deletable(app.scope)
            return {"count": len(items), "bytes": sum(i.total_size for i in items)}
        if route == "/api/delete":
            if str(b.get("confirm", "")).strip().upper() != "DELETE":
                raise ValueError('Type DELETE to confirm.')
            app.need_engine().start_delete(app.scope, int(b.get("count", -1)))
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
        req = urllib.request.Request(url + "/api/overview", headers={"X-Token": info["token"]})
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


def _shutdown_all() -> None:
    with SESSIONS_LOCK:
        apps = list(SESSIONS.values())
        SESSIONS.clear()
    for a in apps:
        a.shutdown()


def _exit_now() -> None:
    _shutdown_all()
    _forget_instance()
    os._exit(0)


def _exit_soon() -> None:
    time.sleep(0.5)
    _exit_now()


def _watchdog(desktop_exit: bool) -> None:
    started = time.time()
    while True:
        time.sleep(30)
        n = _prune()
        if n:
            print(f"Signed out {n} session{'s' if n > 1 else ''} after 20 minutes without use.", flush=True)
        # Desktop: when nobody's left, close the app (it's one person's window).
        if desktop_exit and not SESSIONS and time.time() - started > IDLE_EXIT_SECONDS:
            print("No activity for 20 minutes: closed.", flush=True)
            _exit_now()


def _say(*lines: str) -> None:
    for line in lines:
        print(line, flush=True)


def serve(open_browser: bool = True, port: int = 0, host: str = "127.0.0.1",
          idle_exit: bool = True, server_mode: bool | None = None) -> None:
    """server_mode defaults to "listening beyond this computer" (Docker).

    idle_exit: sign out idle sessions (and, on desktop, close when nobody's left).
    """
    global NETWORK_MODE, TOKEN, MODE
    NETWORK_MODE = host not in ("127.0.0.1", "localhost") if server_mode is None else server_mode
    MODE = "server" if NETWORK_MODE else "desktop"

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
        _say("  Bookmark it, and share it with others in your household: everyone",
             "  signs in to their own Immich and iCloud, privately. The link stays the",
             "  same every time, and it works like a password: anyone on your network",
             "  with it can use the app.", bar)
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
    # Idle sessions are always signed out (on a shared server that's what keeps
    # nobody's iCloud sign-in lying around). idle_exit only decides whether the
    # desktop app also closes itself once nobody's using it.
    threading.Thread(target=_watchdog, args=(idle_exit and not NETWORK_MODE,), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown_all()
        _forget_instance()
