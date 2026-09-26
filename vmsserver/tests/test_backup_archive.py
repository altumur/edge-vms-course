"""Lesson 26 — a backup archive of our own.

Lesson 16 closed a recording's gaps from the DEVICE's archive — whatever a camera keeps on its card, read
through the holder's playback door. The product found that door closed (its DriverPack binding has no
playback calls yet) and a better one open: our own archives. A critical camera is recorded twice — its
primary recording, and a BACKUP recording on a volume of kind `backup`: a second server's disk, or the
card of a camera that runs this platform. The primary closes any hole from the backup — a link that
dropped, the seconds its own recorder took to move — by COPYING the backup's footage, cut to the hole, with
the times it was recorded at. The mechanism is Lesson 16's, over a second kind of source.
"""
import os
import tempfile
from datetime import datetime, timezone

from vms import volumes
from vms.archive import ArchiveResource, Manifest, Segment, segment_path
from vms.config import REC_SPEC, SPEC
from vms.controller import VmsController
from vms.recworker import RecWorker
from vms.worker import FakeActuator, FakeDevice
from w2cplatform.contract import Heartbeat
from w2cplatform.spec import Refused, SpecController
from tests.test_lesson11_edge import CARD, _box, _holder


def _resource(box, server):
    import json
    box.objects.put(f"platform/resources/{server}/heartbeat",
                    json.dumps({"server": server, "ts": box.wall(), "url": f"http://{server}", "units": {}}).encode())


def _recorder(box, name, server, volume, act=None):
    arch = ArchiveResource(tempfile.mkdtemp(prefix=f"{name}-spool-"), tempfile.mkdtemp(prefix=f"{name}-arch-"), wall=box.wall)
    r = RecWorker(name, box.vars.as_writer(f"recworker-{name}", ["rec/epoch/*", "rec/slots/*"]), box.objects,
                  act or FakeActuator(), archive=arch, clock=box.clock, wall=box.wall, server=server,
                  env={"VOLUME": volume}, keep_days=1.0, settle=60.0)
    _resource(box, server)
    r.heartbeat_once()
    return r


def _footage(r, unit, spans):
    for start, end in spans:
        p = segment_path(r.archive.root, unit, r.epochs[unit], datetime.fromtimestamp(start, timezone.utc))
        os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(b"x")
        Manifest(r.archive.root, unit).append(Segment(unit, r.epochs[unit], start, end, os.path.relpath(p, r.archive.root), 1))


def _site(when="always", backup_act=None, card=None):
    """Camera 1, held on srv-1. Recorded twice: `1` on srv-a's disks, `1-copy` on srv-b's backup volume."""
    box, ctl, con, con_vars = _box()
    w = _holder(box, lambda k: FakeDevice(k, channels=["1"], coverage={"1": card} if card else None))   # by default a card we cannot read
    con.create_camera({"name": "front", "source": CARD})
    ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()
    volumes.write(box.vars, {"name": "disks", "kind": "local", "server": "srv-a", "url": "/data/a", "quota_bytes": 10**12})
    volumes.write(box.vars, {"name": "copy", "kind": "backup", "server": "srv-b", "url": "/data/b", "quota_bytes": 10**12})
    con_rec = SpecController(REC_SPEC, con_vars, box.objects, wall=box.wall)
    con_rec.create({"name": "1", "cam": "1", "home": "disks"})
    con_rec.create({"name": "1-copy", "cam": "1", "home": "copy", "when": when})
    primary = _recorder(box, "r-1", "srv-a", "disks")
    backup = _recorder(box, "r-2", "srv-b", "copy", backup_act)
    rec_ctl = SpecController(REC_SPEC, box.vars.as_writer("reccontroller", REC_SPEC.acl_controller()), box.objects, wall=box.wall)
    rec_ctl.ensure_placed()
    for r in (primary, backup):
        r.reconcile_once(); r.heartbeat_once()
    return box, rec_ctl, primary, backup, w


def test_the_primary_closes_its_gap_from_the_backup_recording():
    """The primary's recorder moved, and a hundred seconds are missing from it. The backup recording has
    them. The primary finds the backup in the heartbeats — a recording of the same camera, homed on a backup
    volume, whose recorder says what it holds and serves its archive — and copies exactly the hundred
    seconds, cut out of the backup's segment, with the times they were recorded at. What lands is ours:
    our manifest, our epoch, `source: backup`."""
    box, rec_ctl, primary, backup, _ = _site()
    now = box.wall()
    _footage(backup, "1-copy", ((now - 7200, now - 600),))
    _footage(primary, "1", ((now - 7200, now - 4000), (now - 3900, now - 600)))
    backup.serve_archive(); backup.heartbeat_once()

    src = primary.backup_sources({"id": "1", "cam": "1", "home": "disks"})
    assert [(s["recording"], s["recorder"]) for s in src] == [("1-copy", "r-2")]
    done = primary.backfill(budget=1, now=now, force=True)
    assert done == [{"unit": "1", "cam": "1", "from": now - 4000, "to": now - 3900, "segments": 1, "source": "backup:1-copy"}]
    assert [(c[1], c[2]) for c in primary.actuator.copied] == [(now - 4000, now - 3900)]      # cut to the hole
    assert primary.actuator.fetched == []                                                    # nothing re-recorded
    copied = [s for s in Manifest(primary.archive.root, "1").read() if s.source == "backup"]
    assert copied and copied[0].epoch == primary.epochs["1"]
    assert primary.our_coverage("1") == [(now - 7200, now - 600)]


def test_the_backups_manifest_decides_what_is_copied_not_its_summary():
    """The backup's heartbeat says it holds two hours — a summary, start and end. Its manifest says there is
    a hole in it too. The primary plans from the summary, copies from the manifest, and remembers what the
    backup did not have, so the next pass does not ask it again."""
    box, rec_ctl, primary, backup, _ = _site()
    now = box.wall()
    _footage(backup, "1-copy", ((now - 7200, now - 3960), (now - 3920, now - 600)))        # 40 s missing there too
    _footage(primary, "1", ((now - 7200, now - 4000), (now - 3900, now - 600)))
    backup.serve_archive(); backup.heartbeat_once()

    primary.backfill(budget=1, now=now, force=True)
    assert sorted((c[1], c[2]) for c in primary.actuator.copied) == [(now - 4000, now - 3960), (now - 3920, now - 3900)]
    assert primary.nowhere[("1", "backup:1-copy")] == [(now - 3960, now - 3920)]
    assert primary.backfill(budget=1, now=now, force=True) == []


def test_a_backup_fetches_from_nobody():
    """The backup exists to be copied from. A backup that backfilled — from the primary, or from the card —
    would fill ITS holes with footage the primary also has, and the two archives would stop being two
    independent records of what the camera saw: one hole, copied in both directions, is one hole twice."""
    box, rec_ctl, primary, backup, _ = _site(card=(0.0, 2e9, 1))           # …and a card the device CAN serve
    now = box.wall()
    _footage(primary, "1", ((now - 7200, now - 600),))
    _footage(backup, "1-copy", ((now - 7200, now - 4000), (now - 3900, now - 600)))
    primary.serve_archive(); primary.heartbeat_once()
    assert backup.backup_sources({"id": "1-copy", "cam": "1", "home": "copy"}) == []
    assert backup.backfill(budget=5, now=now, force=True) == []


def test_a_backup_volume_holds_its_own_recordings_and_nothing_else():
    """`home` is a preference everywhere else, and for a backup volume that is wrong both ways. The primary
    put onto the backup volume while its own server is down leaves one copy where two were paid for; the
    backup put onto the primary's disk is no copy at all. So a new camera's two recordings, created while
    only srv-b answers, are placed one on srv-b and one nowhere — unplaceable, said out loud — and when srv-a
    comes back the primary lands there. Without the rule the primary goes to srv-b "away from home"."""
    box, ctl, con, con_vars = _box()
    volumes.write(box.vars, {"name": "disks", "kind": "local", "server": "srv-a", "url": "/data/a", "quota_bytes": 10**12})
    volumes.write(box.vars, {"name": "copy", "kind": "backup", "server": "srv-b", "url": "/data/b", "quota_bytes": 10**12})
    backup = _recorder(box, "r-2", "srv-b", "copy")
    rec_ctl = SpecController(REC_SPEC, box.vars.as_writer("reccontroller", REC_SPEC.acl_controller()), box.objects, wall=box.wall)
    con_rec = SpecController(REC_SPEC, con_vars, box.objects, wall=box.wall)
    con_rec.create({"name": "2", "cam": "2", "home": "disks"})
    con_rec.create({"name": "2-copy", "cam": "2", "home": "copy"})
    rec_ctl.ensure_placed()
    assert rec_ctl.where("2-copy") == "r-2" and rec_ctl.placement("2") is None

    _recorder(box, "r-1", "srv-a", "disks")
    rec_ctl.ensure_placed()
    assert rec_ctl.where("2") == "r-1"

    con_rec.create({"name": "3-copy", "cam": "3", "home": "copy"})
    box.wall.advance(120); _resource(box, "srv-a")
    _recorder(box, "r-3", "srv-a", "disks")                           # srv-b silent: only disks answer
    rec_ctl.ensure_placed()
    assert rec_ctl.placement("3-copy") is None                        # the backup waits for ITS volume


def test_the_camera_stands_beside_the_backup_recording():
    """With two recordings, the camera's worker follows one of them (`near: {sub: rec, of: cam}`). Beside
    the primary, the fall of srv-a takes the worker too, and the backup loses its stream at exactly the
    moment it exists for. Beside the backup, the worker survives srv-a, the backup keeps recording, and the
    primary backfills its own move from the backup."""
    box, rec_ctl, primary, backup, w = _site()
    for r in (primary, backup):
        r.heartbeat_once()
    vms = VmsController(box.vars, box.objects, wall=box.wall)
    assert vms.holder_near("1") == ("r-2", "srv-b")


def test_an_offline_backup_stands_in_for_a_failure_and_never_for_a_decision():
    """`when: offline`: the backup records only while the primary SHOULD be written and is not. The primary
    running — standby. The primary gone quiet — a few seconds of grace, because every recording starts that
    way, then the backup records. The primary switched off, or an event recording whose `until` has passed —
    nobody's failure, and standing in for it would make a recording on events a recording always."""
    box, rec_ctl, primary, backup, _ = _site(when="offline")
    row = next(r for r in backup.rows if r["id"] == "1-copy")
    assert not backup.primary_needs_cover(row) and backup.desired() == []
    assert next(st for st in backup.status() if st["id"] == "1-copy")["phase"] == "standby"

    primary.actuator.stop_all(); primary.reconciler.actual.clear(); primary.heartbeat_once()   # the primary stops writing
    assert not backup.primary_needs_cover(row)                          # …not yet: it may be starting
    box.wall.advance(backup.START_GRACE + 1)
    primary.heartbeat_once()
    assert backup.primary_needs_cover(row) and [r["id"] for r in backup.desired()] == ["1-copy"]

    con = SpecController(REC_SPEC, box.vars.as_writer("console3", REC_SPEC.acl_console()), box.objects, wall=box.wall)
    con.update("1", {"enabled": False})                                  # the operator switched it off
    assert not backup.primary_needs_cover(row)
    con.update("1", {"enabled": True, "until": box.wall() - 1})          # an event that has ended
    assert not backup.primary_needs_cover(row)


def test_a_backup_volume_names_its_box():
    """A copy is only a copy if you know which box it is on: a backup volume without a server is refused,
    the way a local one is."""
    try:
        volumes.refuse({"name": "copy", "kind": "backup", "url": "/data/b", "quota_bytes": 1})
        raise AssertionError("a backup volume must name its box")
    except Refused as e:
        assert "name it" in str(e)
