"""eventdatabase — the event "database", which is a cache. Part of the resource job:
one per RESOURCE, over that resource's own tree — its buckets, and the copies
it holds of its peers' closed buckets (`.mirror/<server>/`). Nothing per
console, nothing cluster-wide: a console with a question asks every live
resource's `GET /events` and merges the answers by time (`MergedIndex`), on
one box and on a cluster alike.

Events are observations: written by the worker that holds a unit's epoch,
into that unit's bucket on its server's resource (w2cplatform.events). The
database is a SQLite table the resource job can rebuild entirely by
re-reading its tree. It knows which subsystems exist by the directories it
finds; a new one is indexed the pass after it starts writing, with no change
here. It knows nothing about what an event means: `cam` is a field an event
may carry, indexed if present.

Its two properties are the controller's, in the form that matters here:
it holds nothing it cannot rebuild, and nothing running depends on it.
No controller writes events. A restarted resource says *catching up*
until its rebuild is done rather than answering short.

    EventDatabase(root, server)   the resource job's: rebuilt on start, tailed every few seconds
    rebuild() / tail()            read the tree: closed buckets once, open ones by the lines past what is held
    query(...)                    subsystem, unit, cam (a field an event may carry), kind, time window
    forget(server, paths)         retention removed a bucket: its rows go with it (the resource calls this)
    MergedIndex(objects)          what a console has instead: every live resource's /events, merged
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # eventdatabase.py — the event "database", which is a cache: one SQLite table per resource, over its own tree
#
# **Role in the module.** Lesson 10 on the box, Lesson 3 on the cluster: the reader of `events.py`. Events are
# written by workers into per-unit buckets on their server's resource; search needs a database over them,
# and this is it — but one PER RESOURCE, not one per cluster: the resource job runs an `EventDatabase` over
# its own root (own buckets and the `.mirror/<server>/` copies it holds) and answers `GET /events` from it.
# A console asks every live resource and merges (`MergedIndex`) — the box's console asks its one resource,
# М11's asks them all. Nothing crosses the network to build a database, and no job holds every server's
# rows. It discovers subsystems and units from the directories, so a new subsystem is indexed the pass
# after it starts writing, with no change here. It knows nothing about what an event means: `cam` is a
# field an event may carry, indexed if present. Its two properties are the controller's in the form that
# matters here: it holds nothing it cannot rebuild, and nothing running depends on it. No controller writes
# events.
#
# ## Module-level names
# None.
#
# ## Notes
# - The `cam` column exists so a VMS timeline can ask "everything about camera 7 from any subsystem" without
#   the database knowing what a camera is; the console's `/events?cam=` maps onto it.
# - Nothing is ever updated in place: rows are inserted per bucket (an open bucket by the lines past what is
#   held) and deleted per bucket, matching the resource's file-level retention.
# - Rows from a mirror copy are inserted under the REAL owning server and the original path: a query answers
#   "srv-a's events" whichever resource holds them, and the console drops the copy when the owner itself
#   answers.
# ================================================================================================
from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

from .events import ALARM, CLASSES, OBSERVATION, Bucket, buckets_under, read_bucket, subsystems_under
from .resource import MIRROR_DIR, mirrored_buckets, mirrored_servers, resources_seen


# The database. State: a SQLite connection (`check_same_thread=False`, so the resource's threaded server may
# query it while the tail thread writes), `state` (`"empty"`, `"catching up"`, `"live"`) and
# `indexed_segments`. Two tables: `seen (server, path, n)` — which buckets have been ingested and how many
# lines of each, keyed by the real owning server and the original path; and `events (subsystem, unit, cam,
# epoch, t, kind, server, path, fields)` with indexes on `(cam, t)`, `(subsystem, unit, t)`, `(kind, t)`.
# `fields` holds the remaining event keys as JSON.
class EventDatabase:
    """The event database of ONE resource: its own buckets and the mirror copies it holds.
    Not a subsystem — a cache with nothing to place — rebuilt on start, tailed
    every few seconds. The resource job serves it as `GET /events`."""

    def __init__(self, root: str, server: str | None = None, path: str = ":memory:", wall=time.time,
                 bucket_seconds: int = 600, interval: float = 3.0):
        self.root, self.wall, self.bucket_seconds, self.interval = root, wall, bucket_seconds, interval
        self.server = server or socket.gethostname()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS seen   (server TEXT, path TEXT, n INTEGER DEFAULT 0, PRIMARY KEY (server, path));
            CREATE TABLE IF NOT EXISTS events (subsystem TEXT, unit TEXT, cam INTEGER, epoch INTEGER, t REAL, kind TEXT,
                                               server TEXT, path TEXT, cls TEXT, fields TEXT);
            CREATE INDEX IF NOT EXISTS events_cam_t ON events (cam, t);
            CREATE INDEX IF NOT EXISTS events_sub_unit_t ON events (subsystem, unit, t);
            CREATE INDEX IF NOT EXISTS events_kind_t ON events (kind, t);
            CREATE INDEX IF NOT EXISTS events_cls_t ON events (cls, t);""")
        self.state = "empty"
        self.indexed_segments = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # From nothing — what a restarted resource job does first: truncate both tables, zero the counter, then
    # `tail(rebuild=True)`.
    def rebuild(self) -> dict:
        """From nothing: what a restarted resource job does first."""
        with self._lock:
            self.db.executescript("DELETE FROM seen; DELETE FROM events;")
            self.indexed_segments = 0
        return self.tail(rebuild=True)

    # Walks the tree: every unit of every subsystem under `root`, then every server under `.mirror/`. For
    # each bucket, skip it if the database already holds as many lines as the file has (a closed bucket
    # never grows; an open one does); otherwise insert the lines past what is held — `cam` is the event's
    # `cam` field, or the unit id itself when the unit is numeric — and mark the count, in one transaction
    # per bucket. Copies are inserted under the REAL owning server and the original path. Returns `{added,
    # segments, mirrored}`; sets `state` to "live".
    def tail(self, rebuild: bool = False) -> dict:
        self.state = "catching up" if rebuild else self.state
        added = 0
        for sub, units in subsystems_under(self.root).items():
            for unit in units:
                for b in buckets_under(self.root, sub, unit, self.bucket_seconds):
                    added += self._ingest(self.server, b, os.path.join(self.root, b.path))
        mirrored = mirrored_servers(self.root)
        for server in mirrored:
            for b in mirrored_buckets(self.root, server, self.bucket_seconds):
                added += self._ingest(server, b, os.path.join(self.root, MIRROR_DIR, server, b.path))
        self.state = "live"
        return {"added": added, "segments": self.indexed_segments, "mirrored": mirrored}

    def _ingest(self, server: str, b: Bucket, file: str) -> int:
        with self._lock:
            row = self.db.execute("SELECT n FROM seen WHERE server=? AND path=?", (server, b.path)).fetchone()
            have = row[0] if row else 0
            if b.events <= have:                                              # nothing new: a closed bucket never grows, an open one may
                return 0
            rows = []
            for e in read_bucket(file)[have:]:                                # buckets are append-only: the lines past what we hold
                cam = e.get("cam", int(b.unit) if b.unit.isdigit() else None)   # a numeric unit is its own `cam`; others may name one
                # `class` becomes a COLUMN and leaves `fields`: it is the one field the database itself
                # acts on (the overflow policy in `query`), and a JSON blob cannot be ordered by. Absent
                # in the line means `observation`, so the column is never null and the SQL never has to
                # say `IS NULL OR = ?`.
                rows.append((b.subsystem, b.unit, cam, b.epoch, float(e["t"]), e["kind"], server, b.path,
                             str(e.get("class", OBSERVATION)),
                             json.dumps({k: v for k, v in e.items() if k not in ("t", "kind", "cam", "class")})))
            with self.db:
                self.db.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
                self.db.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?)", (server, b.path, have + len(rows)))
            self.indexed_segments += 0 if row else 1
            return len(rows)

    def query(self, t0: float, t1: float, cam: int | None = None, kind: str | None = None,
              subsystem: str | None = None, unit: str | None = None,
              current_epochs: dict[tuple[str, str], int] | None = None, limit: int = 1000,
              epoch_policy: dict[str, str] | None = None, keep: str = "newest", cls: str | None = None) -> dict:
        """`current_epochs` is {(subsystem, unit): epoch} — fencing is per unit, and only the
        unit's own subsystem knows its current epoch; the database just compares.

        `epoch_policy` is {subsystem: "fenced" | "earlier-run"} — what an older epoch MEANS
        there. Same comparison, two meanings: a writer that lost the race, or a finished
        earlier run of work that ends. The database is told; it does not decide.

        `keep` is which END of an overflowing window survives `limit` — and it is the CALLER's
        to choose, because "a thousand of the five thousand" means nothing without it. The
        default is "newest": nearly everything asked of an event log is a form of "what just
        happened", and the reader that wants the other end — paging forward through an archive
        from a cursor — knows that about itself and says so. `LIMIT` with no direction was this
        method dropping the newest end silently, which is the end a timeline and an automation
        scenario are both looking at.

        The answer is ascending by time whichever end was kept, and it carries `truncated`, so a
        decision taken on part of a window cannot look like one taken on all of it.

        `cls` narrows to one traffic class — "show me the alarms of this hour" — and an unknown one
        is refused rather than answered with nothing, because an empty list is what "no alarms
        happened" looks like too.

        And the class earns its existence HERE: when the window overflows, OBSERVATIONS ARE DROPPED
        BEFORE ALARMS. A limit is a budget for a screenful, and spending it on the thousand statistics
        lines that crowded out the one contact that opened is the failure the class was introduced to
        prevent. The ordering costs one clause; leaving it out would make the class a word in a file."""
        if keep not in ("newest", "oldest"):
            raise ValueError(f"keep is 'newest' or 'oldest', not {keep!r}")
        if cls is not None and cls not in CLASSES:
            raise ValueError(f"event class is one of {', '.join(CLASSES)}, not {cls!r}")
        sql, args = ("SELECT subsystem, unit, cam, epoch, t, kind, server, path, cls, fields "
                     "FROM events WHERE t >= ? AND t < ?"), [t0, t1]
        if cam is not None: sql += " AND cam = ?"; args.append(cam)
        if kind is not None: sql += " AND kind = ?"; args.append(kind)
        if subsystem is not None: sql += " AND subsystem = ?"; args.append(subsystem)
        if unit is not None: sql += " AND unit = ?"; args.append(str(unit))
        if cls is not None: sql += " AND cls = ?"; args.append(cls)
        # One row PAST the limit, so `truncated` is a fact and not a guess: a window holding exactly
        # `limit` rows is whole, and calling it cut would be the same lie pointing the other way.
        #
        # Alarms first, then time. `keep` still decides which end of the OBSERVATIONS survives; what it
        # no longer decides is whether an alarm survives at all.
        args.append(ALARM)                                        # binds the ORDER BY below, after every filter above
        sql += " ORDER BY cls = ? DESC, t DESC LIMIT ?" if keep == "newest" else " ORDER BY cls = ? DESC, t LIMIT ?"
        args.append(limit + 1)
        out = []
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        truncated = len(rows) > limit
        rows = rows[:limit]
        rows.sort(key=lambda r: r[4])                             # alarms came first for the CUT; the answer is by time
        for sub, u, c, ep, t, k, server, path, rcls, fields in rows:
            cur = (current_epochs or {}).get((sub, u))
            older = cur is not None and ep < cur
            was = (epoch_policy or {}).get(sub, "fenced") if older else "current"
            out.append({"subsystem": sub, "unit": u, "cam": c, "epoch": ep, "t": t, "kind": k, "server": server, "bucket": path,
                        "class": rcls or OBSERVATION,
                        "epoch_is": was, "fenced": was == "fenced", **json.loads(fields)})
        return {"events": out, "state": self.state, "truncated": truncated}

    # Retention removed a bucket: delete its rows and its `seen` row, so a re-mirrored copy is not refused.
    def forget(self, server: str, paths: list[str]) -> int:
        """Retention on the resource removed a bucket: its events go with it."""
        with self._lock, self.db:
            for p in paths:
                self.db.execute("DELETE FROM events WHERE server=? AND path=?", (server, p))
                self.db.execute("DELETE FROM seen WHERE server=? AND path=?", (server, p))
        return len(paths)

    # Rebuild now, then tail every `interval` seconds in a daemon thread, so an open bucket's new lines are
    # on the timeline within one tail.
    def start(self) -> "EventDatabase":
        self.rebuild()

        def loop():
            while not self._stop.wait(self.interval):
                try:
                    self.tail()
                except Exception:                                             # noqa: BLE001
                    logging.getLogger("w2cplatform.eventdatabase").exception("event database tail failed")
        threading.Thread(target=loop, daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()


# What stands behind a console's `/events`: nothing of its own. `query` asks every LIVE resource's
# `GET /events` (each answers from the database over its own tree — own buckets and mirror copies), merges by
# time, dedupes a dead server's copies when two peers hold them, drops a copy when the owner is live (it
# answered for itself), fences by `current_epochs`, and names in `state` the servers nobody answered for:
# `live; srv-a unreachable` for a resource that is silent and unmirrored (or live but not answering),
# `live; srv-a from mirror` when a peer's copy stood in. The same shape `EventDatabase.query` returns, so
# `SpecConsole` cannot tell the difference. `fetch(url, params) -> dict` is HTTP by default; tests pass a
# call into the resource's database.
class MergedIndex:
    """A console's view of the event databases: every live resource's `/events`, merged by time."""

    def __init__(self, objects, fetch=None, wall=time.time, lost_after: float = 45.0, timeout: float = 3.0):
        self.objects, self.wall, self.lost_after, self.timeout = objects, wall, lost_after, timeout
        self.fetch = fetch or self._http
        self.state = "live"

    def _http(self, url: str, params: dict) -> dict:
        with urllib.request.urlopen(f"{url}/events?{urllib.parse.urlencode(params)}", timeout=self.timeout) as r:
            return json.loads(r.read())

    def query(self, t0: float, t1: float, cam=None, kind=None, subsystem=None, unit=None, current_epochs=None,
              limit: int = 1000, epoch_policy: dict[str, str] | None = None, keep: str = "newest",
              cls: str | None = None) -> dict:
        """`keep` as in `EventDatabase.query`, and it has to be carried BOTH ways: the merge asks
        each resource for a window and then cuts the union to `limit` again, so a limit with no
        direction dropped the newest end twice — once per resource, once more over the merge.

        The overflow policy is carried both ways for the same reason: each resource kept its alarms
        ahead of its observations, and the merge must do it again over the union, or an alarm that
        survived on its own server is dropped when it meets a busier neighbour's window.

        `truncated` is true if ANY resource cut its answer or the merge cut theirs: the reader is
        told the window is partial, not which server made it partial.

        The check below is the SAME refusal the database makes, and it has to be here too rather
        than left to the resources: this method reads any failure from a resource as "unreachable"
        (live by heartbeat, not answering), so a caller's bad `keep` would come back as an empty
        window and an infrastructure fault that never happened."""
        if keep not in ("newest", "oldest"):
            raise ValueError(f"keep is 'newest' or 'oldest', not {keep!r}")
        if cls is not None and cls not in CLASSES:
            raise ValueError(f"event class is one of {', '.join(CLASSES)}, not {cls!r}")
        now = self.wall(); seen = resources_seen(self.objects)
        live = {s for s, hb in seen.items() if now - float(hb["ts"]) <= self.lost_after}
        params = {k: v for k, v in (("from", t0), ("to", t1), ("cam", cam), ("kind", kind), ("subsystem", subsystem),
                                    ("unit", unit), ("limit", limit), ("keep", keep), ("class", cls)) if v is not None}
        events, unreachable, from_mirror, have = [], [], set(), set()
        truncated = False
        for server in sorted(live):
            try:
                rep = self.fetch(seen[server]["url"], params)
            except Exception:                                   # noqa: BLE001 — live by heartbeat, not answering
                unreachable.append(server); continue
            truncated = truncated or bool(rep.get("truncated"))
            for e in rep["events"]:
                if e["server"] != server:                         # a copy this resource holds for a peer
                    if e["server"] in live:
                        continue                                  # the owner answers for itself
                    key = (e["server"], e["bucket"], e["t"], e["kind"], e["unit"])
                    if key in have:
                        continue                                  # two peers hold the same copy
                    have.add(key); from_mirror.add(e["server"])
                events.append(e)
        for server in sorted(seen):
            if server not in live and server not in from_mirror:
                unreachable.append(server)                        # silent, and nobody holds its copies
        events.sort(key=lambda e: e["t"])
        if len(events) > limit:
            truncated = True
            alarms = [e for e in events if e.get("class") == ALARM]
            rest = [e for e in events if e.get("class") != ALARM]
            # Alarms first; what is left over is the observations' budget. A window holding more alarms
            # than the whole limit is cut like anything else — an unbounded answer is not a kindness to
            # anybody — but it is cut with the observations already gone, and `truncated` says so.
            alarms = alarms if len(alarms) <= limit else (alarms[-limit:] if keep == "newest" else alarms[:limit])
            room = max(0, limit - len(alarms))
            events = sorted(alarms + (rest[-room:] if keep == "newest" else rest[:room]), key=lambda e: e["t"])
        cur = current_epochs or {}
        for e in events:                                          # each resource fenced its own; re-decide over the merge
            c = cur.get((e["subsystem"], e["unit"]))
            older = c is not None and e["epoch"] < c
            e["epoch_is"] = (epoch_policy or {}).get(e["subsystem"], "fenced") if older else "current"
            e["fenced"] = e["epoch_is"] == "fenced"
        unreachable = sorted(set(unreachable))
        self.state = "live" + (f"; {', '.join(unreachable)} unreachable" if unreachable else "") \
                            + (f"; {', '.join(sorted(from_mirror))} from mirror" if from_mirror else "")
        return {"events": events, "state": self.state, "truncated": truncated}
