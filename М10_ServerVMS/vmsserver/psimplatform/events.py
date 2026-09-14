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


# All lines of one bucket parsed; a missing file is an empty list.
def read_bucket(path: str) -> list[dict]:
    try:
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return []


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
