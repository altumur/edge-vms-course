"""The event log — a platform piece. What the platform knows about events,
and it is all of this:

    a bucket    <resource>/<subsystem>/<unit>/e<epoch>/<start>Z.events.jsonl
                JSON lines {t, kind, ...}, for a span of `bucket_seconds` starting at <start>
    its writer  the worker that holds that unit's epoch — one writer per file, by construction
    its fence   the epoch in the path: a stale instance writes into its own bucket, marked afterwards
    its index   the manifest beside the unit's buckets, and (М11) a cluster-wide cache over every resource

Nothing here knows what a unit is. The VMS's unit has footage and its
bucket sits beside it; a detector's unit is a model; a gateway's
unit is a fan-out. The word "event" means only: something a worker
observed at a time, about a unit it holds.
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # events.py — the event log: JSON-lines buckets per unit per epoch on the resource, for any subsystem
#
# **Role in the module.** Lesson 3. An event here means only "something a worker observed at a time, about a
# unit it holds the epoch for". The platform fixes the shape and nothing else: a bucket is `<resource
# root>/<subsystem>/<unit>/e<epoch>/<start>Z.events.jsonl`, holding JSON lines `{t, kind, ...}` for a span
# of `bucket_seconds` starting at `<start>`; its writer is the worker holding that unit's epoch (one writer
# per file by construction); its fence is the epoch in the path (a stale instance writes into its own
# bucket, which is marked afterwards); its index is the manifest beside the unit's buckets (the VMS's, in
# `vms/archive.py`) and `eventdatabase.py` on each resource. `resource.py` walks these paths for retention
# and mirroring; `console.py` uses `EventLog` for operator marks under `console/<instance>/`; the VMS worker
# uses it per camera. Nothing here knows what a unit is.
#
# ## Module-level names
# - `EVENTS` — regex for a bucket filename: `YYYYMMDDTHHMMSSZ.events.jsonl`.
# - `EPOCH_DIR` — regex for the epoch directory: `e<digits>`.
#
# ## Notes
# - `test_lesson3_archive.py::test_events_are_buckets_on_the_resource_recording_or_not` and
#   `test_lesson4_worker.py::test_the_worker_observes_what_it_holds_recording_or_not` exercise the VMS's use
#   of this file: buckets are written whether or not media is recorded, closed buckets are counted onto the
#   timeline, and a fenced epoch's bucket is marked, not deleted.
# - A bucket is "closed" when `end <= now`; only closed buckets are mirrored and indexed
#   (`resource.Resource.closed_buckets`). The open one is the accepted loss on a disk failure.
# ================================================================================================
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

EVENTS = re.compile(r"^(\d{8}T\d{6}Z)\.events\.jsonl$")
EPOCH_DIR = re.compile(r"^e(\d+)$")


# Floors `t` to the start of its bucket span. Buckets roll by the clock, not by anything the subsystem does.
def bucket_start(t: float, bucket_seconds: int) -> float:
    return float(int(t // bucket_seconds) * bucket_seconds)


# UTC `%Y%m%dT%H%M%SZ` for a timestamp — the filename stem.
def _stamp(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# `<root>/<subsystem>/<unit>`.
def unit_dir(root: str, subsystem: str, unit: str) -> str:
    return os.path.join(root, subsystem, str(unit))


# `<root>/<subsystem>/<unit>/e<epoch>/<stamp>.events.jsonl`.
def bucket_path(root: str, subsystem: str, unit: str, epoch: int, start: float) -> str:
    return os.path.join(unit_dir(root, subsystem, unit), f"e{epoch}", _stamp(start) + ".events.jsonl")


# The inverse of `bucket_path`: relative to `root`, exactly four components, third matching `EPOCH_DIR`,
# fourth matching `EVENTS`; the start is parsed back to a UTC timestamp. Anything else (a media file, a
# manifest, a tmp file) is `None`, which is how the walkers below ignore whatever a subsystem keeps beside
# its buckets.
def parse_bucket(path: str, root: str) -> tuple[str, str, int, float] | None:
    """-> (subsystem, unit, epoch, start) for a bucket path under root, else None."""
    rel = os.path.relpath(path, root).split(os.sep)
    if len(rel) != 4 or not EPOCH_DIR.match(rel[2]):
        return None
    m = EVENTS.match(rel[3])
    if not m:
        return None
    start = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
    return rel[0], rel[1], int(rel[2][1:]), start


# One bucket as the resource describes it over HTTP and the index stores it.
# - `subsystem`, `unit`, `epoch`, `start`, `end` (= start + bucket_seconds), `path` (relative to the
#   resource root), `events` (line count).
@dataclass(frozen=True)
class Bucket:
    subsystem: str
    unit: str
    epoch: int
    start: float
    end: float
    path: str            # relative to the resource root
    events: int

    # The bucket as one JSON line with `kind: "events"` — the wire form of `GET /buckets/<sub>/<unit>` and
    # `GET /mirrored/<server>`; `resource.bucket_from_line` parses it back.
    def line(self) -> str:
        return json.dumps({"kind": "events", "subsystem": self.subsystem, "unit": self.unit, "epoch": self.epoch,
                           "start": self.start, "end": self.end, "path": self.path, "events": self.events})


# What a worker holds per unit it has an epoch for: the writer side.
class EventLog:
    """What a worker holds per unit it has an epoch for. `append` writes one
    line, flushed, into the bucket for `t`; buckets roll by the clock, not by
    anything the subsystem does."""

    # Fixes the resource root, the subsystem prefix, the unit (stringified), the epoch this writer holds and
    # the bucket span (10 minutes by default).
    def __init__(self, root: str, subsystem: str, unit: str, epoch: int, bucket_seconds: int = 600):
        self.root, self.subsystem, self.unit, self.epoch, self.bucket_seconds = root, subsystem, str(unit), epoch, bucket_seconds

    # The bucket file that time `t` falls in, for this epoch.
    def path_for(self, t: float) -> str:
        return bucket_path(self.root, self.subsystem, self.unit, self.epoch, bucket_start(t, self.bucket_seconds))

    # Writes one JSON line `{t, kind, **fields}` to the bucket for `t`, creating directories, flushing after
    # the write; returns the path. Append-only, one process per file: the epoch in the path guarantees no
    # two live writers share a file.
    def append(self, t: float, kind: str, **fields) -> str:
        p = self.path_for(t)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps({"t": t, "kind": kind, **fields}) + "\n"); f.flush()
        return p


# All lines of one bucket parsed; a missing file is an empty list. A line that does not parse is SKIPPED,
# not fatal: `append` writes and flushes without `fsync`, so a crash or a power loss can leave the last line
# half-written, and losing the whole bucket for one torn line would lose ten minutes of observations where
# one record was actually damaged. The accepted loss is then the same shape as it is for footage — the open
# thing, not the day (М10B Lesson 6). `torn` counts them, so a resource whose buckets keep tearing says so
# instead of quietly returning less.
torn = 0


def read_bucket(path: str) -> list[dict]:
    global torn
    out = []
    try:
        with open(path) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:                    # a half-written last line: the writer died mid-append
                    torn += 1
    except FileNotFoundError:
        return []
    return out


# Every bucket file for a unit, from the files alone — what repair and the resource's `/buckets` route read.
# Walks the unit directory, keeps what `parse_bucket` accepts, counts lines, sorts by `(start, epoch)`.
def buckets_under(root: str, subsystem: str, unit: str, bucket_seconds: int) -> list[Bucket]:
    """Every bucket file for a unit, from the files alone — what repair reads."""
    out = []
    base = unit_dir(root, subsystem, unit)
    for d, _, files in os.walk(base):
        for f in files:
            p = os.path.join(d, f)
            parsed = parse_bucket(p, root)
            if parsed:
                sub, u, epoch, start = parsed
                out.append(Bucket(sub, u, epoch, start, start + bucket_seconds, os.path.relpath(p, root), len(read_bucket(p))))
    return sorted(out, key=lambda b: (b.start, b.epoch))


# `{subsystem: [unit, ...]}` present on a resource, from the directory tree — the index's and the resource
# heartbeat's discovery, with no registry. Hidden directories (`.mirror`) are skipped; a missing root is
# `{}`. The console test asserts that after one mark the archive root shows `{"console": [<instance>]}` and
# nothing under `vms/1/`, proving a mark is the console's bucket, not a worker's.
def subsystems_under(root: str) -> dict[str, list[str]]:
    """{subsystem: [unit, ...]} present on a resource — the index's discovery, no registry."""
    out: dict[str, list[str]] = {}
    try:
        subs = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)) and not d.startswith("."))
    except FileNotFoundError:
        return out
    for sub in subs:
        units = sorted(u for u in os.listdir(os.path.join(root, sub)) if os.path.isdir(os.path.join(root, sub, u)))
        out[sub] = units
    return out


# ==================================================================================================
# Suppression — the writer's work, because the writer is the only one who sees the stream before it
# becomes a file.
# ==================================================================================================
# A storm of events is NORMAL, not a fault: a contact bouncing, a link flapping, a sensor re-reporting
# for as long as the thing it watches keeps happening. Nothing downstream can undo one — an index reads
# what is written, a timeline draws what it reads, and every reader that tried to collapse repeats would
# do it its own way and disagree with the others. The one place a repeat can be recognised BEFORE it costs anything is the
# process holding the unit's epoch, one line before `append`.
#
# Two things make suppression honest rather than a quiet loss of data:
#
#   1. The FIRST line of a window is written immediately and unchanged. Whatever the repeats are
#      worth, the first occurrence is the observation, and delaying it to batch it would trade the
#      one property an alarm has for a smaller file.
#   2. What was dropped is SAID. When the window closes, a summary line carries `repeats`, `since`
#      and `until`, so a quiet log and a suppressed storm are different things on the timeline.
#      Without it suppression is indistinguishable from nothing having happened, which is the failure
#      this whole module is built to avoid (`state`, `truncated`, `unreachable` — Lesson 13).
@dataclass(frozen=True)
class Suppress:
    """One kind's rule. `window` in seconds; `by` are the fields that, with the kind, say two lines
    are THE SAME THING — `None` means every field the line carries.

    Every-field is the default because it cannot be wrong: two lines collapse only when they are
    identical, so a contact that opens and then closes (`value` differs) is never one event, and the
    same reading reported twice is never two. A subsystem narrows it when a field drifts for reasons
    that are not a new observation — a score, a temperature, a counter."""
    kind: str
    window: float
    by: tuple[str, ...] | None = None


class Suppressor:
    """Per (unit, kind, identity) counters, held by the writer for as long as it holds the unit.

    Not a platform decision about what deserves suppressing: it applies the rules its subsystem
    declared and nothing else. A kind with no rule passes through untouched, which is what every
    subsystem gets until it says otherwise."""

    def __init__(self, rules: dict[str, Suppress] | None = None):
        self.rules = dict(rules or {})
        self.open: dict[tuple, list] = {}          # key -> [since, until, repeats, kind, first fields]

    # The identity of one line: the unit, the kind, and the values of the fields that decide sameness.
    # `None` when this kind has no rule — the caller writes it and asks nothing further.
    def key(self, unit: str, kind: str, fields: dict):
        rule = self.rules.get(kind)
        if rule is None:
            return None
        names = sorted(fields) if rule.by is None else sorted(rule.by)
        return (str(unit), kind, tuple((n, fields.get(n)) for n in names))

    # What to write for one observation, in order: `(t, kind, fields)` triples.
    #
    # Either one line (the observation, when nothing is being suppressed), or none (a repeat inside
    # an open window), or two (the window closed: the summary of what it swallowed, then this line as
    # the first of the new window). The summary comes FIRST because it happened first — a log read by
    # time must not put the report of a storm after the line that ended it.
    def lines(self, t: float, unit: str, kind: str, fields: dict) -> list[tuple[float, str, dict]]:
        k = self.key(unit, kind, fields)
        if k is None:
            return [(t, kind, dict(fields))]
        rule, state = self.rules[kind], self.open.get(k)
        if state is not None and t - state[0] < rule.window:
            state[1], state[2] = t, state[2] + 1                  # inside the window: counted, not written
            return []
        out = []
        if state is not None and state[2]:
            out.append(self._summary(k))
        self.open[k] = [t, t, 0, kind, dict(fields)]
        out.append((t, kind, dict(fields)))
        return out

    # Windows that have closed since the last call, as summaries — the lines nobody would otherwise
    # write, because the storm stopped and no observation came to close the window. Called once a
    # pass: without it a burst that ends is a burst nobody ever counted.
    def flush(self, t: float) -> list[tuple[str, float, str, dict]]:
        out = []
        for k, state in sorted(self.open.items(), key=lambda kv: kv[1][0]):
            if t - state[0] >= self.rules[state[3]].window:
                if state[2]:
                    out.append((k[0], *self._summary(k)))         # the unit too: the caller writes per unit
                del self.open[k]
        return out

    # `repeats` is how many were swallowed, `since`/`until` the bounds they fell between — the three
    # numbers an incident is reconstructed from. The fields are the FIRST line's of that window: with
    # every-field identity they are all of them, and with a narrowed `by` they are one concrete
    # example of what repeated.
    def _summary(self, k) -> tuple[float, str, dict]:
        since, until, repeats, kind, fields = self.open[k]
        return (until, kind, {**fields, "repeats": repeats, "since": since, "until": until})
