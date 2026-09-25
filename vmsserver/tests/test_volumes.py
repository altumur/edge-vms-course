"""Volumes an operator declares, and the recorders that take them.

A volume used to be deployment — a directory, an `ARCHIVE` in a unit file, one
instance per disk. A network archive cannot be that: it is created in the
console, and no unit file is edited for it. So it is a row (`rec/volumes/<name>`)
and a hold (`rec/holds/<name>`), and these tests are about the seam between the
two: a declaration is not a place until somebody is holding it."""
import io
import json
import os

from w2cplatform.contract import Heartbeat, Slot
from w2cplatform.spec import Refused, SpecController
from vms import volumes
from vms.config import REC_SPEC
from vms.console import rec_routes
from vms.recworker import RecWorker
from tests.conftest import Box


def _recorder(box, name, server, **kw):
    """A recorder with nothing pinned: `env={}` is the box with no VOLUME set."""
    from vms.archive import ArchiveResource
    return RecWorker(name, box.vars, box.objects, archive=ArchiveResource(box.spool, box.archive, wall=box.wall),
                     clock=box.clock, wall=box.wall, server=server, env={}, **kw)


def _resource(box, server, total=0):
    box.objects.put(f"platform/resources/{server}/heartbeat",
                    json.dumps({"server": server, "ts": box.wall(), "url": f"http://{server}", "units": {},
                                "space": {"total": total, "free": total}}).encode())


def test_a_network_volume_needs_a_quota_and_a_local_one_needs_a_server():
    """The two kinds differ in the one place it matters. A local volume is a
    disk, and a disk belongs to a machine. A network volume is an address any
    machine can reach — but there is no `statvfs` for a bucket, so the ceiling
    the watermark counts against has to be given, not read."""
    box = Box()
    for bad, why in (({"name": "vol-a", "kind": "local", "url": "/data/a", "quota_bytes": 1}, "server"),
                     ({"name": "s3", "kind": "network", "url": "s3://b/p"}, "quota_bytes"),
                     ({"name": "s3", "kind": "network", "url": "s3://b/p", "quota_bytes": 1, "server": "srv-a"}, "server"),
                     ({"name": "../etc", "kind": "local", "url": "/data/a", "server": "srv-a",
                       "quota_bytes": 1}, "not a path"),
                     ({"name": "vol-a", "kind": "local", "url": "/data/a", "server": "srv-a"}, "quota_bytes")):
        try:
            volumes.write(box.vars, bad)
            raise AssertionError(f"accepted {bad}")
        except Refused as e:
            assert why in str(e), (bad, e)

    volumes.write(box.vars, {"name": "vol-a", "kind": "local", "url": "/data/a", "server": "srv-a", "quota_bytes": 10 ** 11})
    volumes.write(box.vars, {"name": "s3-main", "kind": "network", "url": "s3://vms/site-7", "quota_bytes": 10 ** 12,
                             "access_secret": "AKIA-and-the-rest"})
    assert [v.name for v in volumes.declared(box.vars)] == ["s3-main", "vol-a"]

    # the secret is stored and never read back out by the console's view
    assert box.vars.get(volumes.key("s3-main"))[0]["access_secret"] == "AKIA-and-the-rest"
    shown = volumes.served(box.vars, REC_SPEC.sub, box.wall())
    assert all("access_secret" not in row for row in shown["volumes"])


def test_who_may_serve_what():
    """A local volume has exactly one machine that can write to it; a network
    volume has any. That asymmetry is the whole arithmetic of spares: one spare
    per box absorbs one network volume per box."""
    box = Box()
    volumes.write(box.vars, {"name": "vol-a", "kind": "local", "url": "/data/a", "server": "srv-a", "quota_bytes": 10 ** 11})
    volumes.write(box.vars, {"name": "vol-b", "kind": "local", "url": "/data/b", "server": "srv-b", "quota_bytes": 10 ** 11})
    volumes.write(box.vars, {"name": "s3-main", "kind": "network", "url": "s3://vms/x", "quota_bytes": 10 ** 12})
    volumes.write(box.vars, {"name": "s3-old", "kind": "network", "url": "s3://vms/y", "quota_bytes": 10 ** 12,
                             "enabled": "false"})
    vols = volumes.declared(box.vars)

    assert volumes.servable(vols, "srv-a") == ["vol-a", "s3-main"]     # its own disk first, then what anybody may take
    assert volumes.servable(vols, "srv-b") == ["vol-b", "s3-main"]
    assert volumes.servable(vols, "srv-c") == ["s3-main"]              # a box with no disk of its own is still a candidate
    assert "s3-old" not in volumes.servable(vols, "srv-a")             # disabled is nobody's


def test_nothing_declared_is_the_box_as_it_always_was():
    """The migration, stated as a test: a recorder that is told nothing, on a
    cluster where nobody declared anything, is on one place named after its
    server — exactly what it was before volumes were rows."""
    box = Box()
    r = _recorder(box, "r-1", "srv-a")
    assert r.volume_pass() == "srv-a" and r.hold is None and r.capacity == r.full_capacity


def test_a_declared_volume_is_taken_by_one_recorder_and_the_other_is_a_spare():
    """The point of the whole arrangement. Two recorders, one declared archive:
    one takes it and IS that archive's recorder; the other carries nothing and
    says so — a spare is a running process with no place, not a failure."""
    box = Box()
    volumes.write(box.vars, {"name": "s3-main", "kind": "network", "url": "s3://vms/x", "quota_bytes": 10 ** 12})
    a, b = _recorder(box, "r-1", "srv-a"), _recorder(box, "r-2", "srv-a")

    assert a.volume_pass() == "s3-main" and a.hold == "s3-main"
    assert b.volume_pass() == "" and b.hold is None                    # taken: this one is spare
    assert b.capacity == 0 and a.capacity == a.full_capacity

    a.heartbeat_once(); b.heartbeat_once()
    ctl = SpecController(REC_SPEC, box.vars, box.objects, wall=box.wall)
    assert ctl.place_of("r-1") == "s3-main"
    assert ctl.place_of("r-2") == ""                                   # NOT "srv-a": a spare is not the box's place
    assert ctl.idle_by_policy(["r-1", "r-2"]) == []                    # two places, one of them empty: neither idles

    # and a recording homed there is placed on the holder, because that is where the place is
    _resource(box, "srv-a")
    ctl.create({"name": "7-cloud", "cam": "7", "home": "s3-main"})
    ctl.ensure_placed()
    assert ctl.where("7-cloud") == "r-1" and "at home on s3-main" in ctl.placement("7-cloud").reason


def test_a_spare_picks_up_a_volume_whose_recorder_went_silent():
    """Why the hold is a lease and not an assignment. The process that held
    `s3-main` stops answering; the hold lapses; the spare takes it and the
    archive is served again — with nobody deciding anything."""
    box = Box()
    volumes.write(box.vars, {"name": "s3-main", "kind": "network", "url": "s3://vms/x", "quota_bytes": 10 ** 12})
    a, b = _recorder(box, "r-1", "srv-a"), _recorder(box, "r-2", "srv-b")
    assert a.volume_pass() == "s3-main" and b.volume_pass() == ""

    box.wall.advance(a.slot_ttl + 1)                                   # r-1 stops renewing: crashed, or the box went away
    assert b.volume_pass() == "s3-main" and b.hold == "s3-main"

    # and the old holder learns it on its next pass: it is not fenced, it is a spare now
    assert a.volume_pass() == "" and a.hold is None and a.recording_allowed


def test_a_withdrawn_volume_stops_the_recordings_and_leaves_the_process_running():
    """The administrator deletes the archive. Whoever was writing into it has to
    stop — and keep its slot: this is a reassignment, not a zombie. The process
    is free to take another volume on the same pass it lost this one."""
    box = Box()
    for n in ("s3-cold", "s3-main"):
        volumes.write(box.vars, {"name": n, "kind": "network", "url": f"s3://vms/{n}", "quota_bytes": 10 ** 12})
    r = _recorder(box, "r-1", "srv-a")
    assert r.volume_pass() == "s3-cold"
    r.reconciler.actual["7-cold"] = {"id": "7-cold"}                   # pretend it is recording one into it

    volumes.delete(box.vars, "s3-cold")
    assert r.volume_pass() == "s3-main"                                # stopped there, took what was free
    assert r.reconciler.actual == {}                                   # and carries nothing of the old archive
    assert r.recording_allowed and r.name == "r-1"                     # still itself: not fenced, still its slot
    assert volumes.holders(box.vars, REC_SPEC.sub)["s3-cold"].released  # let go on purpose, so the row says so

    # and withdrawing the last one is the migration branch, not a dead process: the box's own archive
    volumes.delete(box.vars, "s3-main")
    assert r.volume_pass() == "srv-a" and r.capacity == r.full_capacity


def test_the_console_says_which_archives_nobody_is_writing_into():
    """The number an operator watches. A volume nobody took is not an error and
    not a silence: `home` is a preference, so the footage would go somewhere else
    without a word — the console has to say the word instead."""
    box = Box()
    volumes.write(box.vars, {"name": "s3-main", "kind": "network", "url": "s3://vms/x", "quota_bytes": 10 ** 12})
    volumes.write(box.vars, {"name": "s3-cold", "kind": "network", "url": "s3://vms/y", "quota_bytes": 10 ** 12})
    r = _recorder(box, "r-1", "srv-a")
    assert r.volume_pass() == "s3-cold"                      # one recorder, two archives: it takes the first by name

    view = volumes.served(box.vars, REC_SPEC.sub, box.wall())
    assert view["wanted"] == 2 and view["serving"] == 1       # the number an operator watches: two declared, one served
    by = {v["name"]: v for v in view["volumes"]}
    assert by["s3-main"]["served_by"] is None and "no recorder has taken it" in by["s3-main"]["why"]
    assert by["s3-cold"]["served_by"] == r.instance and by["s3-cold"]["why"] is None

    # the second one is served the moment a process exists for it — no deploy, no assignment
    r2 = _recorder(box, "r-2", "srv-a")
    assert r2.volume_pass() == "s3-main"
    assert volumes.served(box.vars, REC_SPEC.sub, box.wall())["serving"] == 2


class _Body:
    """The two things the extra route reads off a handler: a length and a stream."""
    def __init__(self, payload: bytes):
        self.headers, self.rfile = {"Content-Length": str(len(payload))}, io.BytesIO(payload)


def test_the_console_declares_a_volume_and_says_who_serves_it():
    """The operator's side, over the route the page calls: declare an archive,
    see it unserved, see a recorder take it, and delete the declaration without
    deleting a byte of footage."""
    box = Box()
    rec = SpecController(REC_SPEC, box.vars.as_writer("console", REC_SPEC.acl_console()), box.objects, wall=box.wall)
    route = rec_routes(rec)

    assert "rec/volumes/*" in REC_SPEC.acl_console()                   # `tables: [volumes]`, and nothing else granted

    status, body = route(_Body(json.dumps({"name": "s3-main", "kind": "network", "url": "s3://vms/x",
                                           "quota_bytes": 10 ** 12, "access_secret": "AKIA"}).encode()),
                         "POST", "/volumes", {})
    assert status == 201 and "access_secret" not in body["volume"]     # the reply never carries it back

    status, view = route(None, "GET", "/volumes", {})
    assert status == 200 and view["wanted"] == 1 and view["serving"] == 0 and view["spare"] == 0

    r = _recorder(box, "r-1", "srv-a"); r.volume_pass(); r.heartbeat_once()
    _, view = route(None, "GET", "/volumes", {})
    assert view["serving"] == 1 and view["spare"] == 0

    s = _recorder(box, "r-2", "srv-a"); s.volume_pass(); s.heartbeat_once()
    _, view = route(None, "GET", "/volumes", {})
    assert view["spare"] == 1 and view["spares"] == ["r-2"]            # one process ready for the next archive declared

    status, body = route(None, "DELETE", "/volumes/s3-main", {})
    assert status == 200 and "footage already written is untouched" in body["detail"]
    assert route(None, "DELETE", "/volumes/s3-main", {})[0] == 404

    # a bad declaration is a 400 to the person who typed it, not a 500 from the store
    status, body = route(_Body(json.dumps({"name": "s3-x", "kind": "network", "url": "s3://vms/y"}).encode()),
                         "POST", "/volumes", {})
    assert status == 400 and "quota_bytes" in body["detail"]


def test_the_console_offers_the_disk_this_box_already_records_into():
    """The first volume an operator ever declares should not be typed out. The
    box already says where it records (the recorder's heartbeat) and how big
    that filesystem is (the resource's), so the console offers exactly that,
    with the partition's own size as the capacity — a number the operator can
    then make smaller to fit a second volume on the same disk beside it.

    It is an OFFER: nothing here writes configuration on a process's behalf."""
    box = Box()
    rec = SpecController(REC_SPEC, box.vars.as_writer("console", REC_SPEC.acl_console()), box.objects, wall=box.wall)
    route = rec_routes(rec)
    _resource(box, "srv-a", total=4 * 10 ** 12)
    r = _recorder(box, "r-1", "srv-a"); r.volume_pass(); r.heartbeat_once()

    _, view = route(None, "GET", "/volumes", {})
    assert view["wanted"] == 0 and view["suggested"] == [
        {"name": "srv-a", "kind": "local", "url": box.archive, "server": "srv-a", "quota_bytes": 4 * 10 ** 12,
         "why": "this box records here and the disk is not declared as a volume"}]

    offer = {k: v for k, v in view["suggested"][0].items() if k != "why"}   # `why` is for the operator, not the row
    assert route(_Body(json.dumps(offer).encode()), "POST", "/volumes", {})[0] == 201
    _, view = route(None, "GET", "/volumes", {})
    assert view["wanted"] == 1 and view["suggested"] == []          # declared: nothing left to offer

    # and now the disk can be split: half of it to a second volume beside the first
    status, _ = route(_Body(json.dumps({"name": "cold", "kind": "local", "url": box.archive + "/cold",
                                        "server": "srv-a", "quota_bytes": 2 * 10 ** 12}).encode()),
                      "POST", "/volumes", {})
    assert status == 201
    assert [v["name"] for v in route(None, "GET", "/volumes", {})[1]["volumes"]] == ["cold", "srv-a"]


def test_a_quota_is_a_ceiling_and_not_a_reservation():
    """What lets one partition hold two volumes. Each has a number of its own,
    so neither reads the whole disk's free space and believes it is its own —
    and neither is promised anything either: a volume gets the smaller of what
    its quota leaves and what the disk actually has."""
    from w2cplatform.resource import Resource

    box = Box()
    a, b = os.path.join(box.root, "vol-a"), os.path.join(box.root, "vol-b")
    res = Resource(None, "srv-a", "http://srv-a", box.vars, box.objects, wall=box.wall,
                   volumes={"vol-a": a, "vol-b": b}, quotas={"vol-a": 1000, "vol-b": 4000})
    os.makedirs(os.path.join(a, "rec"), exist_ok=True)
    open(os.path.join(a, "rec", "x"), "wb").write(b"." * 400)

    assert res.space("vol-a") == {"total": 1000, "free": 600, "used": 400, "full": 0.4}
    assert res.space("vol-b")["free"] == 4000                      # same partition, and not the same number
    assert res.space()["total"] == 5000                            # the box is the sum of its volumes, once each

    # the ceiling never promises more than the disk has: a probe that says the filesystem is nearly full
    # wins over a generous quota, because a quota is not storage
    res.space_probe = lambda path: (10 ** 6, 250)
    assert res.space("vol-b")["free"] == 250


def test_the_resource_sweeps_the_volumes_this_box_is_responsible_for():
    """Which trees this server's resource repairs, retains and watches.

    A LOCAL volume declared for it is its own by declaration — footage ages
    whether anybody is recording into it or not. A NETWORK volume is its own
    only while a recorder here holds it: exactly one box may sweep a bucket,
    and the hold is what says which."""
    from vms.archive import ArchiveResource
    from vms.resource import archives_of, refresh_volumes, vms_resource

    box = Box()
    a, s3 = os.path.join(box.root, "vol-a"), os.path.join(box.root, "s3")
    volumes.write(box.vars, {"name": "vol-a", "kind": "local", "url": a, "server": "srv-a", "quota_bytes": 10 ** 9})
    volumes.write(box.vars, {"name": "vol-b", "kind": "local", "url": os.path.join(box.root, "vol-b"),
                             "server": "srv-b", "quota_bytes": 10 ** 9})
    volumes.write(box.vars, {"name": "s3-main", "kind": "network", "url": s3, "quota_bytes": 10 ** 12})

    res = vms_resource(ArchiveResource(box.spool, box.archive, wall=box.wall), "srv-a", "http://srv-a",
                       box.vars, box.objects, wall=box.wall)
    refresh_volumes(res, box.vars, box.objects, REC_SPEC.sub, box.wall())
    assert set(res.volumes) == {"vol-a"}                           # srv-b's disk is not ours; the bucket is nobody's yet
    assert res.quotas["vol-a"] == 10 ** 9

    r1, r2 = _recorder(box, "r-1", "srv-a"), _recorder(box, "r-2", "srv-a")
    r1.volume_pass(); r1.heartbeat_once()                          # takes vol-a: its own disk first
    r2.volume_pass(); r2.heartbeat_once()                          # takes the bucket
    assert (r1.volume, r2.volume) == ("vol-a", "s3-main")

    refresh_volumes(res, box.vars, box.objects, REC_SPEC.sub, box.wall())
    assert set(res.volumes) == {"vol-a", "s3-main"}                # ours while this box holds it
    assert set(res.hooks["rec"].volumes) == {"vol-a", "s3-main"}   # …and the media policy walks both trees

    # the same cluster from srv-b's side: the bucket is not its business, and its own disk is
    paths, _ = archives_of(box.vars, box.objects, REC_SPEC.sub, "srv-b", box.wall())
    assert set(paths) == {"vol-b"}


def test_the_console_writes_out_the_command_and_does_not_run_it():
    """The operator's knob, and the shape it is allowed to have.

    A button that started a recorder would need a token to the orchestrator in
    the console — the one thing the platform keeps out of itself. What the
    console can do instead is stop making the operator translate: it knows how
    many processes are missing and which orchestrator started IT, so it writes
    the line out in that orchestrator's words. Running it stays a person's act."""
    box = Box()
    rec = SpecController(REC_SPEC, box.vars.as_writer("console", REC_SPEC.acl_console()), box.objects, wall=box.wall)
    route = rec_routes(rec)
    for n in ("s3-a", "s3-b"):
        volumes.write(box.vars, {"name": n, "kind": "network", "url": f"s3://vms/{n}", "quota_bytes": 10 ** 12})

    r = _recorder(box, "r-1", "srv-a"); r.volume_pass(); r.heartbeat_once()
    _, view = route(None, "GET", "/volumes", {})
    assert view["needed"] == 1 and view["how"] == "systemctl start recworker@r-2"   # the next slot nobody holds
    # `live` rides along so that whatever acts on `needed` — the timer on a box, the scaler on a cluster —
    # gets both numbers from one reply and never asks the orchestrator for a second opinion
    assert view["live"] == 1

    # a spare covers the gap, so nothing is needed and nothing is suggested
    s = _recorder(box, "r-2", "srv-a"); s.volume_pass(); s.heartbeat_once()
    _, view = route(None, "GET", "/volumes", {})
    assert view["needed"] == 0 and view["how"] is None

    # on a cluster the same number comes out in the scheduler's words
    volumes.write(box.vars, {"name": "s3-c", "kind": "network", "url": "s3://vms/c", "quota_bytes": 10 ** 12})
    os.environ["NOMAD_ALLOC_ID"] = "alloc-1"
    try:
        _, view = route(None, "GET", "/volumes", {})
        assert view["needed"] == 1 and view["how"] == "nomad job scale recworker 3"
    finally:
        del os.environ["NOMAD_ALLOC_ID"]


def test_the_numbers_a_scaling_policy_reads():
    """`spare: 0` while something is declared and unserved is the one state that
    needs a person — so it has to be a number a machine can read too, or the
    person is the only mechanism there is.

    And the trap this test exists for: a spare reports zero capacity, which the
    load gauge would read as FULLY LOADED. Left in, one spare would demand the
    next one for ever."""
    from w2cplatform.console import SpecConsole
    from vms.console import rec_metrics

    box = Box()
    rec = SpecController(REC_SPEC, box.vars.as_writer("console", REC_SPEC.acl_console()), box.objects, wall=box.wall)
    for n in ("s3-main", "s3-cold"):
        volumes.write(box.vars, {"name": n, "kind": "network", "url": f"s3://vms/{n}", "quota_bytes": 10 ** 12})
    r, spare = _recorder(box, "r-1", "srv-a"), _recorder(box, "r-2", "srv-a")
    r.volume_pass(); r.heartbeat_once()
    spare.volume_pass(); spare.heartbeat_once()
    assert (r.volume, spare.volume) == ("s3-cold", "s3-main")

    con = SpecConsole(rec, wall=box.wall, metrics_extra=rec_metrics(rec))
    text = con.metrics_text()
    assert "rec_volumes_declared 2" in text and "rec_volumes_unserved 0" in text

    # both recorders hold an archive, so neither is spare and both are in the load gauge
    assert "rec_spare_workers 0" in text
    assert 'rec_worker_load{worker="r-1"}' in text and 'rec_worker_load{worker="r-2"}' in text

    volumes.write(box.vars, {"name": "s3-third", "kind": "network", "url": "s3://vms/z", "quota_bytes": 10 ** 12})
    text = con.metrics_text()
    assert "rec_volumes_declared 3" in text and "rec_volumes_unserved 1" in text   # declared, and nobody free
    assert "rec_recorders_needed 1" in text                        # …and no spare to take it: a process is missing

    # A shortage a process cannot fix, which is the runaway this gauge exists to stop. `srv-b-disk` is a
    # disk on a box that runs no recorder: starting one HERE does not make it servable, and a policy
    # reading `unserved` would ask for a worker, and another, and another, to its ceiling — every one of
    # them a spare that cannot help. One spare is proof the shortage is not a shortage of processes.
    volumes.delete(box.vars, "s3-third")                           # everything a recorder here could take is taken
    volumes.write(box.vars, {"name": "srv-b-disk", "kind": "local", "url": "/data/b", "server": "srv-b",
                             "quota_bytes": 10 ** 9})
    idle = _recorder(box, "r-3", "srv-a")
    assert idle.volume_pass() == ""                                # nothing here for it: a spare
    idle.heartbeat_once()
    text = con.metrics_text()
    assert "rec_volumes_unserved 1" in text and "rec_spare_workers 1" in text
    assert "rec_recorders_needed 0" in text

    # …and a spare is kept OUT of the load gauge, where zero capacity would read as fully loaded
    assert 'rec_worker_load{worker="r-3"}' not in text
    assert 'rec_worker_load{worker="r-1"}' in text


def test_the_recorder_writes_into_the_volume_it_took():
    """Taking a place means writing into its tree. The archive a recorder
    promotes into follows the hold — otherwise a spare that took the network
    archive would go on filling the local disk, and the declaration would be a
    label on nothing."""
    from datetime import datetime, timezone
    from vms.archive import segment_path

    box = Box()
    a, b = os.path.join(box.root, "vol-a"), os.path.join(box.root, "vol-b")
    for n, url in (("vol-a", a), ("vol-b", b)):
        volumes.write(box.vars, {"name": n, "kind": "local", "url": url, "server": "srv-a", "quota_bytes": 10 ** 9})

    r = _recorder(box, "r-1", "srv-a")
    assert r.volume_pass() == "vol-a"
    assert r.archive.root == a and r.archive_root == a             # …and the heartbeat says so too

    # a segment closed in the spool while we held vol-a is promoted into vol-a, even though the volume is
    # withdrawn in the same pass: we promote first, while we may still write there
    t = box.wall()
    spool_seg = segment_path(r.archive.spool, "7", 1, datetime.fromtimestamp(t - 600, timezone.utc))
    os.makedirs(os.path.dirname(spool_seg), exist_ok=True)
    open(spool_seg, "wb").write(b"footage")
    os.utime(spool_seg, (t - 600, t - 600))                        # closed ten minutes ago, by the box's clock

    volumes.delete(box.vars, "vol-a")
    assert r.volume_pass() == "vol-b" and r.archive.root == b
    assert os.path.isfile(os.path.join(a, "rec", "7", "e1", os.path.basename(spool_seg)))   # in vol-a's tree
    assert not os.path.exists(os.path.join(b, "rec", "7"))                                   # and not in vol-b's


def test_a_volume_held_and_unwritable_is_not_served():
    """The worst failure this subsystem has, because every number says it is fine.

    A hold is fresh, so the console counted the archive served; the recorder sat
    on it and wrote nothing; the screen was green. Only the process that opened
    the volume knows it did not open, so it says so — and what reads that must
    not count the volume as served, must not call the recorder a place to put
    new recordings, and must not let go of the archive when there is nowhere
    else to go: a box that stops recording because of diagnostics is worse."""
    box = Box()
    good, bad = os.path.join(box.root, "good"), os.path.join(box.root, "nope", "deeper")
    open(os.path.join(box.root, "nope"), "wb").write(b"")          # a FILE where a directory is declared
    volumes.write(box.vars, {"name": "a-broken", "kind": "local", "url": bad, "server": "srv-a", "quota_bytes": 10 ** 9})
    volumes.write(box.vars, {"name": "b-good", "kind": "local", "url": good, "server": "srv-a", "quota_bytes": 10 ** 9})

    # `a-broken` sorts first, so it is tried first — and handed back, because there IS somewhere to go
    r = _recorder(box, "r-1", "srv-a")
    assert r.volume_pass() == "b-good" and r.volume_error == ""
    assert r.archive.root == good
    assert volumes.holders(box.vars, REC_SPEC.sub)["a-broken"].released    # let go at once, not sat on

    # the second recorder has nowhere else: it keeps the broken archive, reports why, and offers no room
    s = _recorder(box, "r-2", "srv-a")
    assert s.volume_pass() == "a-broken" and s.volume_error
    assert s.capacity == 0                                         # not a place to put a recording
    s.heartbeat_once(); r.heartbeat_once()

    view = volumes.served(box.vars, REC_SPEC.sub, box.wall(), objects=box.objects)
    by = {v["name"]: v for v in view["volumes"]}
    assert view["wanted"] == 2 and view["serving"] == 1            # NOT two: a held archive nobody can write to
    assert by["a-broken"]["served_by"] is None
    assert "cannot write there" in by["a-broken"]["why"] and s.instance in by["a-broken"]["why"]
    assert by["b-good"]["served_by"] == r.instance

    # and it heals by itself the moment the archive is there: nothing to press, nothing to restart
    os.remove(os.path.join(box.root, "nope"))
    assert s.volume_pass() == "a-broken" and s.volume_error == "" and s.capacity == s.full_capacity
    s.heartbeat_once()
    assert volumes.served(box.vars, REC_SPEC.sub, box.wall(), objects=box.objects)["serving"] == 2


def test_the_key_never_goes_into_the_address():
    """`volumes.py` says it handles no credentials beyond the suffix rule — and
    the one thing it can still do is refuse the obvious way to lose them. A url
    is printed on the page, published in the recorder's heartbeat as `archive`
    and written into the row; a key inside it is the same secret in three public
    places, and the `*_secret` rule cannot help, because the field it guards is
    not the one carrying it."""
    box = Box()
    try:
        volumes.write(box.vars, {"name": "s3", "kind": "network", "quota_bytes": 1,
                                 "url": "s3://AKIAEXAMPLE:wJalrXUtnFEMI@s3.example.com/vms"})
        raise AssertionError("a url with credentials in it was accepted")
    except Refused as e:
        assert "never the key to it" in str(e)

    for ok in ("/data/archive/cold", "file:///data/archive/cold", "s3://s3.example.com/vms/site-7"):
        volumes.refuse({"name": "v", "kind": "network", "url": ok, "quota_bytes": 1})


def test_evacuation_reads_the_emptiest_volume_and_not_the_sum():
    """A sum is the one number that cannot answer "can this box take a gigabyte".

    The destination reports `space` across all its volumes, and the segments
    land on ONE of them — whichever its recorder writes to. A box with one full
    disk and one empty one reports half free, the "it is tight there" check
    stops working, and back come the two servers trading gigabytes."""
    from vms.space import room_on

    half_full = {"space": {"total": 200, "free": 100},                 # the sum says: plenty
                 "volumes": {"vol-a": {"total": 100, "free": 0},       # …and every byte of it is here
                             "vol-b": {"total": 100, "free": 100}}}
    assert room_on(half_full) == 100                                   # the emptiest, not the sum

    tight = {"space": {"total": 200, "free": 20},
             "volumes": {"vol-a": {"total": 100, "free": 10}, "vol-b": {"total": 100, "free": 10}}}
    assert room_on(tight) == 10                                        # …and here the sum would have lied upward

    # a resource that names no volumes was written before there were any: sum and volume are one number
    assert room_on({"space": {"total": 100, "free": 40}}) == 40
    assert room_on({}) == 0


def test_a_recorder_that_holds_an_archive_is_not_a_spare():
    """Taking a volume and opening it are two moments, and on a bucket whose
    previous writer is still letting go the gap is most of a minute. A process
    counted spare in that gap is a spare the operator is promised and the
    scaling policy will not ask to replace — and both numbers heal themselves,
    which is exactly when nobody notices."""
    from vms.console import spare_workers

    box = Box()
    rec = SpecController(REC_SPEC, box.vars.as_writer("console", REC_SPEC.acl_console()), box.objects, wall=box.wall)
    volumes.write(box.vars, {"name": "s3-main", "kind": "network", "url": "s3://vms/x", "quota_bytes": 10 ** 12})

    r, s = _recorder(box, "r-1", "srv-a"), _recorder(box, "r-2", "srv-a")
    r.volume_pass(); s.volume_pass()
    r.heartbeat_once(); s.heartbeat_once()
    assert spare_workers(rec) == ["r-2"]                               # r-1 holds it, r-2 has nothing

    # now the heartbeat of the holder says it is nowhere — the window between taking and opening
    r.volume = ""
    r.heartbeat_once()
    assert rec.place_of("r-1") == ""                                   # the heartbeat alone would say "spare"
    assert spare_workers(rec) == ["r-2"]                               # the hold says otherwise, and it wins


# -- every segment knows its volume -------------------------------------------------------------------------
#
# A segment's path in the spool is `rec/<unit>/e<epoch>/<start>` — it does not name the volume it was recorded
# for. That knowledge lived in the process (`self.hold`), and a process that dies takes it along. So the epoch
# directory says it: `rec/<unit>/e<epoch>/.volume`. Not the spool — every recorder on a box shares one — but
# the epoch, which exactly one recorder holds, for exactly one volume. Nothing is promoted anywhere else.

def _closed_segment(box, r, cam="7", age=600, epoch=1):
    """A segment the pipeline closed `age` seconds ago and nobody promoted — what a spool holds after an
    outage, or after a process died with its last segment still in it. Its epoch directory is marked for
    the volume `r` holds, the way starting the pipeline marks it."""
    from datetime import datetime, timezone
    from vms.archive import segment_path
    r.mark_epoch(cam, epoch)
    t = box.wall()
    p = segment_path(r.archive.spool, cam, epoch, datetime.fromtimestamp(t - age, timezone.utc))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(b"footage")
    os.utime(p, (t - age, t - age))
    return p


def test_a_restart_does_not_pour_the_spool_into_the_wrong_archive():
    """The constructor promoted "what the last instance closed but did not promote" — into `self.archive`,
    which in a constructor is the LOCAL default, because no volume has been looked at yet. So every restart
    of a recorder writing to a network archive filed its last segment, the last ten minutes, in the local
    one; during an outage, the whole queue. Healthy network or not: the constructor runs before anything
    asks where the recorder writes. And the first pass of the loop promoted before its first `volume_pass`,
    which is the same door opened a second time."""
    box = Box()
    cloud = os.path.join(box.root, "cloud")
    volumes.write(box.vars, {"name": "cloud", "kind": "local", "url": cloud, "server": "srv-a", "quota_bytes": 10 ** 9})
    r = _recorder(box, "r-1", "srv-a")
    assert r.volume_pass() == "cloud"
    seg = _closed_segment(box, r)                                  # …and then the process died with it in the spool

    box.wall.advance(60); box.clock.advance(60)                    # its hold and its slot lapse
    r2 = _recorder(box, "r-1", "srv-a")                            # a new process, which has looked at no volume
    local = os.path.join(box.archive, "rec", "7")
    assert not os.path.exists(local), "the constructor promoted a segment recorded for `cloud` into the local archive"
    assert os.path.isfile(seg)

    r2.pump_once()                                                 # the loop reaches promotion before volume_pass
    assert not os.path.exists(local), "the first pass promoted it into the local archive before looking at volumes"

    assert r2.volume_pass() == "cloud"                             # the volume the spool was recorded for
    r2.promote_closed()
    assert os.path.isfile(os.path.join(cloud, "rec", "7", "e1", os.path.basename(seg)))
    assert not os.path.exists(local)


def test_a_recorder_that_moves_on_leaves_the_old_volumes_footage_where_it_is():
    """Mid-run, the same mistake by a different road. The recorder holds `vol-z`, the archive stops
    answering with a segment still in the spool, and `vol-z` is taken from it. Looking for somewhere to
    write, it finds `vol-a` — and the next promote moved `vol-z`'s footage into `vol-a`, with nothing to
    say it happened, because nothing failed.

    Forbidding the move would have cost a recorder: a process pinned by a queue it cannot send is a spare
    doing nothing. So the recorder moves, and the footage does not: each segment says which volume it
    belongs to, and one for `vol-z` waits for whoever holds `vol-z` next. Right even if nobody ever does —
    footage in the wrong archive is found by nobody, footage waiting in a spool is found by the next
    person who reads the heartbeat."""
    box = Box()
    z = os.path.join(box.root, "vol-z")
    volumes.write(box.vars, {"name": "vol-z", "kind": "local", "url": z, "server": "srv-a", "quota_bytes": 10 ** 9})
    r = _recorder(box, "r-1", "srv-a")
    assert r.volume_pass() == "vol-z"
    seg = _closed_segment(box, r)

    def away(*a, **k):
        raise OSError("network is unreachable")
    r.archive.promote = away                                       # vol-z stops answering: the segment cannot go
    a = os.path.join(box.root, "vol-a")
    volumes.write(box.vars, {"name": "vol-a", "kind": "local", "url": a, "server": "srv-a", "quota_bytes": 10 ** 9})
    volumes.delete(box.vars, "vol-z")                              # …and vol-z is taken away with its queue unsent

    assert r.volume_pass() == "vol-a"                              # the recorder is free to move on…
    r.pump_once()
    assert not os.path.exists(os.path.join(a, "rec", "7")), "vol-z's footage was promoted into vol-a"
    assert os.path.isfile(seg)                                     # …and the footage is not: it waits for vol-z
    hb = r.heartbeat_extra()
    assert hb["spool_for"] == {"vol-z": 1} and hb["spool"] == 1   # whose footage is waiting, and how much


def test_a_drained_spool_lets_the_recorder_go_anywhere():
    """The pin is a queue's, not a volume's. Once what the spool held has gone where it belonged, the
    recorder is as free as it ever was — the rule must not outlive the reason for it."""
    box = Box()
    z = os.path.join(box.root, "vol-z")
    volumes.write(box.vars, {"name": "vol-z", "kind": "local", "url": z, "server": "srv-a", "quota_bytes": 10 ** 9})
    r = _recorder(box, "r-1", "srv-a")
    assert r.volume_pass() == "vol-z"
    _closed_segment(box, r)
    r.promote_closed()                                             # it went across
    assert r.heartbeat_extra()["spool_for"] == {}

    a = os.path.join(box.root, "vol-a")
    volumes.write(box.vars, {"name": "vol-a", "kind": "local", "url": a, "server": "srv-a", "quota_bytes": 10 ** 9})
    volumes.delete(box.vars, "vol-z")
    assert r.volume_pass() == "vol-a"


def test_recorders_sharing_a_spool_each_promote_only_their_own():
    """The spool is the BOX's, not a process's: the unit file is a template, and every recorder on a box
    mounts the same `/data/spool` — three disks, three recorders, one spool. So `closed_in_spool` lists
    every recorder's segments, and whichever recorder promoted first carried its neighbours' footage into
    its own archive. With GStreamer running, `archivesink` promotes on its own as each segment closes and
    this path only sweeps up stragglers — which is exactly the queue an outage leaves behind: when the link
    came back, a box with three disks shuffled its backlog across the wrong ones.

    This was true before any of the outage work; it is the same fix. Each segment knows its volume, and a
    recorder promotes only what is its own."""
    box = Box()
    a, b = os.path.join(box.root, "vol-a"), os.path.join(box.root, "vol-b")
    for n, url in (("vol-a", a), ("vol-b", b)):
        volumes.write(box.vars, {"name": n, "kind": "local", "url": url, "server": "srv-a", "quota_bytes": 10 ** 9})
    r1, r2 = _recorder(box, "r-1", "srv-a"), _recorder(box, "r-2", "srv-a")      # one box, one spool
    assert (r1.volume_pass(), r2.volume_pass()) == ("vol-a", "vol-b")
    one = _closed_segment(box, r1, cam="1")
    two = _closed_segment(box, r2, cam="2")

    r1.promote_closed()
    assert os.path.isfile(os.path.join(a, "rec", "1", "e1", os.path.basename(one)))
    assert os.path.isfile(two) and not os.path.exists(os.path.join(a, "rec", "2")), \
        "r-1 promoted its neighbour's footage into its own archive"
    r2.promote_closed()
    assert os.path.isfile(os.path.join(b, "rec", "2", "e1", os.path.basename(two)))


# -- away, or wrong: a transient failure and a permanent one are answered differently -----------------------
#
# The rule from section H — "an archive that will not open is handed back, if there is somewhere else to go" —
# is right for a wrong key or a path that is a file, and wrong for a link that dropped for a minute: the
# recorder gives the volume up, its recordings are reshuffled, and a minute later the link is back. And
# everything that failed mid-run was treated as an outage, so a revoked key had the spool queueing for ever
# for a place that was never coming back. The errno says which it is.

def _refusing_open(monkeypatch_target, url, err):
    """Make opening the archive at `url` fail with `err`, the way a network volume fails at open."""
    import vms.recworker as rw
    real = rw.ArchiveResource

    class Opening(real):
        def __init__(self, spool, root, *a, create=True, **k):
            if root == url and create:
                raise err
            super().__init__(spool, root, *a, create=create, **k)
    rw.ArchiveResource = Opening
    return lambda: setattr(rw, "ArchiveResource", real)


def test_an_archive_that_is_away_at_open_is_kept_and_buffered_into():
    """A recorder that restarts in the middle of an outage opens its archive and gets a timeout. That is
    "away", not "wrong": handing the volume back would reshuffle its recordings for a link that is back in a
    minute. So the recorder keeps it — as a place, at full capacity — records into the spool as it always
    does, and promotion keeps trying. It says the archive is away; it does not say the volume is not a place."""
    import errno
    box = Box()
    cloud = os.path.join(box.root, "cloud")
    volumes.write(box.vars, {"name": "cloud", "kind": "local", "url": cloud, "server": "srv-a", "quota_bytes": 10 ** 9})
    restore = _refusing_open(None, cloud, OSError(errno.ETIMEDOUT, "Connection timed out"))
    try:
        r = _recorder(box, "r-1", "srv-a")
        assert r.volume_pass() == "cloud", "a volume that timed out at open was handed back"
        assert r.capacity == r.full_capacity and r.volume_error == ""   # still a place: nothing moves off it
        assert r.archive.root == cloud                                   # …and it points there, so segments go there
        hb = r.heartbeat_extra()
        assert hb["archive_error"] and hb["archive_failure"] == "transient"
    finally:
        restore()


def test_an_archive_that_refuses_writes_mid_run_is_handed_back():
    """The other way round. The recorder holds `vol-a`, and promotion starts failing with "permission
    denied" — a key that was revoked, a bucket policy that changed. Nothing will fix that but a person, so
    buffering is waiting for nothing while the spool grows. The volume is handed back, its recordings are
    free to go somewhere that works, and what was already recorded stays in the spool marked for `vol-a`.

    And it does not take `vol-a` straight back on the next pass: opening may well succeed (the directories
    are there) and the first write fail again — a recorder flapping between taking and dropping the same
    broken archive. It is left alone for a while, long enough for somebody to fix it."""
    import errno
    box = Box()
    a, b = os.path.join(box.root, "vol-a"), os.path.join(box.root, "vol-b")
    volumes.write(box.vars, {"name": "vol-a", "kind": "local", "url": a, "server": "srv-a", "quota_bytes": 10 ** 9})
    r = _recorder(box, "r-1", "srv-a")
    assert r.volume_pass() == "vol-a"
    seg = _closed_segment(box, r)
    volumes.write(box.vars, {"name": "vol-b", "kind": "local", "url": b, "server": "srv-a", "quota_bytes": 10 ** 9})

    def refused(*a, **k):
        raise OSError(errno.EACCES, "Permission denied")
    r.archive.promote = refused
    r.promote_closed()
    assert r.heartbeat_extra()["archive_failure"] == "permanent"

    assert r.volume_pass() == "vol-b", "a volume that refuses writes was kept, and the spool queues for nothing"
    assert os.path.isfile(seg) and not os.path.exists(os.path.join(b, "rec", "7"))   # vol-a's footage waits for vol-a
    box.wall.advance(30)
    r.leave_volume("test: let go of vol-b")                         # free again, and vol-a sorts first…
    assert r.volume_pass() != "vol-a", "it took the broken archive straight back"


def test_a_full_archive_is_kept_so_the_one_process_that_can_free_it_still_holds_it():
    """"No space" looks permanent and is not. A full archive is emptied by its own watermark (Lesson 18),
    and that pass is run by the recorder holding it — hand a full volume back and the one process that
    could free it stops holding it, and it stays full. So a full archive is an outage: the recorder keeps it
    and buffers, and the watermark makes the room."""
    import errno
    box = Box()
    a, b = os.path.join(box.root, "vol-a"), os.path.join(box.root, "vol-b")
    volumes.write(box.vars, {"name": "vol-a", "kind": "local", "url": a, "server": "srv-a", "quota_bytes": 10 ** 9})
    r = _recorder(box, "r-1", "srv-a")
    assert r.volume_pass() == "vol-a"
    _closed_segment(box, r)
    volumes.write(box.vars, {"name": "vol-b", "kind": "local", "url": b, "server": "srv-a", "quota_bytes": 10 ** 9})

    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    r.archive.promote = full
    r.promote_closed()
    assert r.heartbeat_extra()["archive_failure"] == "transient"
    assert r.volume_pass() == "vol-a", "a full archive was handed back — and nobody is left to empty it"


def test_an_archive_that_is_away_until_the_spool_is_full_is_handed_back():
    """An archive that is AWAY is waited for — but the waiting has to end somewhere, and the honest place
    is where waiting stops being free: the local spool running out. Until then every minute of outage is
    footage kept; after it, every minute is the recordings that COULD be delivered losing their room to a
    queue for a place that is not answering.

    So an away archive becomes a wrong one when the spool's disk crosses the high mark — the watermark's
    own number (Lesson 18), read even when the watermark is not switched on, because switching it on means
    DELETING footage for room and this only means giving up the wait. The volume is handed back like any
    wrong one; its recordings stop, the ones that can still be delivered keep their room, and what it
    already holds stays in the spool, marked for it."""
    import errno
    box = Box()
    a, b = os.path.join(box.root, "vol-a"), os.path.join(box.root, "vol-b")
    volumes.write(box.vars, {"name": "vol-a", "kind": "local", "url": a, "server": "srv-a", "quota_bytes": 10 ** 9})
    r = _recorder(box, "r-1", "srv-a")
    assert r.volume_pass() == "vol-a"
    seg = _closed_segment(box, r)
    volumes.write(box.vars, {"name": "vol-b", "kind": "local", "url": b, "server": "srv-a", "quota_bytes": 10 ** 9})

    def away(*a, **k):
        raise OSError(errno.ETIMEDOUT, "Connection timed out")
    r.archive.promote = away
    r.promote_closed()
    assert r.archive_failure == "transient"

    r.space_probe = lambda path: (100, 50)                         # half full: room to wait
    assert r.volume_pass() == "vol-a", "an away archive was given up while the spool still had room"

    r.space_probe = lambda path: (100, 5) if path == box.spool else (100, 50)   # the spool is past the high mark
    assert r.volume_pass() == "vol-b", "the spool filled up and the recorder went on waiting for an archive that is away"
    refused = r.heartbeat_extra()["refused"]
    assert "vol-a" in refused and "spool" in refused["vol-a"]       # why it stopped waiting is said
    assert os.path.isfile(seg)                                     # and vol-a's footage stays, marked for vol-a


def test_a_full_spool_with_a_healthy_archive_hands_nothing_back():
    """The rule is about WAITING. A spool that is full while its archive is taking segments is a local
    capacity problem — the watermark's to solve — and nothing is gained by giving up a volume that works."""
    box = Box()
    a = os.path.join(box.root, "vol-a")
    volumes.write(box.vars, {"name": "vol-a", "kind": "local", "url": a, "server": "srv-a", "quota_bytes": 10 ** 9})
    r = _recorder(box, "r-1", "srv-a")
    assert r.volume_pass() == "vol-a"
    r.space_probe = lambda path: (100, 1)                          # full, everywhere
    assert r.volume_pass() == "vol-a"
