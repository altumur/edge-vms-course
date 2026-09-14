"""eventindex — the event "database", which is a cache. A platform job.

Events are observations: written by the worker that holds a unit's epoch,
into that unit's bucket on its server's resource (vmsplatform.events).
Cross-unit search needs an index over all of them, and this is it: one
per cluster (or per box), holding a SQLite table it can rebuild entirely
by re-reading every resource's buckets. It knows which subsystems exist by
what it finds in the resources' heartbeats; a new one is indexed the pass
after it starts writing, with no change here. It knows nothing about what
an event means: `cam` is a field an event may carry, indexed if present.

Its two properties are the controller's, in the form that matters here:
it holds nothing it cannot rebuild, and nothing running depends on it.
No controller writes events. A failed-over eventindex says *catching up*
until its rebuild is done rather than answering short.

    rebuild(resources)   read every subsystem's closed buckets on every resource and index them
    tail(resources)      the same, for buckets it has not seen (by (server, path))
    query(...)           subsystem, unit, cam (a field an event may carry), kind, time window; `unreachable` names silent resources
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # eventindex.py — the event "database", which is a cache: a SQLite index over every subsystem's buckets on
# every resource
#
# **Role in the module.** Lesson 3, the cluster-side reader of `events.py`. Events are written by workers
# into per-unit buckets on their server's resource; cross-unit search needs one index over all of them, and
# this is it: one per cluster (or per box), a SQLite table it can rebuild entirely by re-reading every
# resource's closed buckets over the resource's HTTP routes. It discovers subsystems and units from the
# resource heartbeats' `units` field, so a new subsystem is indexed the pass after it starts writing, with
# no change here. It knows nothing about what an event means: `cam` is a field an event may carry, indexed
# if present. Its two properties are the controller's in the form that matters here: it holds nothing it
# cannot rebuild, and nothing running depends on it. No controller writes events. A failed-over instance
# says *catching up* until its rebuild is done rather than answering short. `console.SpecConsole` serves
# `/events` from an instance when one is passed as `index`; none of the 47 tests construct one (it is
# exercised on the box / in М11).
#
# ## Module-level names
# None.
#
# ### `query(self, t0, t1, cam=None, kind=None, subsystem=None, unit=None, current_epochs=None, limit=1000)
# ->
# dict` Time window plus optional equality filters, ordered by `t`, limited. `current_epochs` is
# `{(subsystem, unit): epoch}` — fencing is per unit, and only the unit's own subsystem knows its current
# epoch; the index just compares and sets `fenced: epoch < current`. Each result row is `{subsystem, unit,
# cam, epoch, t, kind, server, bucket, fenced, **fields}`; the reply is `{events, state}` so a caller can
# tell a short answer during catch-up from a complete one. The console builds `current_epochs` from
# `<sub>/epoch/*`.
#
# ## Notes
# - The `cam` column exists so a VMS timeline can ask "everything about camera 7 from any subsystem" without
#   the index knowing what a camera is; the console's `/events?cam=` maps onto it.
# - Nothing is ever updated in place: rows are inserted per bucket and deleted per bucket, matching the
#   resource's file-level retention.
# ================================================================================================
from __future__ import annotations

import json
import sqlite3
import time
import urllib.request

from .events import Bucket
from .resource import bucket_from_line


# HTTP against the resource job (`resource.serve`); tests would substitute a directory reader with the same
# four methods.
class ResourceReader:
    """HTTP against the resource job; tests substitute a directory reader."""
    def __init__(self, timeout: float = 3.0): self.timeout = timeout

    # `GET <url>/buckets/<sub>/<unit>`, parsed with `bucket_from_line`.
    def buckets(self, url: str, sub: str, unit: str) -> list[Bucket]:
        with urllib.request.urlopen(f"{url}/buckets/{sub}/{unit}", timeout=self.timeout) as r:
            return [bucket_from_line(l) for l in r.read().decode().splitlines() if l.strip()]

    # `GET <url>/events/<b.path>`, one dict per line.
    def events(self, url: str, b: Bucket) -> list[dict]:
        with urllib.request.urlopen(f"{url}/events/{b.path}", timeout=self.timeout) as r:
            return [json.loads(l) for l in r.read().decode().splitlines() if l.strip()]

    # `GET <url>/mirrored/<server>` — a peer's copies of `server`'s buckets.
    def mirrored(self, url: str, server: str) -> list[Bucket]:
        with urllib.request.urlopen(f"{url}/mirrored/{server}", timeout=self.timeout) as r:
            return [bucket_from_line(l) for l in r.read().decode().splitlines() if l.strip()]

    # `GET <url>/events/.mirror/<server>/<b.path>`.
    def mirrored_events(self, url: str, server: str, b: Bucket) -> list[dict]:
        with urllib.request.urlopen(f"{url}/events/.mirror/{server}/{b.path}", timeout=self.timeout) as r:
            return [json.loads(l) for l in r.read().decode().splitlines() if l.strip()]


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
            CREATE TABLE IF NOT EXISTS seen   (server TEXT, path TEXT, PRIMARY KEY (server, path));
            CREATE TABLE IF NOT EXISTS events (subsystem TEXT, unit TEXT, cam INTEGER, epoch INTEGER, t REAL, kind TEXT,
                                               server TEXT, path TEXT, fields TEXT);
            CREATE INDEX IF NOT EXISTS events_cam_t ON events (cam, t);
            CREATE INDEX IF NOT EXISTS events_sub_unit_t ON events (subsystem, unit, t);
            CREATE INDEX IF NOT EXISTS events_kind_t ON events (kind, t);""")
        self.state = "empty"
        self.indexed_segments = 0

    # From nothing — what a failed-over instance does first: truncate both tables, zero the counter, then
    # `tail(resources, rebuild=True)`.
    def rebuild(self, resources: dict[str, dict]) -> dict:
        """From nothing: what a failed-over instance does first."""
        self.db.executescript("DELETE FROM seen; DELETE FROM events;")
        self.indexed_segments = 0
        return self.tail(resources, rebuild=True)

    # `resources` is `resources_seen()` (`{server: heartbeat}`). Sets `state` to "catching up" on a rebuild.
    # Splits servers into live and silent by `ts`. For a silent server, try `_from_mirror`; if no live peer
    # holds copies, list it as unreachable. For a live server, walk `heartbeat["units"]` and, for each
    # bucket the resource reports, skip it if it has no events or is already in `seen`; otherwise fetch its
    # lines and insert one row each — `cam` is the event's `cam` field, or the unit id itself when the unit
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
                            if b.events == 0 or self.db.execute("SELECT 1 FROM seen WHERE server=? AND path=?", (server, b.path)).fetchone():
                                continue
                            rows = []
                            for e in self.reader.events(hb["url"], b):
                                cam = e.get("cam", int(unit) if unit.isdigit() else None)     # a numeric unit is its own `cam`; others may name one
                                rows.append((sub, unit, cam, b.epoch, float(e["t"]), e["kind"], server, b.path,
                                             json.dumps({k: v for k, v in e.items() if k not in ("t", "kind", "cam")})))
                            with self.db:
                                self.db.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", rows)
                                self.db.execute("INSERT INTO seen VALUES (?,?)", (server, b.path))
                            added += len(rows); self.indexed_segments += 1
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
                        self.db.execute("INSERT INTO seen VALUES (?,?)", (server, b.path))
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
