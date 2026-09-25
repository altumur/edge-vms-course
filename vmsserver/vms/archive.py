"""The archive as a resource — server-bound, no controller, a policy.

    <spool>/rec/<unit>/e<epoch>/<start>Z.mp4       the open segment, and closed ones not yet promoted
    <archive>/rec/<unit>/e<epoch>/<start>Z.mp4     promoted: the resource's media — the RECORDER's tree
    <archive>/rec/<unit>/manifest.jsonl            one line per media segment: the index beside the footage
    <archive>/vms/<cam>/e<epoch>/<start>Z.events.jsonl
                                                   the camera's EVENT BUCKETS — the platform's event log
                                                   (w2cplatform.events), written by the WORKER holding the
                                                   camera, whether or not anything records it; on the
                                                   worker's server, which need not be the recorder's

Two subsystems, two trees, one camera. The worker (`vms`) holds the camera —
one connection, one epoch, the fan-out — and writes what it observes into
`vms/<cam>/`. The recorder (`rec`) is placed on a server with an archive,
subscribes to the worker's fan-out, and writes footage under its own epoch
into `rec/<unit>/` — the recording's own directory, named by the operator
(`7`, `7-cloud`) and not by the camera. A camera that is watched and never recorded has buckets
and no `rec/` tree; a camera whose recorder moved has footage under two
servers' `rec/` trees, merged by the console. The manifest indexes media
only; events are indexed by the resource's event database.

The acknowledgement order is М9 Lesson 4's: a closed segment is PROMOTED
(renamed into the archive, then a manifest line appended), and the spool
copy is gone only after that. The manifest is append-only and rebuildable
from the files.

Retention is a policy, per kind. Media is the recorder's: `retain()` after
the recording row's `retention_days`, files first then lines. Buckets are
the PLATFORM's (w2cplatform.resource): the VMS controller writes
`vms/retention/<cam> {days: events_retention_days}` and the resource job
deletes the files — events are small and often kept a year where footage
is kept a month.

The layout is `<subsystem>/<unit>/...` — the platform resource's tree — so
that every subsystem's data sits on the same server under its own prefix.
`ArchivePolicy` is what the recorder registers with the platform's resource
job: its own pass over its own part of the tree.
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # archive.py — the archive resource: promotion, the manifest, repair, media retention; the recorder's tree
#
# **Role in the module.** Lesson 3 (the archive as a resource) and Lesson 6 (the recorder). Media lives under
# `rec/<unit>/e<epoch>/` on the recorder's server; the camera's event buckets live under `vms/<cam>/e<epoch>/`
# on the worker's server (`event_log`, the platform's `EventLog`). `segment_path`/`parse` name and read the
# media paths; `Manifest` is the per-camera index beside the footage — media lines only, since the events
# are indexed by the resource's event database; `ArchiveResource` promotes closed segments from the spool,
# repairs manifests from the files, retains media by days; `ArchivePolicy` is the hook the recorder
# registers with the platform's resource job (`vms/resource.py`).
#
# ## Module-level names
# - `SUB = "rec"` — the recorder's tree; `EVENTS_SUB = "vms"` — the worker's, for `event_log`.
# - `SEGMENT`, `EPOCH_DIR` — the path grammar: `<sub>/<unit>/e<epoch>/<YYYYMMDDTHHMMSSZ>.mp4`.
#
# ## Notes
# - `test_promote_then_line_then_spool_copy_gone`, `test_manifest_rebuilt_from_the_files_alone`,
#   `test_retention_file_first_then_line`, `test_timeline_marks_a_fenced_epoch_and_spans_two_resources` walk
#   these; `test_events_are_buckets_on_the_resource_recording_or_not` shows the two trees side by side.
# - `_move` renames within a filesystem and copies-then-replaces across one: a segment appears in the archive
#   whole or not at all, and the spool copy goes last.
# ================================================================================================
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone

from w2cplatform.events import EventLog, unit_dir

SUB = "rec"          # the recorder's tree: media and the manifest
EVENTS_SUB = "vms"   # the worker's tree: the camera's event buckets
SEGMENT = re.compile(r"^(\d{8}T\d{6}Z)\.mp4$")
EPOCH_DIR = re.compile(r"^e(\d+)$")


# `<root>/rec/<unit>/e<epoch>/<start>Z.mp4` — the same grammar in the spool and the archive.
#
# The middle segment is the UNIT, not the camera. They were the same string for a long time, because
# `rec.subsystem.yaml` said `id: cam` — a recording was named by the camera it recorded. The path code
# never knew that, and the day a camera got a second archive the unit became `7` and `7-cloud` and this
# grammar kept working unchanged. Naming the argument `cam` and casting it with `int()` is how that day
# becomes a rewrite of four modules instead of one line of YAML.
def segment_path(root: str, unit: str, epoch: int, start: datetime) -> str:
    return os.path.join(root, SUB, str(unit), f"e{epoch}", start.strftime("%Y%m%dT%H%M%SZ") + ".mp4")


# The inverse: `(unit, epoch, start)` or `None` for anything that is not a segment path under `root`.
def parse(path: str, root: str) -> tuple[str, int, datetime] | None:
    rel = os.path.relpath(path, root).split(os.sep)
    if len(rel) != 4 or rel[0] != SUB or not rel[1] or not EPOCH_DIR.match(rel[2]):
        return None
    m = SEGMENT.match(rel[3])
    if not m:
        return None
    return rel[1], int(rel[2][1:]), datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def event_log(root: str, cam, epoch: int, bucket_seconds: int = 600) -> EventLog:
    """The camera's event log on this resource: what the worker holding the
    camera's epoch writes into, recording or not — `vms/<cam>/`, not `rec/`."""
    return EventLog(root, EVENTS_SUB, str(cam), epoch, bucket_seconds)


# One promoted segment: `cam`, `epoch`, `start`/`end` (unix seconds), `path` (relative to the archive root),
# `bytes`.
@dataclass(frozen=True)
class Segment:
    unit: str             # the recording this footage belongs to — `7`, or `7-cloud`: the operator's name
    epoch: int
    start: float          # unix seconds
    end: float
    path: str             # relative to the archive root
    bytes: int
    # `live` (written from the fan-out) or `edge` (fetched from the camera's own card or its NVR — Lesson
    # 16). It lives in the LINE, not in the path: the path grammar is what `repair` rebuilds a manifest
    # from, and a repaired line loses `source` — a named cost, paid to keep that grammar untouched. The
    # footage stays where it is and plays the same; only the provenance mark is gone.
    source: str = "live"

    def line(self) -> str:
        return json.dumps({"kind": "media", "unit": self.unit, "epoch": self.epoch, "start": self.start, "end": self.end,
                           "path": self.path, "bytes": self.bytes, "source": self.source})

    @classmethod
    def from_line(cls, line: str) -> "Segment":
        d = json.loads(line)
        unit = d.get("unit", d.get("cam"))        # `cam` is what a line written before the unit-keyed tree says
        return cls(str(unit), int(d["epoch"]), float(d["start"]), float(d["end"]), d["path"], int(d["bytes"]),
                   d.get("source", "live"))


# Per unit, append-only, beside the footage: `<archive>/rec/<unit>/manifest.jsonl`. Media lines only.
class Manifest:
    """Per unit, append-only, beside the footage."""

    def __init__(self, archive_root: str, unit):
        self.path = os.path.join(unit_dir(archive_root, SUB, str(unit)), "manifest.jsonl")

    def append(self, entry: Segment) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a") as f:
            f.write(entry.line() + "\n")

    def _lines(self) -> list[str]:
        try:
            with open(self.path) as f:
                return [l for l in f if l.strip()]
        except FileNotFoundError:
            return []

    def read(self) -> list[Segment]:
        """The media lines — what a player needs."""
        return [Segment.from_line(l) for l in self._lines() if json.loads(l).get("kind", "media") == "media"]

    # Replaces the file atomically (write `.tmp`, `os.replace`), sorted by `(start, epoch)`.
    def rewrite(self, segs: list[Segment]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            for e in sorted(segs, key=lambda e: (e.start, e.epoch)):
                f.write(e.line() + "\n")
        os.replace(tmp, self.path)

    # Spans overlapping `[t0, t1)` as `{start, end, media: path, epoch, fenced}`; `fenced` is true when
    # `current_epoch` is given and the span's epoch is older — how the page shows a zombie's footage. Two
    # resources' timelines simply concatenate and sort — the console merges manifests.
    def timeline(self, t0: float, t1: float, current_epoch: int | None = None) -> list[dict]:
        """Media spans overlapping [t0, t1), each marked *fenced* if its epoch is
        older than the recorder's current one. Events are not here: the resource's
        event database has them, and the console draws them over these spans."""
        out = []
        for s in self.read():
            if s.end > t0 and s.start < t1:
                out.append({"start": s.start, "end": s.end, "media": s.path, "epoch": s.epoch, "source": s.source,
                            "fenced": current_epoch is not None and s.epoch < current_epoch})
        return sorted(out, key=lambda d: (d["start"], d["epoch"]))


# One server's archive: a spool root and an archive root, both created on construction.
class ArchiveResource:
    """One server's archive. `promote()` is what archivesink calls on
    fragment-closed; `repair()` is what М11 called the re-index sweep."""

    # `create=False` is a handle on an archive that is away right now: the spool is local and is made, the
    # root is not touched. Promotion makes its own directories segment by segment, so a handle made this
    # way starts working the moment the archive answers — which is what a recorder holding a volume through
    # an outage needs (`RecWorker._write_into`).
    def __init__(self, spool_root: str, archive_root: str, bucket_seconds: int = 600, wall=None, create: bool = True):
        import time
        self.spool, self.root, self.bucket_seconds = spool_root, archive_root, bucket_seconds
        self.wall = wall or time.time
        os.makedirs(self.spool, exist_ok=True)
        if create:
            os.makedirs(self.root, exist_ok=True)

    # 1. the file into the archive (rename, or copy-then-replace across filesystems), 2. the manifest line,
    # 3. the spool copy gone — the order that survives a crash at any point.
    def promote(self, spool_path: str, end: float | None = None, source: str = "live") -> Segment:
        parsed = parse(spool_path, self.spool)
        if parsed is None:
            raise ValueError(f"not a segment path: {spool_path}")
        unit, epoch, start = parsed
        st = os.stat(spool_path)
        end = end if end is not None else st.st_mtime
        rel = os.path.relpath(spool_path, self.spool)
        dest = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        self._move(spool_path, dest)                    # 1. into the archive, atomically (same filesystem)
        seg = Segment(unit, epoch, start.timestamp(), end, rel, st.st_size, source)
        Manifest(self.root, unit).append(seg)           # 2. then the line
        return seg

    # The units with a `rec/<unit>/` directory. Strings, and sorted so that numeric names — which is most
    # of them, a recording taking its camera's number unless it was given another — come out in numeric
    # order rather than "1, 10, 2".
    def units(self) -> list[str]:
        try:
            names = [d for d in os.listdir(os.path.join(self.root, SUB))
                     if os.path.isdir(os.path.join(self.root, SUB, d))]
        except FileNotFoundError:
            return []
        return sorted(names, key=lambda d: (0, int(d), "") if d.isdigit() else (1, 0, d))

    @staticmethod
    def _move(src: str, dest: str) -> None:
        try:
            os.rename(src, dest)
        except OSError:
            shutil.copy2(src, dest + ".tmp")            # different filesystem: copy, then appear whole
            os.replace(dest + ".tmp", dest)
            os.remove(src)                              # 3. the spool copy, last

    def closed_in_spool(self, grace_seconds: float, now: float) -> list[str]:
        """Segments in the spool older than the grace: closed, not yet promoted
        (a recorder died between close and promote)."""
        out = []
        for d, _, files in os.walk(self.spool):
            for f in files:
                p = os.path.join(d, f)
                if parse(p, self.spool) and now - os.path.getmtime(p) >= grace_seconds:
                    out.append(p)
        return sorted(out)

    def repair(self) -> dict:
        """Make the manifests agree with the files: add lines for files no
        line names (with the epoch from the path), drop lines whose file is
        gone. Idempotent."""
        added = dropped = 0
        for unit in self.units():
            man = Manifest(self.root, unit)
            lines = {s.path: s for s in man.read()}
            present = {}
            for d, _, files in os.walk(unit_dir(self.root, SUB, unit)):
                for f in files:
                    p = os.path.join(d, f)
                    parsed = parse(p, self.root)
                    if parsed:
                        present[os.path.relpath(p, self.root)] = parsed
            for rel, (u, epoch, start) in present.items():
                if rel not in lines:
                    st = os.stat(os.path.join(self.root, rel))
                    lines[rel] = Segment(u, epoch, start.timestamp(), st.st_mtime, rel, st.st_size)
                    added += 1
            for rel in list(lines):
                if rel not in present:
                    del lines[rel]
                    dropped += 1
            man.rewrite(list(lines.values()))
        return {"added": added, "dropped": dropped}

    # `[(start, end), …]` of what the manifest says we have, adjacent runs merged. `stitch` is the tolerance
    # that makes it work at all: between two segments there is always a seam — the fraction of a second it
    # takes to close one and open the next. Count a seam as a gap and a day of continuous ten-minute
    # recording has 144 of them.
    def coverage(self, unit, stitch: float = 2.0) -> list[tuple[float, float]]:
        runs: list[list[float]] = []
        for s in sorted(Manifest(self.root, unit).read(), key=lambda s: s.start):
            if runs and s.start <= runs[-1][1] + stitch:
                runs[-1][1] = max(runs[-1][1], s.end)
            else:
                runs.append([s.start, s.end])
        return [(a, b) for a, b in runs]

    def retain(self, unit, days: float, now: float) -> int:
        """Delete media older than `days`: the file first, then the line. The
        buckets are the platform's to retain (vms/retention/<cam>, written by the
        VMS controller); the resource's event database forgets their rows."""
        cutoff = now - days * 86400
        man = Manifest(self.root, unit)
        keep, removed = [], 0
        for s in man.read():
            if s.end < cutoff:
                try:
                    os.remove(os.path.join(self.root, s.path))
                except FileNotFoundError:
                    pass
                removed += 1
            else:
                keep.append(s)
        if removed:
            man.rewrite(keep)
        return removed

    # Bytes of media under the root, for the heartbeat.
    def usage(self) -> int:
        total = 0
        for d, _, files in os.walk(self.root):
            for f in files:
                p = os.path.join(d, f)
                if parse(p, self.root):
                    total += os.path.getsize(p)
        return total


# `want` minus every range in `have`. The one subtraction two things use: the console draws device coverage
# only where ours does not cover it (Lesson 15), and the recorder fetches only what it does not have (Lesson
# 16). One rule, two uses — they cannot drift apart.
def subtract(want: tuple[float, float], have: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = [want]
    for a, b in sorted(have):
        nxt = []
        for x, y in out:
            if b <= x or a >= y:
                nxt.append((x, y)); continue
            if a > x:
                nxt.append((x, min(a, y)))
            if b < y:
                nxt.append((max(b, x), y))
        out = nxt
    return [(a, b) for a, b in out if b > a]


def overlaps(have: list[tuple[float, float]], span: tuple[float, float]) -> bool:
    return any(a < span[1] and span[0] < b for a, b in have)


class ArchivePolicy:
    """What the recorder registers with the platform's resource job: repair the
    manifests, retain media per unit from the recording rows
    (`rec/recordings/<unit>`, `retention_days`; 30 for one with no row), and,
    when the disk is over its watermark, free bytes — `vms/space.py` decides
    which, and the platform only says how many."""

    def __init__(self, resource: ArchiveResource, vars_, objects=None, peers=None, server: str = "",
                 volumes: dict | None = None):
        # One archive tree per VOLUME on a box with several disks, and the resource says which one is
        # short when it asks. The single-disk case is this dict with one entry and nobody naming it: the
        # policy that was written before there were volumes reads exactly the same.
        self.res, self.vars = resource, vars_
        self.volumes = dict(volumes) if volumes else {}
        # Only `free` needs these: the peers' heartbeats say who writes what and who has room, and the
        # client carries the bytes. Absent, the policy still repairs and retains — an archive on a box
        # with no neighbours has nowhere to evacuate to and does not pretend otherwise.
        self.objects, self.peers, self.server = objects, peers, server

    # The resource's watermark, answered in the recorder's own terms. Evacuate what is not ours, then cut
    # above the floor, then report the shortfall — the order is in `vms/space.py`, and so is why.
    def free(self, need: int, now: float, min_days: float = 3.0, volume: str | None = None) -> dict:
        from .space import cut, evacuate
        # The resource measured a DISK, so the answer has to come off that disk: bytes freed on another
        # volume of the same box close nothing, because the recording that cannot write is on this one.
        res = self.volumes.get(volume, self.res) if volume else self.res
        freed, out = 0, {}
        if self.objects is not None and self.peers is not None and self.server:
            rep = evacuate(res, self.objects, self.peers, self.server, need, now, vars_=self.vars)
            freed += rep["freed"]
            out.update({"evacuated": rep["moved"], **({"skipped": rep["skipped"]} if "skipped" in rep else {})})
        if freed < need:
            rep = cut(res, need - freed, now, min_days)
            freed += rep["freed"]
            out["cut"] = rep["removed"]
        short = max(0, need - freed)
        if short:                                   # everything on the floor: said out loud, not cut into
            out["shortfall"] = short
        return {"freed": freed, **out}

    def pass_(self, now: float) -> dict:
        out, removed = {}, 0
        for res in (list(self.volumes.values()) or [self.res]):
            rep = res.repair()
            for k, v in rep.items():
                out[k] = out.get(k, 0) + v if isinstance(v, int) else v
            for unit in res.units():
                items, _ = self.vars.get(f"{SUB}/recordings/{unit}")   # the unit's own row: its retention, not the camera's
                days = int(items.get("retention_days", 30)) if items else 30
                removed += res.retain(unit, days, now)
        return {**out, "media_removed": removed}
