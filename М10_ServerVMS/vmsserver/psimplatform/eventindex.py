"""eventindex — the event "database", which is a cache. A platform job's part:
one index per RESOURCE, over that resource's own tree — its buckets and the
copies it holds of its peers' closed buckets (`.mirror/<server>/`). Nothing
cluster-wide: a console with a question fans it out to the live resources
and merges the answers by time (М11's `cluster.console.MergedIndex`); on one
box the console asks the one index its archive has (`vms.console.LocalIndex`).

Events are observations: written by the worker that holds a unit's epoch,
into that unit's bucket on its server's resource (psimplatform.events).
The index holds a SQLite table it can rebuild entirely by re-reading the
tree. It knows which subsystems exist by the directories it finds; a new
one is indexed the pass after it starts writing, with no change here. It
knows nothing about what an event means: `cam` is a field an event may
carry, indexed if present.

Its two properties are the controller's, in the form that matters here:
it holds nothing it cannot rebuild, and nothing running depends on it.
No controller writes events. A restarted resource says *catching up*
until its rebuild is done rather than answering short.

    ResourceIndex(root, server)   the index a resource job runs: rebuilt on start, tailed every few seconds
    rebuild() / tail()            read the tree: closed buckets once, open ones by the lines past what is held
    query(...)                    subsystem, unit, cam (a field an event may carry), kind, time window
    forget(server, paths)         retention removed a bucket: its rows go with it (the resource calls this)
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # eventindex.py — the event "database", which is a cache: one SQLite index per resource, over its own tree
#
# **Role in the module.** Lesson 3, the reader of `events.py`. Events are written by workers into per-unit
# buckets on their server's resource; search needs an index over them, and this is it — but one PER
# RESOURCE, not one per cluster: the resource job runs a `ResourceIndex` over its own root (own buckets and
# the `.mirror/<server>/` copies it holds) and answers `GET /events` from it. A console with a cluster-wide
# question fans out to the live resources and merges (М11 `cluster.console.MergedIndex`); the box's console
# asks its one index. Nothing crosses the network to build an index, and no job holds every server's rows.
# It discovers subsystems and units from the directories, so a new subsystem is indexed the pass after it
# starts writing, with no change here. It knows nothing about what an event means: `cam` is a field an event
# may carry, indexed if present. Its two properties are the controller's in the form that matters here: it
# holds nothing it cannot rebuild, and nothing running depends on it. No controller writes events.
#
# ## Module-level names
# None.
#
# ### `query(self, t0, t1, cam=None, kind=None, subsystem=None, unit=None, current_epochs=None, limit=1000)
# -> dict` Time window plus optional equality filters, ordered by `t`, limited. `current_epochs` is
# `{(subsystem, unit): epoch}` — fencing is per unit, and only the unit's own subsystem knows its current
# epoch; the index just compares and sets `fenced: epoch < current`. Each result row is `{subsystem, unit,
# cam, epoch, t, kind, server, bucket, fenced, **fields}`; the reply is `{events, state}` so a caller can
# tell a short answer during catch-up from a complete one. The console builds `current_epochs` from
# `<sub>/epoch/*`; the resource's `/events` answers unfenced and the console fences the merge.
#
# ## Notes
# - The `cam` column exists so a VMS timeline can ask "everything about camera 7 from any subsystem" without
#   the index knowing what a camera is; the console's `/events?cam=` maps onto it.
# - Nothing is ever updated in place: rows are inserted per bucket (an open bucket by the lines past what is
#   held) and deleted per bucket, matching the resource's file-level retention.
# - Rows from a mirror copy are inserted under the REAL owning server and the original path: a query
#   answers "srv-a's events" whichever resource holds them, and the console drops the copy when the owner
#   itself answers.
# ================================================================================================
from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import threading
import time

from .events import Bucket, buckets_under, read_bucket, subsystems_under
from .resource import MIRROR_DIR, mirrored_buckets, mirrored_servers


# How the index reads a tree: directories, on the resource's own disk. `url` is carried for the shape of
# `tail` (one entry per server) and ignored — a resource never reads another resource to index it.
class DirectoryReader:
    """The index's reader: this resource's tree — own buckets, and the copies under `.mirror/<server>/`."""
    def __init__(self, root: str, bucket_seconds: int = 600):
        self.root, self.bucket_seconds = root, bucket_seconds

    def buckets(self, url: str, sub: str, unit: str) -> list[Bucket]:
        return buckets_under(self.root, sub, unit, self.bucket_seconds)

    def events(self, url: str, b: Bucket) -> list[dict]:
        return read_bucket(os.path.join(self.root, b.path))

    # Copies this resource holds of `server`'s closed buckets; `path` is the ORIGINAL path on `server`.
    def mirrored(self, url: str, server: str) -> list[Bucket]:
        return mirrored_buckets(self.root, server, self.bucket_seconds)

    def mirrored_events(self, url: str, server: str, b: Bucket) -> list[dict]:
        return read_bucket(os.path.join(self.root, MIRROR_DIR, server, b.path))


# The index. State: a SQLite connection (`check_same_thread=False`, so the console's threaded server may
# query it), `state` (`"empty"`, `"catching up"`, `"live[; … unreachable][; … from mirror]"`) and
# `indexed_segments`.
class EventIndex:
    # Creates two tables: `seen (server, path)` primary key — which buckets have been ingested, keyed by the
    # real owning server and the original path; and `events (subsystem, unit, cam, epoch, t, kind, server,
    # path, fields)` with indexes on `(cam, t)`, `(subsystem, unit, t)` and `(kind, t)`. `fields` holds the
    # remaining event keys as JSON. `lost_after` is how old a resource heartbeat may be before the resource
    # counts as silent.
    def __init__(self, reader, path: str = ":memory:", wall=time.time, lost_after: float = 45.0):
        self.reader, self.wall, self.lost_after = reader, wall, lost_after
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS seen   (server TEXT, path TEXT, n INTEGER DEFAULT 0, PRIMARY KEY (server, path));
            CREATE TABLE IF NOT EXISTS events (subsystem TEXT, unit TEXT, cam INTEGER, epoch INTEGER, t REAL, kind TEXT,
                                               server TEXT, path TEXT, fields TEXT);
            CREATE INDEX IF NOT EXISTS events_cam_t ON events (cam, t);
            CREATE INDEX IF NOT EXISTS events_sub_unit_t ON events (subsystem, unit, t);
            CREATE INDEX IF NOT EXISTS events_kind_t ON events (kind, t);""")
        self.state = "empty"
        self.indexed_segments = 0

    # From nothing — what a restarted resource does first: truncate both tables, zero the counter, then
    # `tail(resources, rebuild=True)`.
    def rebuild(self, resources: dict[str, dict]) -> dict:
        """From nothing: what a failed-over instance does first."""
        self.db.executescript("DELETE FROM seen; DELETE FROM events;")
        self.indexed_segments = 0
        return self.tail(resources, rebuild=True)

    # `resources` is `{server: entry}` in a heartbeat's shape (`ResourceIndex.resources()`). Sets `state` to "catching up" on a rebuild.
    # Splits servers into live and silent by `ts`. For a silent server, try `_from_mirror`; if no live peer
    # holds copies, list it as unreachable. For a live server, walk `heartbeat["units"]` and, for each
    # bucket the resource reports, skip it if it has no events or is already in `seen`; otherwise fetch its
    # lines past what is held and insert one row each — `cam` is the event's `cam` field, or the unit id itself when the unit
    # is numeric (a numeric unit is its own `cam`; others may name one) — then mark the bucket seen, all in
    # one transaction per bucket. Any exception while talking to a live server (fresh heartbeat, server not
    # answering) marks it unreachable. Ends by setting `state` to "live" plus the unreachable and
    # from-mirror lists, and returns `{added, unreachable, from_mirror, segments}`. Because only closed
    # buckets are reported by the resource and `seen` is keyed per bucket, tailing is idempotent and each
    # bucket is read once.
    def tail(self, resources: dict[str, dict], rebuild: bool = False) -> dict:
        self.state = "catching up" if rebuild else self.state
        now = self.wall(); added = 0; unreachable = []; from_mirror = []
        live = {s: hb for s, hb in resources.items() if now - float(hb["ts"]) <= self.lost_after}
        for server, hb in sorted(resources.items()):
            if server not in live:
                if self._from_mirror(server, live):
                    from_mirror.append(server); added += self._last_mirror_added
                else:
                    unreachable.append(server)
                continue
            try:
                for sub, units in hb.get("units", {}).items():
                    for unit in units:
                        for b in self.reader.buckets(hb["url"], sub, unit):
                            row = self.db.execute("SELECT n FROM seen WHERE server=? AND path=?", (server, b.path)).fetchone()
                            have = row[0] if row else 0
                            if b.events <= have:                                  # nothing new in this bucket (an open one grows; a closed one never)
                                continue
                            rows = []
                            for e in self.reader.events(hb["url"], b)[have:]:     # buckets are append-only: the lines past what we hold
                                cam = e.get("cam", int(unit) if unit.isdigit() else None)     # a numeric unit is its own `cam`; others may name one
                                rows.append((sub, unit, cam, b.epoch, float(e["t"]), e["kind"], server, b.path,
                                             json.dumps({k: v for k, v in e.items() if k not in ("t", "kind", "cam")})))
                            with self.db:
                                self.db.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", rows)
                                self.db.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?)", (server, b.path, have + len(rows)))
                            added += len(rows); self.indexed_segments += (0 if row else 1)
            except Exception:                          # noqa: BLE001 — fresh heartbeat, server not answering
                unreachable.append(server)
        self.state = "live" + (f"; {', '.join(unreachable)} unreachable" if unreachable else "") \
                            + (f"; {', '.join(from_mirror)} from mirror" if from_mirror else "")
        return {"added": added, "unreachable": unreachable, "from_mirror": from_mirror, "segments": self.indexed_segments}

    # A silent server's closed buckets, from whichever live peer's heartbeat says `mirrors` includes it.
    # Rows are inserted under the real server and original path — only the source differed — so a later
    # `tail` from the recovered server skips them as seen. Returns False if nobody holds copies; a peer that
    # fails is skipped. Sets `_last_mirror_added` for `tail` to sum. Never used while the resource answers.
    def _from_mirror(self, server: str, live: dict[str, dict]) -> bool:
        """A silent server's closed buckets, from whichever live peer holds
        copies (its heartbeat says: mirrors). Rows are inserted under the
        REAL server: only the source differed. Never used while the resource answers."""
        self._last_mirror_added = 0
        holders = [(s, hb) for s, hb in live.items() if server in hb.get("mirrors", {})]
        if not holders:
            return False
        for peer, hb in holders:
            try:
                for b in self.reader.mirrored(hb["url"], server):
                    if b.events == 0 or self.db.execute("SELECT 1 FROM seen WHERE server=? AND path=?", (server, b.path)).fetchone():
                        continue
                    rows = []
                    for e in self.reader.mirrored_events(hb["url"], server, b):
                        cam = e.get("cam", int(b.unit) if b.unit.isdigit() else None)
                        rows.append((b.subsystem, b.unit, cam, b.epoch, float(e["t"]), e["kind"], server, b.path,
                                     json.dumps({k: v for k, v in e.items() if k not in ("t", "kind", "cam")})))
                    with self.db:
                        self.db.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", rows)
                        self.db.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?)", (server, b.path, len(rows)))
                    self._last_mirror_added += len(rows); self.indexed_segments += 1
            except Exception:                        # noqa: BLE001 — that peer is not answering either
                continue
        return True

    def query(self, t0: float, t1: float, cam: int | None = None, kind: str | None = None,
              subsystem: str | None = None, unit: str | None = None,
              current_epochs: dict[tuple[str, str], int] | None = None, limit: int = 1000) -> dict:
        """`current_epochs` is {(subsystem, unit): epoch} — fencing is per unit, and only the
        unit's own subsystem knows its current epoch; the index just compares."""
        sql, args = "SELECT subsystem, unit, cam, epoch, t, kind, server, path, fields FROM events WHERE t >= ? AND t < ?", [t0, t1]
        if cam is not None: sql += " AND cam = ?"; args.append(cam)
        if kind is not None: sql += " AND kind = ?"; args.append(kind)
        if subsystem is not None: sql += " AND subsystem = ?"; args.append(subsystem)
        if unit is not None: sql += " AND unit = ?"; args.append(str(unit))
        sql += " ORDER BY t LIMIT ?"; args.append(limit)
        out = []
        for sub, u, c, ep, t, k, server, path, fields in self.db.execute(sql, args):
            cur = (current_epochs or {}).get((sub, u))
            out.append({"subsystem": sub, "unit": u, "cam": c, "epoch": ep, "t": t, "kind": k, "server": server, "bucket": path,
                        "fenced": cur is not None and ep < cur, **json.loads(fields)})
        return {"events": out, "state": self.state}

    # Retention on a resource removed a segment: delete its events and its `seen` row so a re-mirrored copy
    # would not be refused. Returns the count of paths.
    def forget(self, server: str, paths: list[str]) -> int:
        """Retention on a resource removed a segment: its events go with it."""
        with self.db:
            for p in paths:
                self.db.execute("DELETE FROM events WHERE server=? AND path=?", (server, p))
                self.db.execute("DELETE FROM seen WHERE server=? AND path=?", (server, p))
        return len(paths)


# The index a resource job runs, over its own root. `resources()` stands in for the heartbeats a cluster-wide
# index used to read: one live entry — this server, this tree — plus one SILENT entry per server whose copies
# lie under `.mirror/`, so `tail` indexes those copies through `_from_mirror` under their real owner's name.
# Rebuilt on start (a cache), tailed every `interval` seconds in a daemon thread so an open bucket's new
# lines are on the timeline within one tail. The resource calls `forget` when retention removes a bucket.
class ResourceIndex:
    """The eventindex of ONE resource: its own buckets and the mirror copies it holds.
    Not a subsystem — a cache with nothing to place — rebuilt on start, tailed
    every few seconds. М11 runs it inside the resource job and serves it as
    `GET /events`; the box's console runs it as `LocalIndex` over its archive."""

    def __init__(self, root: str, server: str | None = None, wall=time.time, path: str = ":memory:",
                 bucket_seconds: int = 600, interval: float = 3.0):
        self.root, self.wall, self.interval, self.bucket_seconds = root, wall, interval, bucket_seconds
        self.server = server or socket.gethostname()
        self.index = EventIndex(DirectoryReader(root, bucket_seconds), path, wall=wall)
        self._stop = threading.Event()

    @property
    def state(self) -> str:
        return self.index.state

    def resources(self) -> dict:
        mine = {"server": self.server, "ts": self.wall(), "url": f"file://{self.root}", "units": subsystems_under(self.root),
                "mirrors": {s: len(mirrored_buckets(self.root, s, self.bucket_seconds)) for s in mirrored_servers(self.root)}}
        held = {s: {"server": s, "ts": 0.0, "url": "", "units": {}} for s in mine["mirrors"]}     # silent by construction: read from my copies
        return {self.server: mine, **held}

    def tail(self) -> dict:
        return self.index.tail(self.resources())

    def rebuild(self) -> dict:
        return self.index.rebuild(self.resources())

    def query(self, *a, **kw) -> dict:
        return self.index.query(*a, **kw)

    def forget(self, server: str, paths: list[str]) -> int:
        return self.index.forget(server, paths)

    def start(self) -> "ResourceIndex":
        self.rebuild()
        def loop():
            while not self._stop.wait(self.interval):
                try:
                    self.tail()
                except Exception:                                         # noqa: BLE001
                    logging.getLogger("psimplatform.eventindex").exception("index tail failed")
        threading.Thread(target=loop, daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
