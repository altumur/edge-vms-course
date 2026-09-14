"""The archive as a resource — server-bound, no controller, a policy.

    <spool>/vms/<cam>/e<epoch>/<start>Z.mp4        the open segment, and closed ones not yet promoted
    <archive>/vms/<cam>/e<epoch>/<start>Z.mp4      promoted: the resource's media
    <archive>/vms/<cam>/e<epoch>/<start>Z.events.jsonl
                                                   the camera's EVENT BUCKETS — the platform's event log
                                                   (psimplatform.events), written by the worker holding the
                                                   camera's epoch, whether or not it is recording
    <archive>/vms/<cam>/manifest.jsonl             one line per media segment and one per closed event bucket:
                                                   the index that lives beside the footage

The archive's unit is a TIME SPAN under an epoch, not a media file. A span
may hold media (archivesink wrote it), events (the worker observed
something — motion, silence, an operator's mark), or both. A camera that
is watched and never recorded still has buckets. A camera that went silent
has no segment open — and the event that says so goes into its bucket.

The acknowledgement order is М9 Lesson 4's: a closed segment is PROMOTED
(renamed into the archive, then a manifest line appended), and the spool
copy is gone only after that. An event bucket is written in place on the
resource, one flushed line at a time, and gets its manifest line when it
closes (its span is over and nothing has touched it for a grace period).
The manifest is append-only and rebuildable from the files.

Retention is a policy, per kind. Media is the VMS's: `retain()` after
`retention_days`, files first then lines. Buckets are the PLATFORM's
(psimplatform.resource): the controller writes `vms/retention/<cam>
{days: events_retention_days}` and the resource job deletes the files —
events are small and often kept a year where footage is kept a month —
and `repair()` drops the lines whose files are gone.

The layout is `<subsystem>/<unit>/...` — the platform resource's tree — so
that other subsystems' buckets sit on the same server under their own
prefix. `ArchivePolicy` is what the VMS registers with the platform's
resource job: its own pass over its own part of the tree.
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # archive.py — the VMS's part of the resource under `vms/<cam>/`: spool → promote → manifest, the camera's
# event buckets, and the retention policy
#
# **Role in the module.** Lesson 3. The archive is server-bound and has no controller; it has a layout and a
# policy. The layout, from the docstring:
#
# - `<spool>/vms/<cam>/e<epoch>/<start>Z.mp4` — the open segment, and closed ones not yet promoted
#   (archivesink writes here).
# - `<archive>/vms/<cam>/e<epoch>/<start>Z.mp4` — promoted: the resource's media.
# - `<archive>/vms/<cam>/e<epoch>/<start>Z.events.jsonl` — the camera's event buckets: the platform's event
#   log (`psimplatform.events`, see `events.py`), written by the worker holding the camera's epoch, recording
#   or not.
# - `<archive>/vms/<cam>/manifest.jsonl` — one line per media segment and one per closed event bucket: the
#   index that lives beside the footage.
#
# The archive's unit is a *time span under an epoch*, not a media file: a span may hold media, events, or
# both; a watched-but-never-recorded camera still has buckets; a camera that went silent has no segment
# open, and the `silent` event goes into its bucket. The acknowledgement order is М9 Lesson 4's: a closed
# segment is promoted (renamed into the archive, then a manifest line appended) and the spool copy is gone
# only after that; a bucket is written in place one flushed line at a time and gets its manifest line when
# it closes. The manifest is append-only and rebuildable from the files. Retention is a policy per kind:
# media is the VMS's (`retain`, by `retention_days`, files first then lines); buckets are the platform's
# (`psimplatform.resource` deletes files by `vms/retention/<cam>`, and `repair` drops the orphaned lines).
# The tree is `<subsystem>/<unit>/…`, the platform resource's, so other subsystems' buckets sit on the same
# server under their own prefix. Used by `gstvms/archivesink.py` (`promote` on fragment-closed), `worker.py`
# (`event_log`), `console.py` (`Manifest.timeline`, `root`), `__main__` (`closed_in_spool` on worker start;
# `repair`/`close_buckets`/`retain` in `retain`), and `ArchivePolicy` is what the VMS registers with the
# platform's `Resource`.
#
# ## Module-level names
# - `SUB = "vms"` — the subsystem directory under both roots.
# - `SEGMENT` — regex for a segment filename `YYYYMMDDTHHMMSSZ.mp4`.
# - `EPOCH_DIR` — regex `e<digits>`.
#
# ## Notes
# - Every path carries the epoch: `e<epoch>` between the camera and the file. That is the fence made visible
#   on disk — a zombie's segment and a zombie's bucket are in their own epoch directory, and the timeline
#   marks them.
# - Ordering that matters: `_move` before `Manifest.append` in `promote`; file removal before `rewrite` in
#   `retain`; `close_buckets` only after the span is over *and* the file is quiet for the grace.
# - On one box, `ArchivePolicy` is not wired up by `__main__`; `retain` runs the same three steps by hand
#   and then the platform's bucket half (`Resource.retain`) in the same pass.
#   `test_events_are_buckets_on_the_resource_recording_or_not` exercises that half directly with
#   `vms/retention/7 {days: 30}`.
# ================================================================================================
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone

from psimplatform.events import Bucket, EventLog, buckets_under, read_bucket, unit_dir

SUB = "vms"
SEGMENT = re.compile(r"^(\d{8}T\d{6}Z)\.mp4$")
EPOCH_DIR = re.compile(r"^e(\d+)$")


# `<root>/vms/<cam>/e<epoch>/<start as %Y%m%dT%H%M%SZ>.mp4`. archivesink asks for it with the spool root;
# tests build spool files with it.
def segment_path(root: str, cam: int, epoch: int, start: datetime) -> str:
    return os.path.join(root, SUB, str(cam), f"e{epoch}", start.strftime("%Y%m%dT%H%M%SZ") + ".mp4")


# The inverse, relative to `root`: exactly four components, `vms`, a numeric camera, `e<n>`, and a `SEGMENT`
# name; the start is parsed as UTC. Anything else — a manifest, a bucket, a tmp file, a path outside `vms/`
# — is `None`, which is how every walker here ignores what it is not looking for. `test_parse_and_paths`.
def parse(path: str, root: str) -> tuple[int, int, datetime] | None:
    rel = os.path.relpath(path, root).split(os.sep)
    if len(rel) != 4 or rel[0] != SUB or not rel[1].isdigit() or not EPOCH_DIR.match(rel[2]):
        return None
    m = SEGMENT.match(rel[3])
    if not m:
        return None
    return int(rel[1]), int(rel[2][1:]), datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


# The camera's event log on this resource: `EventLog(root, "vms", str(cam), epoch, bucket_seconds)` — what
# the worker holding the camera's epoch writes into. The epoch is in the path, so a stale writer's lines are
# identifiable afterwards.
def event_log(root: str, cam: int, epoch: int, bucket_seconds: int = 600) -> EventLog:
    """The camera's event log on this resource: what the worker holding the
    camera's epoch writes into, recording or not."""
    return EventLog(root, SUB, str(cam), epoch, bucket_seconds)


# One media line of the manifest: `cam`, `epoch`, `start`, `end` (unix seconds), `path` (relative to the
# archive root), `bytes`.
@dataclass(frozen=True)
class Segment:
    cam: int
    epoch: int
    start: float          # unix seconds
    end: float
    path: str             # relative to the archive root
    bytes: int

    # JSON with `kind: "media"` and the fields.
    def line(self) -> str:
        return json.dumps({"kind": "media", "cam": self.cam, "epoch": self.epoch, "start": self.start, "end": self.end,
                           "path": self.path, "bytes": self.bytes})

    # The inverse, types coerced.
    @classmethod
    def from_line(cls, line: str) -> "Segment":
        d = json.loads(line)
        return cls(int(d["cam"]), int(d["epoch"]), float(d["start"]), float(d["end"]), d["path"], int(d["bytes"]))


# Parses a manifest line of `kind: "events"` (the form `Bucket.line()` writes) back into a platform
# `Bucket`. Same shape as `psimplatform.resource.bucket_from_line`.
def bucket_from_line(line: str) -> Bucket:
    d = json.loads(line)
    return Bucket(d["subsystem"], str(d["unit"]), int(d["epoch"]), float(d["start"]), float(d["end"]), d["path"], int(d["events"]))


# Per camera, append-only, beside the footage: `<archive>/vms/<cam>/manifest.jsonl`. Two kinds of line,
# media and events, distinguished by `kind` (a line without one is media, for files written before buckets
# existed).
class Manifest:
    """Per camera, append-only, beside the footage."""

    # Computes `self.path` via `psimplatform.events.unit_dir`; creates nothing.
    def __init__(self, archive_root: str, cam: int):
        self.path = os.path.join(unit_dir(archive_root, SUB, str(cam)), "manifest.jsonl")

    # Creates the directory if needed and appends `entry.line()` — a `Segment` or a `Bucket`.
    def append(self, entry) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a") as f:
            f.write(entry.line() + "\n")

    # Non-blank lines; `[]` if the file does not exist.
    def _lines(self) -> list[str]:
        try:
            with open(self.path) as f:
                return [l for l in f if l.strip()]
        except FileNotFoundError:
            return []

    # The media lines — what a player needs.
    def read(self) -> list[Segment]:
        """The media lines — what a player needs."""
        return [Segment.from_line(l) for l in self._lines() if json.loads(l).get("kind", "media") == "media"]

    # The closed event buckets — what an index needs.
    def buckets(self) -> list[Bucket]:
        """The closed event buckets — what an index needs."""
        return [bucket_from_line(l) for l in self._lines() if json.loads(l).get("kind") == "events"]

    # Replaces the file atomically (write `.tmp`, `os.replace`) with the given segments plus the given
    # buckets (or the current bucket lines if `None`), sorted by `(start, epoch)`. Called by `repair` and
    # `retain`.
    def rewrite(self, segs: list[Segment], buckets: list[Bucket] | None = None) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            for e in sorted(list(segs) + list(buckets if buckets is not None else self.buckets()), key=lambda e: (e.start, e.epoch)):
                f.write(e.line() + "\n")
        os.replace(tmp, self.path)

    # Spans overlapping `[t0, t1)`: every media segment as `{start, end, media: path, epoch, events: 0,
    # fenced}`; then every closed bucket — if a media span of the same epoch overlaps it, the bucket's event
    # count is added onto that span (events during a recorded span are counted on it), otherwise the bucket
    # stands alone as `{…, media: None, events: n}` (the camera was watched, not recorded). `fenced` is true
    # when `current_epoch` is given and the span's epoch is older — how the page shows a zombie's footage.
    # Sorted by `(start, epoch)`. `test_timeline_marks_a_fenced_epoch_and_spans_two_resources`: epochs `[3
    # fenced, 3 fenced, 4]` against `current_epoch=4`; two resources' timelines simply concatenate and sort
    # — the console merges manifests. `test_events_are_buckets…`: `(None, 2, True), (None, 1, True)` for two
    # watched-not-recorded buckets, and the epoch-4 bucket counted onto the epoch-4 segment.
    def timeline(self, t0: float, t1: float, current_epoch: int | None = None) -> list[dict]:
        """Spans overlapping [t0, t1): media segments, and event buckets with no
        media (the camera was watched, not recorded). Each marked *fenced* if its
        epoch is older than the current."""
        out = []
        for s in self.read():
            if s.end > t0 and s.start < t1:
                out.append({"start": s.start, "end": s.end, "media": s.path, "epoch": s.epoch, "events": 0,
                            "fenced": current_epoch is not None and s.epoch < current_epoch})
        for b in self.buckets():
            if b.end > t0 and b.start < t1:
                hit = next((o for o in out if o["media"] and o["epoch"] == b.epoch and o["start"] < b.end and b.start < o["end"]), None)
                if hit:
                    hit["events"] += b.events               # events during a recorded span: count them on it
                else:
                    out.append({"start": b.start, "end": b.end, "media": None, "epoch": b.epoch, "events": b.events,
                                "fenced": current_epoch is not None and b.epoch < current_epoch})
        return sorted(out, key=lambda d: (d["start"], d["epoch"]))


# One server's archive: a spool root and an archive root, both created on construction.
class ArchiveResource:
    """One server's archive. `promote()` is what archivesink calls on
    fragment-closed; `repair()` is what М11 called the re-index sweep."""

    # `wall` defaults to `time.time`; `repair` uses it to tell an open bucket from a closed one.
    def __init__(self, spool_root: str, archive_root: str, bucket_seconds: int = 600, wall=None):
        import time
        self.spool, self.root, self.bucket_seconds = spool_root, archive_root, bucket_seconds
        self.wall = wall or time.time
        os.makedirs(self.spool, exist_ok=True)
        os.makedirs(self.root, exist_ok=True)

    # What archivesink calls on `splitmuxsink-fragment-closed`, and what the worker's start-up calls for
    # leftovers. `parse` the spool path (`ValueError` if it is not a segment path), `end` defaults to the
    # file's mtime, then in order: 1. `_move` into the archive at the same relative path (atomic on one
    # filesystem); 2. append the `Segment` line to the camera's manifest. The spool copy disappears as part
    # of step 1 (rename) or last (copy path). `test_promote_is_the_acknowledgement_order`: gone from the
    # spool, present in the archive, one manifest line with `end − start == 600`.
    def promote(self, spool_path: str, end: float | None = None) -> Segment:
        parsed = parse(spool_path, self.spool)
        if parsed is None:
            raise ValueError(f"not a segment path: {spool_path}")
        cam, epoch, start = parsed
        st = os.stat(spool_path)
        end = end if end is not None else st.st_mtime
        rel = os.path.relpath(spool_path, self.spool)
        dest = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        self._move(spool_path, dest)                    # 1. into the archive, atomically (same filesystem)
        seg = Segment(cam, epoch, start.timestamp(), end, rel, st.st_size)
        Manifest(self.root, cam).append(seg)            # 2. then the line
        return seg

    # For every camera directory: every bucket on disk (`buckets_under`) that is not yet in the manifest,
    # whose span is over (`end <= now`) and whose file has not been touched for `grace_seconds`, gets its
    # manifest line. "The events were durable the moment they were written; this is the index catching up,
    # not an acknowledgement." Idempotent: the second call returns `[]`. Note the default `bucket_seconds`
    # here is the literal 600, not `self.bucket_seconds` — `ArchivePolicy.pass_` passes the instance's
    # explicitly; `__main__.retain` does not.
    def close_buckets(self, now: float, grace_seconds: float = 30.0, bucket_seconds: int = 600) -> list[Bucket]:
        """Event buckets whose span is over and that nobody has written to for
        the grace get their manifest line. The events were durable the moment
        they were written; this is the index catching up, not an acknowledgement."""
        closed = []
        for cam in self.cameras():
            man = Manifest(self.root, cam)
            known = {b.path for b in man.buckets()}
            for b in buckets_under(self.root, SUB, str(cam), bucket_seconds):
                p = os.path.join(self.root, b.path)
                if b.path in known or b.end > now or now - os.path.getmtime(p) < grace_seconds:
                    continue
                man.append(b); closed.append(b)
        return closed

    # Numeric directory names under `<archive>/vms/`, sorted; `[]` if none.
    def cameras(self) -> list[int]:
        try:
            return sorted(int(d) for d in os.listdir(os.path.join(self.root, SUB)) if d.isdigit())
        except FileNotFoundError:
            return []

    # `os.rename`; if that fails (different filesystem) `copy2` to `dest.tmp`, `os.replace` so the file
    # appears whole, then remove the source — the spool copy last.
    @staticmethod
    def _move(src: str, dest: str) -> None:
        try:
            os.rename(src, dest)
        except OSError:
            shutil.copy2(src, dest + ".tmp")            # different filesystem: copy, then appear whole
            os.replace(dest + ".tmp", dest)
            os.remove(src)                              # 3. the spool copy, last

    # Segment paths in the spool whose mtime is at least `grace_seconds` old: closed by the previous
    # instance but never promoted (it died between close and promote). `__main__.worker` promotes these
    # before starting. The open segment (recent mtime) is not listed — it is the one a kill loses, up to one
    # segment length. `test_kill_mid_segment_open_lost_closed_kept`: six promoted, one late one found and
    # promoted, the open one left; a second call finds nothing.
    def closed_in_spool(self, grace_seconds: float, now: float) -> list[str]:
        """Segments in the spool older than the grace: closed, not yet promoted
        (a worker died between close and promote)."""
        out = []
        for d, _, files in os.walk(self.spool):
            for f in files:
                p = os.path.join(d, f)
                if parse(p, self.spool) and now - os.path.getmtime(p) >= grace_seconds:
                    out.append(p)
        return sorted(out)

    # Make every camera's manifest agree with the files (М11 called it the re-index sweep). Media: walk the
    # camera's directory, add a `Segment` for every segment file no line names (epoch from the path, end
    # from mtime), drop every line whose file is gone. Buckets: every *closed* bucket on disk (`end <=
    # wall()`) is a line, every line without a file is dropped — an open bucket is still being written and
    # is left out. Then `rewrite`. Idempotent: `{added: 0, dropped: 0}` on the second run.
    # `test_manifest_rebuilt_from_the_files_alone`: the manifest deleted, `added: 3` rebuilds it
    # identically; a file removed under a line, `dropped: 1`. `test_events_are_buckets…`: `added: 4` (one
    # segment, three buckets) after the manifest is removed; `dropped: 3` after the platform's
    # `Resource.retain` deleted the bucket files.
    def repair(self) -> dict:
        """Make the manifests agree with the files: add lines for files no
        line names (with the epoch from the path), drop lines whose file is
        gone. Idempotent."""
        added = dropped = 0
        for cam in self.cameras():
            man = Manifest(self.root, cam)
            lines = {s.path: s for s in man.read()}
            present = {}
            for d, _, files in os.walk(unit_dir(self.root, SUB, str(cam))):
                for f in files:
                    p = os.path.join(d, f)
                    parsed = parse(p, self.root)
                    if parsed:
                        present[os.path.relpath(p, self.root)] = parsed
            for rel, (c, epoch, start) in present.items():
                if rel not in lines:
                    st = os.stat(os.path.join(self.root, rel))
                    lines[rel] = Segment(c, epoch, start.timestamp(), st.st_mtime, rel, st.st_size)
                    added += 1
            for rel in list(lines):
                if rel not in present:
                    del lines[rel]
                    dropped += 1
            # event buckets: every CLOSED bucket on disk is a line (an open one is still being written);
            # a line whose file is gone is dropped
            known = {b.path: b for b in man.buckets()}
            on_disk = {b.path: b for b in buckets_under(self.root, SUB, str(cam), self.bucket_seconds) if b.end <= self.wall()}
            added += sum(1 for pth in on_disk if pth not in known)
            dropped += sum(1 for pth in known if pth not in on_disk)
            man.rewrite(list(lines.values()), list(on_disk.values()))
        return {"added": added, "dropped": dropped}

    # Media retention, the VMS's own: for every media line with `end < now − days·86400`, remove the file (a
    # missing file is fine) and count it; if anything was removed, `rewrite` the manifest with the kept
    # segments (bucket lines untouched). Files first, then lines — the manifest never names a file that is
    # gone for long. Buckets are not this method's: they go by `vms/retention/<cam>` through the platform's
    # resource job, and `repair` drops their lines afterwards. `test_retention_is_a_policy_on_the_resource`:
    # days=8 removes the 1st and 10th, leaves the 19th; `usage()` is 1000 after.
    def retain(self, cam: int, days: float, now: float) -> int:
        """Delete media older than `days`: the file first, then the line. The
        buckets are the platform's to retain (vms/retention/<cam>, written by the
        controller); their lines go when repair() finds the files gone."""
        cutoff = now - days * 86400
        man = Manifest(self.root, cam)
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

    # Bytes of every segment file under the archive root (files `parse` recognises — buckets and manifests
    # not counted).
    def usage(self) -> int:
        total = 0
        for d, _, files in os.walk(self.root):
            for f in files:
                p = os.path.join(d, f)
                if parse(p, self.root):
                    total += os.path.getsize(p)
        return total


# What the VMS registers with the platform's resource job (`Resource.register("vms", ArchivePolicy(...))`):
# its own pass over its own part of the tree, run on the resource's timer beside the platform's own bucket
# retention and mirror.
class ArchivePolicy:
    """What the VMS registers with the platform's resource job: repair the
    manifests, close the buckets into them, retain media per camera from the
    camera rows. Runs on the resource's timer beside the platform's own pass."""

    # The `ArchiveResource` and a Variables reader (for the camera rows).
    def __init__(self, resource: ArchiveResource, vars_):
        self.res, self.vars = resource, vars_

    # `repair()`, then `close_buckets(now, bucket_seconds=self.res.bucket_seconds)`, then for each camera
    # directory read `vms/cameras/<cam>` and `retain(cam, retention_days or 30, now)`. Returns `{added,
    # dropped, closed, media_removed}`. A camera with no row (deleted rows are marked, not removed, so this
    # is a row that never existed) uses 30 days.
    def pass_(self, now: float) -> dict:
        rep = self.res.repair()
        closed = len(self.res.close_buckets(now, bucket_seconds=self.res.bucket_seconds))
        removed = 0
        for cam in self.res.cameras():
            items, _ = self.vars.get(f"{SUB}/cameras/{cam}")
            days = int(items.get("retention_days", 30)) if items else 30
            removed += self.res.retain(cam, days, now)
        return {**rep, "closed": closed, "media_removed": removed}
