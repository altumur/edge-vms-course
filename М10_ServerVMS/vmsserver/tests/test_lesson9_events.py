"""Lesson 9 — events: the database that is a cache. An event is an
observation, written by the worker holding a unit's epoch into that unit's
bucket on the resource (Lesson 3); the resource PROCESS keeps a database
over its own tree and serves it; the console holds none and asks. Three
subsystems' events land on one camera's timeline, fenced by their own
epochs; a restarted database rebuilds to the same answer from the files;
retention on the resource takes the rows with the file."""
import json
import os
import urllib.error
import urllib.request

from psimplatform.eventdatabase import EventDatabase, MergedIndex
from psimplatform.resource import resources_seen, serve as serve_resource
from psimplatform.spec import SpecController
from vms.archive import ArchiveResource
from vms.config import DET_SPEC, LIVE_SPEC, SPEC
from vms.console import serve
from vms.controller import VmsController
from vms.detector import DetWorker
from vms.resource import vms_resource, vms_routes
from vms.worker import FakeActuator, VmsWorker
from tests.conftest import Box


def call(base, method, path, body=None, headers=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def _resource_process(box):
    """What `python3 -m vms resource` does: the platform's Resource with the VMS registered,
    served over HTTP, heartbeating so the console can find it, its database rebuilt from the tree."""
    res = vms_resource(ArchiveResource(box.spool, box.archive, wall=box.wall), "srv-1", "", box.vars, box.objects, wall=box.wall)
    rsrv = serve_resource(res, "127.0.0.1", 0, extra=vms_routes(ArchiveResource(box.spool, box.archive, wall=box.wall)))
    res.url = f"http://127.0.0.1:{rsrv.server_address[1]}"
    res.heartbeat(); res.database.rebuild()
    return res, rsrv


def test_three_subsystems_events_reach_one_timeline_through_the_resource_process_and_the_console():
    box = Box()
    ctl = VmsController(box.vars.as_writer("vmscontroller", SPEC.acl_controller()), box.objects, wall=box.wall)
    con_vars = box.vars.as_writer("vmsconsole", SPEC.acl_console() + LIVE_SPEC.acl_console() + DET_SPEC.acl_console())
    con = VmsController(con_vars, box.objects, wall=box.wall)
    det_ctl = SpecController(DET_SPEC, box.vars.as_writer("detcontroller", DET_SPEC.acl_controller()), box.objects, wall=box.wall)
    w = VmsWorker("w-1", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall, server="srv-1", archive_root=box.archive)
    w.heartbeat_once(); con.create_camera({"name": "gate", "source": "driverpack://file/gate.mp4"}); ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()
    res, rsrv = _resource_process(box)
    assert list(resources_seen(box.objects)) == ["srv-1"]                                  # the console finds the resource by its heartbeat
    srv = serve(con, ArchiveResource(box.spool, box.archive), port=0, wall=box.wall,
                mounts={"det": SpecController(DET_SPEC, con_vars, box.objects, wall=box.wall)})   # no database here: the default MergedIndex asks srv-1
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        gpu = DetWorker("d-1", box.vars.as_writer("detworker", ["det/epoch/*", "det/slots/*"]), box.objects, capacity=8,
                        clock=box.clock, wall=box.wall, server="srv-1", archive_root=box.archive, env={"NOMAD_META_labels": "gpu"})
        gpu.heartbeat_once()
        call(base, "POST", "/det/units", {"name": "1-linecross", "cam": "1", "kind": "linecross"}, {"Idempotency-Key": "k1"})
        det_ctl.ensure_placed(); gpu.reconcile_once()
        for _ in range(3):
            box.wall.advance(2); gpu.reconcile_once()                                    # one event, on the third pass
        # the operator marks a moment, and the camera goes silent: three subsystems' events on one resource
        call(base, "POST", "/marks", {"cam": 1, "note": "check this"}, {"Idempotency-Key": "m1", "X-User": "murat"})
        w.actuator.dead.append(1); w.pump_once()
        assert res.database.tail()["added"] == 3                                          # the resource's tail; the console was not told
        st, out = call(base, "GET", "/events?cam=1"); ev = out["events"]
        assert st == 200 and out["state"] == "live"
        assert [(e["subsystem"], e["kind"], e["server"], e["fenced"]) for e in ev] == [
            ("det", "linecross", "srv-1", False), ("console", "mark", "srv-1", False), ("vms", "silent", "srv-1", False)]
        assert ev[0]["unit"] == "1-linecross" and ev[0]["pass"] == 3 and ev[0]["epoch"] == 1 and ev[1]["user"] == "murat"
        # the page shows all of it: the events under the timeline, the live feed beside the picture, the Mark button
        page = urllib.request.urlopen(base + "/").read().decode()
        assert 'id="events"' in page and 'id="livefeed"' in page and "/marks" in page and "/servers" in page
        # the same answer under the mount: /det/events fences by det's epochs too
        assert call(base, "GET", "/det/events?cam=1&subsystem=det")[1]["events"][0]["kind"] == "linecross"
        # an open bucket keeps growing: the next event is picked up by the next tail, not left for a rebuild
        for _ in range(3):
            box.wall.advance(2); gpu.reconcile_once()
        assert res.database.tail()["added"] == 1 and len(call(base, "GET", "/events?cam=1&subsystem=det")[1]["events"]) == 2
        # another detector instance takes the unit's epoch: the first one's events are fenced, nobody else's
        box.vars.put("det/epoch/1-linecross", {"epoch": "2"})
        assert [(e["subsystem"], e["fenced"]) for e in call(base, "GET", "/events?cam=1")[1]["events"]] == [
            ("det", True), ("console", False), ("vms", False), ("det", True)]
        # the resource process stops: the console says so by name, and answers with what it has — nothing
        rsrv.shutdown(); rsrv.server_close()
        st, out = call(base, "GET", "/events?cam=1")
        assert st == 200 and out["events"] == [] and out["state"] == "live; srv-1 unreachable"
    finally:
        srv.shutdown(); srv.server_close()


def test_the_database_is_a_cache_and_retention_takes_the_rows_with_the_file():
    """No store between the buckets and the answer: a fresh database over the same
    tree gives the same rows; the resource's retention pass removes a bucket file
    and its rows go with it — the database is told, the console just asks."""
    from vms.archive import event_log
    box = Box(); t = box.wall() - 3 * 86400
    event_log(box.archive, 7, 1).append(t + 10, "motion", zone="gate")                  # three days old: past a 1-day policy
    event_log(box.archive, 7, 1).append(box.wall() - 100, "motion")                      # fresh
    res = vms_resource(ArchiveResource(box.spool, box.archive, wall=box.wall), "srv-1", "http://srv-1", box.vars, box.objects, wall=box.wall)
    res.heartbeat()
    assert res.database.rebuild() == {"added": 2, "segments": 2, "mirrored": []} and res.database.state == "live"
    again = EventDatabase(box.archive, "srv-1", wall=box.wall); again.rebuild()
    assert again.query(0, 1e12)["events"] == res.database.query(0, 1e12)["events"]          # a cache proves it by being rebuilt
    m = MergedIndex(box.objects, fetch=lambda url, p: res.database.query(float(p["from"]), float(p["to"]), int(p["cam"]) if "cam" in p else None), wall=box.wall)
    assert [e["t"] for e in m.query(0, 1e12, cam=7)["events"]] == [t + 10, box.wall() - 100]
    box.vars.put("vms/retention/7", {"days": "1"})                                        # the VMS's policy for its unit, as a row the platform reads
    assert res.retain() == 1                                                              # the file went — and the rows with it
    assert [e["t"] for e in m.query(0, 1e12, cam=7)["events"]] == [box.wall() - 100]
    assert box.vars.list("vms/events") == [] and box.objects.list("vms/events") == []     # nothing about events in any store
