"""Lesson 8, Step 3 — retention under pressure, policy honoured, event logged."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from apphost.retention import enforce_retention, ensure_partitions, RetentionReport
from tests.conftest import FakeDb, FakeFs, LoggingFs, settings

UTC = timezone.utc
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
ARCH = "/tmp/recorder-test-archive"


def _db(cameras=None, months=("2026_06", "2026_07", "2026_08", "2026_09")):
    db = FakeDb(cameras or {1: {"retention_days": 30, "priority": 100},
                            2: {"retention_days": 7, "priority": 10}})
    for m in months:
        y, mo = map(int, m.split("_"))
        db.add_partition("segments", date(y, mo, 1))
        db.add_partition("events", date(y, mo, 1))
    return db


def _fill(db, fs, cid, days_back, count=10, size=1000):
    for i in range(count):
        start = NOW - timedelta(days=days_back, minutes=10 * i)
        path = f"{ARCH}/{cid}/e1/{start:%Y%m%dT%H%M%SZ}.mp4"
        db.add_segment(cid, start, path, size)
        fs.put(path, size, mtime=start.timestamp())


async def test_partitions_are_created_ahead():
    db = _db(months=("2026_09",))
    rep = RetentionReport()
    await ensure_partitions(db, NOW, 2, rep)
    assert rep.partitions_created == ["segments_2026_10", "segments_2026_11",
                                      "events_2026_10", "events_2026_11"]
    rep2 = RetentionReport()
    await ensure_partitions(db, NOW, 2, rep2)
    assert rep2.partitions_created == []                 # idempotent


async def test_whole_partitions_older_than_longest_window_are_dropped_then_unlinked():
    db, fs = _db(), None
    fs = LoggingFs(10**9, db)
    _fill(db, fs, 1, days_back=80)                       # June: outside every window
    _fill(db, fs, 1, days_back=5)                        # September: inside
    rep = await enforce_retention(db, fs, settings(), now=NOW, archive_dir=ARCH)
    assert rep.partitions_dropped == ["segments_2026_06", "segments_2026_07",
                                      "events_2026_06", "events_2026_07"]
    assert rep.files_unlinked == 10
    assert len(db.all_rows()) == 10
    # ORDER: index dropped before the files were unlinked. A crash between the
    # two leaves orphans, never index rows pointing at nothing.
    first_unlink = db.log.index(next(l for l in db.log if l.startswith("unlink")))
    assert db.log.index("drop segments_2026_06") < first_unlink
    assert rep.policy_applied is None


async def test_per_camera_window_shorter_than_longest():
    db = _db()
    fs = LoggingFs(10**9, db)
    _fill(db, fs, 2, days_back=10)                       # camera 2: 7-day window -> expired
    _fill(db, fs, 1, days_back=10)                       # camera 1: 30-day window -> kept
    rep = await enforce_retention(db, fs, settings(), now=NOW, archive_dir=ARCH)
    # June and July are empty and older than the longest window: dropped.
    # September holds live rows for camera 1 and stays.
    assert rep.partitions_dropped == ["segments_2026_06", "segments_2026_07",
                                      "events_2026_06", "events_2026_07"]
    assert rep.files_unlinked == 10
    assert {r["camera_id"] for r in db.all_rows()} == {1}
    # rows first, then files
    assert db.log.index(next(l for l in db.log if l.startswith("delete-row"))) < \
           db.log.index(next(l for l in db.log if l.startswith("unlink")))


async def test_disk_full_degrades_by_policy_and_says_so():
    db = _db()
    fs = FakeFs(capacity=20_000)                         # 20 segments of 1000 fill it
    _fill(db, fs, 1, days_back=3, count=10)
    _fill(db, fs, 2, days_back=1, count=10)              # everything inside its window
    assert fs.usage(ARCH) == 1.0
    rep = await enforce_retention(db, fs, settings(disk_full_policy="degrade_retention"),
                                  now=NOW, archive_dir=ARCH)
    assert rep.policy_applied == "degrade_retention"
    assert rep.recording_allowed is True                 # still recording
    assert fs.usage(ARCH) <= 0.90
    assert ("retention.degraded", None) in [(k, c) for k, c, _ in db.events]   # and it said so
    # oldest first, across cameras: camera 1's three-day-old footage went first
    assert 1 in rep.degraded_cameras
    assert rep.files_unlinked == len(fs.unlinked) > 0


async def test_disk_full_by_priority_sacrifices_the_car_park_first():
    db = _db({1: {"retention_days": 30, "priority": 100},          # safe room
              2: {"retention_days": 30, "priority": 10}})          # car park
    fs = FakeFs(capacity=20_000)
    _fill(db, fs, 1, days_back=5, count=10)                        # older, high priority
    _fill(db, fs, 2, days_back=1, count=10)                        # newer, low priority
    rep = await enforce_retention(db, fs, settings(disk_full_policy="by_priority"),
                                  now=NOW, archive_dir=ARCH)
    assert set(rep.degraded_cameras) == {2}                        # only the car park gave
    assert len([r for r in db.all_rows() if r["camera_id"] == 1]) == 10   # the safe room kept all
    assert fs.usage(ARCH) <= 0.90
    assert 0 < len(fs.unlinked) < 10                               # and only as much as needed


async def test_disk_full_stop_recording_honours_every_window():
    db = _db()
    fs = FakeFs(capacity=20_000)
    _fill(db, fs, 1, days_back=3, count=10)
    _fill(db, fs, 2, days_back=1, count=10)
    rep = await enforce_retention(db, fs, settings(disk_full_policy="stop_recording"),
                                  now=NOW, archive_dir=ARCH)
    assert rep.recording_allowed is False
    assert len(db.all_rows()) == 20                                # nothing sacrificed
    assert fs.unlinked == []
    assert any(k == "retention.stopped" for k, _, _ in db.events)


async def test_orphans_are_reported_not_silently_deleted():
    db = _db()
    fs = FakeFs(10**9)
    _fill(db, fs, 1, days_back=1, count=2)
    fs.put(f"{ARCH}/1/e1/orphan-old.mp4", 500, mtime=(NOW - timedelta(hours=3)).timestamp())
    fs.put(f"{ARCH}/1/e1/orphan-open.mp4", 500, mtime=(NOW - timedelta(minutes=5)).timestamp())
    rep = await enforce_retention(db, fs, settings(), now=NOW, archive_dir=ARCH)
    assert rep.orphans == [f"{ARCH}/1/e1/orphan-old.mp4"]          # the open one is left alone
    assert fs.unlinked == []                                       # reported, not unlinked
    assert ("archive.orphans", None, {"count": 1}) in db.events


def test_bad_policy_is_rejected_at_startup():
    with pytest.raises(ValueError):
        settings(disk_full_policy="hope")
