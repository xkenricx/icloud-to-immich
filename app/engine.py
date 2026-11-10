"""The transfer engine: scope -> copy + verify -> albums -> (optional) delete.

Safety rule, enforced in `deletable()` and re-checked against Immich right before
each delete: an iCloud item may be deleted only when EVERY file it has (photo,
Live Photo video, edited version) is in Immich, not in the trash, and byte-for-byte
identical (SHA-1) to what was downloaded from iCloud.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .icloud import ICloudError, ICloudSessionExpired, Item
from .immich import ImmichError

ROLE_ORDER = {"live_video": 0, "original": 1, "edited": 2}
MAX_FAILURES_IN_A_ROW = 10


@dataclass
class Scope:
    mode: str = "all"            # all | older_than | range
    days: int = 90
    start: str | None = None     # YYYY-MM-DD
    end: str | None = None

    def matches(self, item: Item) -> bool:
        taken = item.taken if item.taken.tzinfo else item.taken.replace(tzinfo=timezone.utc)
        if self.mode == "older_than":
            return taken < datetime.now(timezone.utc) - timedelta(days=int(self.days))
        if self.mode == "range":
            day = taken.date().isoformat()
            return (not self.start or day >= self.start) and (not self.end or day <= self.end)
        return True


@dataclass
class Progress:
    phase: str = "idle"          # idle | scanning | transferring | albums | deleting | done | stopped | error
    job: str = ""                # which job this progress belongs to: scanning | transferring | deleting
    done: int = 0
    total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    current: str = ""
    message: str = ""
    started: float = 0.0
    counts: dict = field(default_factory=dict)
    log: list = field(default_factory=list)

    def note(self, text: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')}  {text}")
        del self.log[:-200]


class Engine:
    def __init__(self, icloud, immich, state) -> None:
        self.icloud, self.immich, self.state = icloud, immich, state
        self.items: list[Item] = []
        self.progress = Progress()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.session_expired = False   # set when Apple ended the sign-in mid-job

    # ---------- background job plumbing ----------
    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self, phase: str, fn: Callable[[], None]) -> None:
        if self.busy:
            raise RuntimeError("Something is already running.")
        self._stop.clear()
        self.progress = Progress(phase=phase, job=phase, started=time.time(), log=self.progress.log)

        def wrapper():
            try:
                fn()
                if self._stop.is_set():
                    self.progress.phase = "stopped"
                    self.progress.note("Stopped. Run again to pick up where it left off.")
                else:
                    self.progress.phase = "done"
            except (ICloudError, ImmichError) as e:
                self.progress.phase, self.progress.message = "error", str(e)
                self.progress.note(f"Error: {e}")
            except Exception as e:  # unexpected: keep the app alive and report
                self.progress.phase, self.progress.message = "error", f"Unexpected error: {e}"
                self.progress.note(traceback.format_exc(limit=3))
            self.progress.current = ""

        self._thread = threading.Thread(target=wrapper, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ---------- scan ----------
    def start_scan(self) -> None:
        def job():
            def cb(n, total):
                self.progress.done, self.progress.total = n, total
            self.items = self.icloud.scan(cb)
            for it in self.items:
                self.state.upsert_item(it)
            self.progress.note(f"Found {len(self.items)} items.")
        self._run("scanning", job)

    def summary(self, scope: Scope) -> dict:
        statuses = self.state.statuses()
        chosen = [i for i in self.items if scope.matches(i)]
        by = lambda s: [i for i in chosen if statuses.get(i.id, "pending") == s]  # noqa: E731
        albums = sorted({a for i in chosen for a in i.albums})
        return {
            "library": len(self.items),
            "in_scope": len(chosen),
            "bytes": sum(i.total_size for i in chosen),
            "photos": sum(1 for i in chosen if i.kind == "image"),
            "videos": sum(1 for i in chosen if i.kind == "video"),
            "live": sum(1 for i in chosen if any(p.role == "live_video" for p in i.parts)),
            "edited": sum(1 for i in chosen if any(p.role == "edited" for p in i.parts)),
            "albums": albums,
            "verified": len(by("verified")),
            "failed": len(by("failed")),
            "deleted": len(by("deleted")),
            "pending": len(by("pending")),
            "first": chosen[0].taken.date().isoformat() if chosen else None,
            "last": chosen[-1].taken.date().isoformat() if chosen else None,
            # whole-library span, so the page can offer only years that have photos
            "library_first": self.items[0].taken.date().isoformat() if self.items else None,
            "library_last": self.items[-1].taken.date().isoformat() if self.items else None,
        }

    # ---------- transfer ----------
    def start_transfer(self, scope: Scope) -> None:
        def job():
            todo = [i for i in self.items if scope.matches(i)
                    and self.state.status(i.id) not in ("verified", "deleted")]
            p = self.progress
            p.total, p.bytes_total = len(todo), sum(i.total_size for i in todo)
            p.counts = {"verified": 0, "failed": 0}
            p.note(f"Copying {len(todo)} items to Immich.")
            work = Path(tempfile.mkdtemp(prefix="icloud2immich-work-"))
            in_a_row = 0
            try:
                for item in todo:
                    if self._stop.is_set():
                        break
                    p.current = item.filename
                    try:
                        self.transfer_item(item, work)
                        p.counts["verified"] += 1
                        in_a_row = 0
                    except ICloudSessionExpired:
                        # Not this item's fault: stop cleanly so the user can sign in
                        # again, instead of marking everything left as failed.
                        self.session_expired = True
                        raise
                    except Exception as e:  # one bad item must never stop the whole copy
                        msg = str(e) if isinstance(e, (ICloudError, ImmichError)) \
                            else f"Unexpected problem: {e}"
                        self.state.set_status(item.id, "failed", msg)
                        p.counts["failed"] += 1
                        p.note(f"{item.filename}: {msg}")
                        in_a_row += 1
                        if in_a_row >= MAX_FAILURES_IN_A_ROW:
                            raise ICloudError(
                                f"Paused after {in_a_row} items in a row didn't copy. That usually "
                                "means iCloud, Immich or the network is having trouble. Check that "
                                f"Immich is up, then click Continue. Last problem: {msg}")
                    p.done += 1
                    p.bytes_done += item.total_size
            finally:
                shutil.rmtree(work, ignore_errors=True)
            if not self._stop.is_set():
                self.progress.phase = "albums"
                self.sync_albums(scope)
        self._run("transferring", job)

    def transfer_item(self, item: Item, work: Path) -> None:
        ids: dict[str, str] = {}
        visibility = "archive" if item.hidden else None
        for part in sorted(item.parts, key=lambda x: ROLE_ORDER[x.role]):
            row = self.state.part(item.id, part.role)
            if row is not None and row["verified"] and row["immich_id"]:
                ids[part.role] = row["immich_id"]
                continue
            dest = work / f"{item.id[:16]}_{part.role}"
            try:
                size, sha1 = self.icloud.download(item.id, part.role, dest)
                if part.size and size != part.size:
                    raise ICloudError(f"Download of {part.filename} didn't match the size iCloud "
                                      f"reported ({size} vs {part.size} bytes).")
                if part.role == "live_video":
                    aid, status = self.immich.upload(dest, part.filename, item.taken, visibility="hidden")
                elif part.role == "original":
                    aid, status = self.immich.upload(
                        dest, part.filename, item.taken, favorite=item.favorite,
                        visibility=visibility, live_video_id=ids.get("live_video"))
                    if status == "duplicate" and "live_video" in ids:
                        self.immich.link_live(aid, ids["live_video"])
                else:
                    aid, status = self.immich.upload(dest, part.filename, item.taken,
                                                     favorite=item.favorite, visibility=visibility)
            finally:
                dest.unlink(missing_ok=True)
            ok = self.immich.verify(aid, sha1)
            self.state.set_part(item.id, part.role, sha1, aid, ok)
            if not ok:
                raise ImmichError(f"Immich's copy of {part.filename} doesn't match iCloud's "
                                  f"(or it is in the Immich trash).")
            ids[part.role] = aid

        primary = ids["original"]
        if "edited" in ids:
            try:
                self.immich.stack(ids["edited"], ids["original"])
                primary = ids["edited"]
            except ImmichError as e:
                self.progress.note(f"{item.filename}: copied, but couldn't stack edit with original ({e})")
        self.state.set_status(item.id, "verified", None, primary_id=primary)

    # ---------- albums ----------
    def sync_albums(self, scope: Scope) -> None:
        statuses = self.state.statuses()
        wanted: dict[str, list[str]] = {}
        for item in self.items:
            if not item.albums or not scope.matches(item):
                continue
            if statuses.get(item.id) not in ("verified", "deleted"):
                continue
            pid = self.state.primary_id(item.id)
            if pid:
                for name in item.albums:
                    wanted.setdefault(name, []).append(pid)
        if not wanted:
            return
        existing = self.immich.albums()
        for name, ids in wanted.items():
            self.progress.current = f"Album: {name}"
            if name in existing:
                self.immich.add_to_album(existing[name], ids)
            else:
                self.immich.create_album(name, ids)
            self.progress.note(f"Album '{name}': {len(ids)} items")

    # ---------- delete ----------
    def deletable(self, scope: Scope) -> list[Item]:
        """Items in scope whose every file is recorded as verified in Immich."""
        out = []
        for item in self.items:
            if not scope.matches(item) or self.state.status(item.id) != "verified":
                continue
            rows = {r["role"]: r for r in self.state.parts(item.id)}
            if all(p.role in rows and rows[p.role]["verified"] and rows[p.role]["sha1"]
                   and rows[p.role]["immich_id"] for p in item.parts):
                out.append(item)
        return out

    def start_delete(self, scope: Scope, expected_count: int) -> None:
        candidates = self.deletable(scope)
        if len(candidates) != expected_count:
            raise RuntimeError("The number of items to delete changed. Review it again.")

        def job():
            p = self.progress
            p.total = len(candidates)
            p.counts = {"deleted": 0, "skipped": 0, "failed": 0}
            p.note("Re-checking every file against Immich before deleting.")
            def rows_for(item):
                roles = {pt.role for pt in item.parts}
                return [r for r in self.state.parts(item.id) if r["role"] in roles]

            keys = [(f"{item.id}|{r['role']}", r["sha1"]) for item in candidates for r in rows_for(item)]
            present = self.immich.check_present(keys)
            for item in candidates:
                if self._stop.is_set():
                    break
                p.current = item.filename
                rows = rows_for(item)
                safe = len(rows) == len(item.parts) and all(
                    present.get(f"{item.id}|{r['role']}") == r["immich_id"] for r in rows)
                if not safe:
                    p.counts["skipped"] += 1
                    self.state.set_status(item.id, "pending", "No longer verified in Immich; not deleted.")
                    p.note(f"{item.filename}: not in Immich any more, kept in iCloud.")
                else:
                    try:
                        self.icloud.delete(item.id)
                        self.state.mark_deleted(item.id)
                        p.counts["deleted"] += 1
                    except ICloudSessionExpired:
                        self.session_expired = True
                        raise
                    except Exception as e:
                        p.counts["failed"] += 1
                        p.note(f"{item.filename}: delete failed ({e})")
                p.done += 1
        self._run("deleting", job)
