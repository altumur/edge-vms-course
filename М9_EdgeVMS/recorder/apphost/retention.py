"""Lesson 8, Step 3 — retention that runs while the disk is full.

Three rules, in the order a crash makes them matter:

  1. Drop the index BEFORE unlinking the files. A crash in between leaves
     orphaned files — wasteful, recoverable by a scan. The other order
     leaves index rows pointing at nothing: footage the console offers and
     cannot play. Never delete what you cannot prove is superseded.
  2. Partitions are created AHEAD of time. A missing partition is a
     recording outage ("no partition of relation segments found for row").
  3. If every camera is inside its window and the disk is still full,
     something gives — and the recorder SAYS which policy it applied, as an
     event, because a silent drop is indistinguishable from a bug.

`db` and `fs` are injected so the arithmetic runs in tests without Postgres.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

log = logging.getLogger("apphost.retention")

STOP_RECORDING, DEGRADE_RETENTION, BY_PRIORITY = "stop_recording", "degrade_retention", "by_priority"


class RetentionDb(Protocol):
    async def partitions(self, parent: str) -> list[tuple[str, date, date]]: ...
    async def ensure_partition(self, parent: str, month: date) -> str: ...
    async def paths_in_partition(self, part: str) -> list[str]: ...
    async def drop_partition(self, parent: str, part: str) -> None: ...
    async def expire_rows(self, camera_id: int, older_than: datetime) -> list[str]: ...
    async def oldest_segments(self, limit: int, priority_first: bool) -> list[dict]: ...
    async def delete_segment(self, path: str) -> None: ...
    async def retention_days(self) -> dict[int, int]: ...
    async def indexed_paths_under(self, prefix: str) -> set[str]: ...
    async def log_event(self, kind: str, camera_id: int | None = None, payload: dict | None = None) -> None: ...


class Fs(Protocol):
    def usage(self, path: str) -> float: ...          # fraction used, 0..1
    def unlink(self, path: str) -> None: ...
    def walk_files(self, root: str) -> list[tuple[str, float]]: ...   # (path, mtime)


class RealFs:
    def usage(self, path: str) -> float:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return 1.0 - free / total if total else 0.0

    def unlink(self, path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def walk_files(self, root: str) -> list[tuple[str, float]]:
        out = []
        for d, _, files in os.walk(root):
            for f in files:
                p = os.path.join(d, f)
                try:
                    out.append((p, os.path.getmtime(p)))
                except OSError:
                    pass
        return out


@dataclass
class RetentionReport:
    partitions_created: list[str] = field(default_factory=list)
    partitions_dropped: list[str] = field(default_factory=list)
    files_unlinked: int = 0
    bytes_freed_by_policy: int = 0
    policy_applied: str | None = None
    recording_allowed: bool = True
    degraded_cameras: dict[int, datetime] = field(default_factory=dict)   # cid -> new oldest
    orphans: list[str] = field(default_factory=list)
    events: list[tuple[str, int | None, dict]] = field(default_factory=list)


def _months(start: date, n: int) -> list[date]:
    out, y, m = [], start.year, start.month
    for _ in range(n):
        out.append(date(y, m, 1))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


async def ensure_partitions(db: RetentionDb, now: datetime, ahead: int, report: RetentionReport) -> None:
    existing = {p[0] for parent in ("segments", "events") for p in await db.partitions(parent)}
    for parent in ("segments", "events"):
        for month in _months(now.date(), ahead + 1):
            name = f"{parent}_{month:%Y_%m}"
            if name not in existing:
                await db.ensure_partition(parent, month)
                report.partitions_created.append(name)


async def _unlink_all(fs: Fs, paths: list[str], report: RetentionReport) -> None:
    for p in paths:
        fs.unlink(p)
        report.files_unlinked += 1


async def enforce_retention(db: RetentionDb, fs: Fs, settings, now: datetime | None = None,
                            archive_dir: str | None = None) -> RetentionReport:
    now = now or datetime.now(timezone.utc)
    archive_dir = archive_dir or settings.archive_dir
    report = RetentionReport()

    # 0. Partitions ahead. Cheap, and its absence is an outage.
    await ensure_partitions(db, now, settings.partitions_ahead, report)

    retention = await db.retention_days()
    if not retention:
        return report
    longest = max(retention.values())

    # 1. Whole partitions older than the LONGEST retention on any camera:
    #    remember the paths, detach + drop (5 ms), then unlink.
    cutoff = now - timedelta(days=longest)
    for name, _lo, hi in await db.partitions("segments"):
        if datetime(hi.year, hi.month, hi.day, tzinfo=timezone.utc) <= cutoff:
            paths = await db.paths_in_partition(name)          # remember first
            await db.drop_partition("segments", name)
            report.partitions_dropped.append(name)
            await _unlink_all(fs, paths, report)                 # then unlink
    for name, _lo, hi in await db.partitions("events"):
        if datetime(hi.year, hi.month, hi.day, tzinfo=timezone.utc) <= cutoff:
            await db.drop_partition("events", name)
            report.partitions_dropped.append(name)

    # 2. Per-camera windows shorter than the longest: rows first, files after.
    for cid, days in retention.items():
        if days < longest:
            paths = await db.expire_rows(cid, now - timedelta(days=days))
            await _unlink_all(fs, paths, report)

    # 3. The disk is still full. Policy decides, and the recorder says so.
    if fs.usage(archive_dir) >= settings.disk_high_water:
        await _apply_policy(db, fs, settings, now, archive_dir, report)

    # 4. Orphan sweep: files with no index row. The safe direction of rule 1
    #    produces these on a crash; they are reported, and unlinked only once
    #    old enough that no pipeline could still be writing them.
    indexed = await db.indexed_paths_under(archive_dir)
    grace = 2 * settings.segment_seconds
    for path, mtime in fs.walk_files(archive_dir):
        if path not in indexed and now.timestamp() - mtime > grace:
            report.orphans.append(path)
    if report.orphans:
        report.events.append(("archive.orphans", None, {"count": len(report.orphans)}))

    for kind, cid, payload in report.events:
        await db.log_event(kind, cid, payload)
    return report


async def _apply_policy(db: RetentionDb, fs: Fs, settings, now: datetime,
                        archive_dir: str, report: RetentionReport) -> None:
    policy = settings.disk_full_policy
    report.policy_applied = policy
    if policy == STOP_RECORDING:
        # Honour every window; refuse new segments. Footage is evidence and a
        # gap is better than a missing week.
        report.recording_allowed = False
        report.events.append(("retention.stopped", None,
                              {"policy": policy, "usage": round(fs.usage(archive_dir), 3)}))
        return

    # DEGRADE_RETENTION: oldest first, across all cameras.
    # BY_PRIORITY: lowest priority first, then oldest within it.
    target = settings.disk_high_water - 0.05
    freed = 0
    while fs.usage(archive_dir) > target:
        batch = await db.oldest_segments(limit=50, priority_first=(policy == BY_PRIORITY))
        if not batch:
            report.recording_allowed = False          # nothing left to give
            break
        for seg in batch:
            if fs.usage(archive_dir) <= target:
                break                                 # give up exactly as much as needed
            await db.delete_segment(seg["path"])     # index first
            fs.unlink(seg["path"])                    # then file
            report.files_unlinked += 1
            freed += seg["bytes"]
            report.degraded_cameras[seg["camera_id"]] = seg["start"]
    report.bytes_freed_by_policy = freed
    report.events.append(("retention.degraded", None, {
        "policy": policy, "bytes_freed": freed,
        "cameras": {str(c): s.isoformat() for c, s in report.degraded_cameras.items()}}))
