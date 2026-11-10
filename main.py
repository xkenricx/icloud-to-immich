"""Entry point: python main.py  (or the packaged iCloudToImmich app / Docker image)."""

import argparse
import multiprocessing

from app.server import serve

if __name__ == "__main__":
    multiprocessing.freeze_support()  # harmless; needed if a frozen build ever spawns
    ap = argparse.ArgumentParser(description="Move photos from iCloud to Immich.")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    ap.add_argument("--port", type=int, default=0, help="fixed port (default: random)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="interface to listen on; use 0.0.0.0 inside Docker (default: this computer only)")
    ap.add_argument("--no-idle-exit", action="store_true",
                    help="keep running when nobody has the page open (Docker)")
    a = ap.parse_args()
    serve(open_browser=not a.no_browser, port=a.port, host=a.host, idle_exit=not a.no_idle_exit)
