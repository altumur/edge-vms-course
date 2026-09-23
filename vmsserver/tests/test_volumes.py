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
