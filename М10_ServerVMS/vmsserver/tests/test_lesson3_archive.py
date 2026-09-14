"""Lesson 3 — the archive as a resource: promote, manifest, repair, retention."""
import os
from datetime import datetime, timezone
from vms.archive import ArchiveResource, Manifest, parse, segment_path
from tests.conftest import Box


def utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def write_segment(root, cam, epoch, start, size=1000, mtime=None):
    p = segment_path(root, cam, epoch, utc(start))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"\0" * size)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def test_parse_and_paths():
    assert parse("/a/vms/7/e5/20260912T101000Z.mp4", "/a") == (7, 5, utc("2026-09-12T10:10:00"))
    assert parse("/a/vms/7/e5/manifest.jsonl", "/a") is None and parse("/a/7/e5/20260912T101000Z.mp4", "/a") is None


def test_promote_is_the_acknowledgement_order():
    box = Box(); res = ArchiveResource(box.spool, box.archive)
    p = write_segment(box.spool, 7, 3, "2026-09-12T10:00:00", mtime=utc("2026-09-12T10:10:00").timestamp())
    seg = res.promote(p)
    assert not os.path.exists(p)                                              # 3. gone from the spool, last
    assert os.path.exists(os.path.join(box.archive, seg.path))                # 1. in the archive, whole
    lines = Manifest(box.archive, 7).read()                                    # 2. named in the manifest
    assert len(lines) == 1 and lines[0].epoch == 3 and lines[0].end - lines[0].start == 600 and lines[0].bytes == 1000


def test_kill_mid_segment_open_lost_closed_kept():
    """Seven minutes of a ten-minute segment length: six closed segments
    promoted (or still in the spool, closed), one open segment lost."""
    box = Box(); res = ArchiveResource(box.spool, box.archive)
    now = utc("2026-09-12T12:00:00").timestamp()
    for m in range(0, 60, 10):                                                 # 6 closed, promoted in time
        p = write_segment(box.spool, 7, 3, f"2026-09-12T11:{m:02d}:00", mtime=now - 3600 + (m + 10) * 60)
        res.promote(p)
    late = write_segment(box.spool, 7, 3, "2026-09-12T12:00:00", mtime=now - 60)        # closed, worker died before promote
    write_segment(box.spool, 7, 3, "2026-09-12T12:10:00", size=10, mtime=now - 5)      # the open one
    assert res.closed_in_spool(grace_seconds=30, now=now) == [late]           # what the restart promotes
    res.promote(late)
    assert len(Manifest(box.archive, 7).read()) == 7
    # the open segment is what a kill loses: up to one segment length, the number М9 Lesson 4 stated
    assert res.closed_in_spool(grace_seconds=30, now=now) == []


def test_manifest_rebuilt_from_the_files_alone():
    box = Box(); res = ArchiveResource(box.spool, box.archive)
    for m in ("10:00:00", "10:10:00", "10:20:00"):
        res.promote(write_segment(box.spool, 7, 3, f"2026-09-12T{m}"))
    orig = Manifest(box.archive, 7).read()
    os.remove(Manifest(box.archive, 7).path)                                   # the index did not travel
    rep = res.repair()
    assert rep == {"added": 3, "dropped": 0}
    rebuilt = Manifest(box.archive, 7).read()
    assert [(s.path, s.epoch, s.start) for s in rebuilt] == [(s.path, s.epoch, s.start) for s in orig]
    os.remove(os.path.join(box.archive, orig[0].path))                        # a file went missing under a line
    assert res.repair() == {"added": 0, "dropped": 1} and len(Manifest(box.archive, 7).read()) == 2
    assert res.repair() == {"added": 0, "dropped": 0}                          # idempotent


def test_timeline_marks_a_fenced_epoch_and_spans_two_resources():
    box = Box(); res = ArchiveResource(box.spool, box.archive)
    res.promote(write_segment(box.spool, 7, 3, "2026-09-12T10:00:00", mtime=utc("2026-09-12T10:10:00").timestamp()))
    res.promote(write_segment(box.spool, 7, 4, "2026-09-12T10:10:00", mtime=utc("2026-09-12T10:20:00").timestamp()))
    res.promote(write_segment(box.spool, 7, 3, "2026-09-12T10:10:00", mtime=utc("2026-09-12T10:15:00").timestamp()))  # the zombie's
    tl = Manifest(box.archive, 7).timeline(utc("2026-09-12T10:05:00").timestamp(), utc("2026-09-12T10:30:00").timestamp(), current_epoch=4)
    assert [(t["epoch"], t["fenced"]) for t in tl] == [(3, True), (3, True), (4, False)]
    # a second resource (another server) holds later footage: the console merges two manifests
    other = ArchiveResource(box.spool + "2", box.archive + "2")
    other.promote(write_segment(other.spool, 7, 5, "2026-09-12T10:20:00", mtime=utc("2026-09-12T10:30:00").timestamp()))
    merged = sorted(Manifest(box.archive, 7).timeline(0, 1e12) + Manifest(other.root, 7).timeline(0, 1e12), key=lambda t: t["start"])
    assert [t["epoch"] for t in merged] == [3, 3, 4, 5]


def test_retention_is_a_policy_on_the_resource():
    box = Box(); res = ArchiveResource(box.spool, box.archive)
    now = utc("2026-10-20T00:00:00").timestamp()
    for day in (1, 10, 19):
        res.promote(write_segment(box.spool, 7, 3, f"2026-10-{day:02d}T10:00:00", mtime=utc(f"2026-10-{day:02d}T10:10:00").timestamp()))
    assert res.retain(7, days=8, now=now) == 2                              # cutoff 12 Oct: the 1st and the 10th go
    left = Manifest(box.archive, 7).read()
    assert len(left) == 1 and not os.path.exists(os.path.join(box.archive, "vms", "7", "e3", "20261001T100000Z.mp4"))
    assert res.usage() == 1000


def test_events_are_buckets_on_the_resource_recording_or_not():
    """The archive's unit is a time span under an epoch, not a media file. A
    camera that is watched and never recorded has event buckets; a camera that
    went silent has no segment open, and the event that says so goes into its
    bucket. Buckets are written in place, indexed when closed, counted onto a
    recorded span when one overlaps, fenced by the epoch, rebuilt by repair,
    retained by their own policy. No controller wrote any of it."""
    from vms.archive import event_log
    from psimplatform.events import parse_bucket, read_bucket
    box = Box(); res = ArchiveResource(box.spool, box.archive, wall=lambda: box.wall())
    t0 = utc("2026-09-12T10:00:00").timestamp()
    box.wall.t = t0 + 2000                                                     # the resource's clock: every bucket below is over
    log = event_log(box.archive, 7, 3)                                          # the worker holds epoch 3 for camera 7
    p = log.append(t0 + 12.5, "motion", zone="gate")                            # not recording: still an event
    assert parse_bucket(p, box.archive) == ("vms", "7", 3, t0) and read_bucket(p)[0]["zone"] == "gate"
    log.append(t0 + 40.0, "silent")                                             # the event with no segment, by definition
    p2 = log.append(t0 + 700.0, "person", score=0.9)                            # the next bucket: rolled by the clock
    for f in (p, p2): os.utime(f, (t0 + 1250, t0 + 1250))                       # quiet for the grace (the test's clock is not the disk's)
    assert Manifest(box.archive, 7).buckets() == []                             # durable already; not yet indexed
    assert [b.events for b in res.close_buckets(now=t0 + 1300)] == [2, 1]      # both spans over and quiet: indexed
    assert res.close_buckets(now=t0 + 1300) == []                               # idempotent
    tl = Manifest(box.archive, 7).timeline(t0, t0 + 1200, current_epoch=4)
    assert [(x["media"], x["events"], x["fenced"]) for x in tl] == [(None, 2, True), (None, 1, True)]   # watched, not recorded; fenced
    # now the camera IS recorded for the second span: the events count onto the media
    seg = res.promote(write_segment(box.spool, 7, 4, "2026-09-12T10:10:00", mtime=t0 + 1200))
    log4 = event_log(box.archive, 7, 4); p4 = log4.append(t0 + 650.0, "motion"); os.utime(p4, (t0 + 1900, t0 + 1900)); res.close_buckets(now=t0 + 2000)
    tl = Manifest(box.archive, 7).timeline(t0 + 600, t0 + 1200, current_epoch=4)
    assert [(x["media"] is not None, x["epoch"], x["events"], x["fenced"]) for x in tl] == [(False, 3, 1, True), (True, 4, 1, False)]
    # repair rebuilds both kinds from the files; media retention is the VMS's, bucket retention the platform's
    from psimplatform.resource import Resource
    os.remove(Manifest(box.archive, 7).path)
    assert res.repair() == {"added": 4, "dropped": 0}
    assert res.retain(7, days=1, now=t0 + 3 * 86400) == 1 and len(Manifest(box.archive, 7).buckets()) == 3
    box.vars.put("vms/retention/7", {"days": 30})                               # what the controller writes for a camera's events
    platform = Resource(box.archive, "box", "http://box", box.vars, box.objects, wall=lambda: t0 + 40 * 86400)
    assert platform.retain() == 3 and not os.path.exists(p)                     # files, by the platform...
    assert res.repair() == {"added": 0, "dropped": 3}                           # ...lines, by the VMS's own pass
