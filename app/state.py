"""Per-run progress on disk (SQLite) so an interrupted transfer resumes where it stopped.

One database per (Apple ID, Immich server) pair, under %LOCALAPPDATA%\\iCloudToImmich\\runs.
Holds only ids, filenames, sizes, checksums and statuses: never media or credentials.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY, filename TEXT, taken TEXT, kind TEXT, total_size INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | verified | failed | deleted
  error TEXT, primary_id TEXT, deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS parts (
  item_id TEXT, role TEXT, filename TEXT, size INTEGER, sha1 TEXT, immich_id TEXT,
  verified INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (item_id, role)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def mask_email(s: str) -> str:
    """'alice@example.com' -> 'a•••@example.com' (enough to recognise, not to read off)."""
    name, at, domain = s.partition("@")
    return f"{name[:1]}•••{at}{domain}" if at else (s[:1] + "•••" if s else "")


def past_runs(only: set[str] | None = None) -> list[dict]:
    """Progress from earlier runs, so the page can say "welcome back" before sign-in.

    `only` limits it to run ids this browser has taken part in, so on a shared
    server one person never sees another person's progress. None = all runs
    (desktop mode, where it's one person's own computer).
    """
    out = []
    runs = data_dir() / "runs"
    if not runs.is_dir():
        return out
    for f in sorted(runs.glob("*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True):
        if only is not None and f.stem not in only:
            continue
        try:
            db = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
            counts = dict(db.execute("SELECT status, count(*) FROM items GROUP BY status").fetchall())
            try:
                meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
            except sqlite3.OperationalError:
                meta = {}
            db.close()
        except sqlite3.Error:
            continue
        copied = counts.get("verified", 0) + counts.get("deleted", 0)
        if not copied:
            continue
        out.append({
            "run_id": f.stem,
            "account": meta.get("account", ""),
            "immich": meta.get("immich", ""),
            "copied": copied,
            "remaining": counts.get("pending", 0) + counts.get("failed", 0),
            "freed": counts.get("deleted", 0),
            "when": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat(),
        })
    return out


def data_dir() -> Path:
    override = os.environ.get("ICLOUD2IMMICH_DATA")   # set in the Docker image
    if override:
        d = Path(override)
        d.mkdir(parents=True, exist_ok=True)
        return d
    if os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".local" / "share"
    d = base / "iCloudToImmich"
    d.mkdir(parents=True, exist_ok=True)
    return d


class State:
    def __init__(self, apple_id: str, immich_url: str, path: Path | None = None) -> None:
        if path is None:
            key = hashlib.sha256(f"{apple_id.lower()}|{immich_url.lower()}".encode()).hexdigest()[:16]
            runs = data_dir() / "runs"
            runs.mkdir(exist_ok=True)
            path = runs / f"{key}.sqlite"
        self.path = path
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # Only a masked hint of the account, for the "welcome back" message.
        self.db.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                            [("account", mask_email(apple_id)), ("immich", immich_url)])
        self.db.commit()

    def _x(self, sql: str, args=()):
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    # items
    def upsert_item(self, item) -> None:
        self._x("INSERT INTO items(id, filename, taken, kind, total_size) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET filename=excluded.filename, taken=excluded.taken, "
                "total_size=excluded.total_size",
                (item.id, item.filename, item.taken.isoformat(), item.kind, item.total_size))
        # A photo can change after it was copied (edited in iCloud, a Live Photo's video
        # added). Any new or changed file sends the item back to "to copy", so Continue
        # copies just that file, and it can't be deleted until that file is in Immich too.
        changed = False
        for p in item.parts:
            row = self._q("SELECT size FROM parts WHERE item_id=? AND role=?", (item.id, p.role))
            if not row:
                self._x("INSERT INTO parts(item_id, role, filename, size) VALUES (?,?,?,?)",
                        (item.id, p.role, p.filename, p.size))
                changed = True
            elif p.size is not None and row[0]["size"] != p.size:
                self._x("UPDATE parts SET filename=?, size=?, sha1=NULL, verified=0 "
                        "WHERE item_id=? AND role=?", (p.filename, p.size, item.id, p.role))
                changed = True
        if changed:
            self._x("UPDATE items SET status='pending', error=NULL "
                    "WHERE id=? AND status IN ('verified', 'failed')", (item.id,))

    def status(self, item_id: str) -> str:
        rows = self._q("SELECT status FROM items WHERE id=?", (item_id,))
        return rows[0]["status"] if rows else "pending"

    def statuses(self) -> dict[str, str]:
        return {r["id"]: r["status"] for r in self._q("SELECT id, status FROM items")}

    def set_status(self, item_id: str, status: str, error: str | None = None,
                   primary_id: str | None = None) -> None:
        if primary_id is not None:
            self._x("UPDATE items SET status=?, error=?, primary_id=? WHERE id=?",
                    (status, error, primary_id, item_id))
        else:
            self._x("UPDATE items SET status=?, error=? WHERE id=?", (status, error, item_id))

    def mark_deleted(self, item_id: str) -> None:
        self._x("UPDATE items SET status='deleted', deleted_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), item_id))

    def primary_id(self, item_id: str) -> str | None:
        rows = self._q("SELECT primary_id FROM items WHERE id=?", (item_id,))
        return rows[0]["primary_id"] if rows else None

    # parts
    def part(self, item_id: str, role: str) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM parts WHERE item_id=? AND role=?", (item_id, role))
        return rows[0] if rows else None

    def parts(self, item_id: str) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM parts WHERE item_id=?", (item_id,))

    def set_part(self, item_id: str, role: str, sha1: str, immich_id: str, verified: bool) -> None:
        self._x("UPDATE parts SET sha1=?, immich_id=?, verified=? WHERE item_id=? AND role=?",
                (sha1, immich_id, int(verified), item_id, role))

    def failures(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT filename, taken, error FROM items WHERE status='failed' ORDER BY taken LIMIT ?", (limit,))]

    def close(self) -> None:
        with self._lock:
            self.db.close()
