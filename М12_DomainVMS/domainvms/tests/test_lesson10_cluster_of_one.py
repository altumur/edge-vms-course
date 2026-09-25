"""Lesson 10 — a cluster of one.

A camera that runs the platform is a cluster of its own: one store on its flash, one writer, a unit pinned
to its hardware, no controller, no slot, its own epoch. For the domain it must be nothing special — the
directory, the read view, the forwarded write, the agent and Lesson 9's kept edits read it exactly as they
read a server room. What IS special is small and physical: flash that wears, and a box that is rebooted
whole, which makes the order of its boot a correctness question.
"""
from cluster.variables import FakeVariables

from domain.agent import DomainAgent, DomainPublisher
from domain.api import ApiError, ConsoleAPI
from domain.device import DeviceCluster
from domain.federation import DomainDirectory, Federation
from domain.grants import Grant
from domain.pending import PendingEdits
from domain.readview import ReadView
from tests.conftest import Clock, Running, make_cluster

SERIAL = "SN4471"


def _domain(wall, *serials):
    """A server room (М11's real controller and a worker) that hosts the domain, and cameras, each its own
    cluster."""
    fed = Federation()
    north, _ = make_cluster("north", domain=True)
    fed.add(north)
    room = Running(north, wall)
    room.create(101, 102)
    devices = {}
    for s in serials:
        d = DeviceCluster(s, FakeVariables(), wall=wall)
        d.boot(first_name=f"gate-{s}")
        fed.add(d.cluster())
        devices[s] = d
    return fed, room, devices


def _grant(fed, cluster, subject, wall, capability="edit"):
    DomainPublisher(fed.domain_cluster.vars).publish_grants(cluster, [Grant(subject, capability, None, wall() + 3600)])


def test_the_domain_reads_a_camera_the_way_it_reads_a_server_room():
    """Nothing in the directory or the read view was written for devices. A camera publishes one heartbeat
    and one snapshot shard in the shapes a cluster publishes, and it is found by its serial — the domain's
    name for it — on a worker and a server that are both the camera itself."""
    wall = Clock()
    fed, room, devices = _domain(wall, SERIAL, "SN4472")
    ans = DomainDirectory(fed, wall=wall).where(SERIAL)
    assert (ans.cluster, ans.worker, ans.server, ans.complete) == (f"cam-{SERIAL}", SERIAL, SERIAL, True)
    assert DomainDirectory(fed, wall=wall).where("101").cluster == "north"      # and the server room, unchanged

    view = ReadView(fed, wall=wall)
    view.refresh()
    mine = [r for r in view.rows() if r.cluster == f"cam-{SERIAL}"]
    assert [(r.ref, r.worker_state, r.epoch) for r in mine] == [(SERIAL, "live", 1)]


def test_the_epoch_grows_on_every_boot_and_the_archive_path_carries_it():
    """There is no second instance for the epoch to fence. It is still taken, by the camera, from its flash,
    on every boot — because the archive and the event buckets carry it, and footage from after a reboot
    must never land in the directory of footage from before it."""
    d = DeviceCluster(SERIAL, FakeVariables(), wall=Clock())
    assert d.boot() == 1 and d.rec_prefix() == f"rec/{SERIAL}/e1/"
    d.power_off()
    assert d.boot() == 2 and d.rec_prefix() == f"rec/{SERIAL}/e2/"
    assert d.row()["ref"] == SERIAL and d.row()["revision"] == 1              # the row was made once, at the FIRST boot


def test_a_day_of_heartbeats_costs_the_flash_nothing():
    """A heartbeat every ten seconds is 8640 writes a day. On a server's disk that is nothing; on a
    camera's flash, over years, it is the camera. So what changes every few seconds lives in RAM and is
    read through the door; flash takes the epoch, the row, and what the agent carries when it changes."""
    wall = Clock()
    fed, room, devices = _domain(wall, SERIAL)
    d = devices[SERIAL]
    _grant(fed, d.name, "anna", wall)
    agent = DomainAgent(d.name, fed.domain_cluster.vars, d.flash, now=wall)
    after_boot = d.flash.writes
    for i in range(8640):                                # one day, a heartbeat every 10 s
        wall.advance(10)
        d.publish()
        if i % 3 == 0:
            agent.sync()                                 # and the agent every 30 s
    assert after_boot == 2                               # the epoch and the row
    assert d.flash.writes - after_boot == 1              # the grants, once, when they arrived — and nothing else


def test_nothing_places_a_pinned_unit_so_nothing_a_placer_writes_is_there():
    """The camera IS the unit. Its store holds its row, its epoch, and what the agent carried — `domain/*`.
    No slot, no assignment, no snapshot written by a controller: not forbidden, simply absent, because
    nobody's job is to write them."""
    wall = Clock()
    fed, room, devices = _domain(wall, SERIAL)
    d = devices[SERIAL]
    _grant(fed, d.name, "anna", wall)
    DomainAgent(d.name, fed.domain_cluster.vars, d.flash, now=wall).sync()
    assert d.flash.list("") == ["domain/grants", "vms/cameras/1", "vms/epoch/1"]


def test_an_edit_from_the_domain_is_the_cameras_own_console_deciding():
    """The domain forwards the edit, as Lesson 3 does for any cluster, and the camera's console checks the
    subject against the grants ITS agent carried. A camera writes its own row, always; the domain owns
    nothing."""
    wall = Clock()
    fed, room, devices = _domain(wall, SERIAL)
    d = devices[SERIAL]
    _grant(fed, d.name, "anna", wall)
    DomainAgent(d.name, fed.domain_cluster.vars, d.flash, now=wall).sync()
    api = ConsoleAPI(DomainDirectory(fed, wall=wall), lambda name: devices[SERIAL], verifier=lambda token: token)

    r = api.update_camera(SERIAL, {"name": "main-gate"}, idempotency_key="k1", token="anna")
    assert r["cluster"] == d.name and d.row()["name"] == "main-gate" and d.row()["revision"] == 2
    try:
        api.update_camera(SERIAL, {"name": "x"}, idempotency_key="k2", token="boris")
        raise AssertionError("boris has no grant on this camera")
    except ApiError as e:
        assert e.status == 403


def test_the_door_opens_after_the_first_publish_not_before():
    """A camera is rebooted whole, which a server cluster never is. For the seconds between power and the
    first publish its RAM is empty — and a door open in those seconds says "I hold no cameras", a COMPLETE
    answer that the domain must believe: an edit sent then is a 404, the camera 'is not anywhere'. Closed
    until the publish, the same seconds are a camera that did not answer, and the edit is kept."""
    wall = Clock()
    fed, room, devices = _domain(wall, SERIAL)
    d = devices[SERIAL]
    view = ReadView(fed, wall=wall)
    view.refresh()
    api = ConsoleAPI(DomainDirectory(fed, wall=wall), lambda name: d, pending=PendingEdits(fed.domain_cluster.vars, wall),
                     last_known=view.last_known)

    # the wrong order, by hand: power, the door, and nothing published yet
    d.power_off(); d.ram = type(d.ram)(); d.door_open = True
    try:
        api.update_camera(SERIAL, {"name": "x"}, idempotency_key="k1")
        raise AssertionError("an open door with nothing published is a complete answer")
    except ApiError as e:
        assert e.status == 404

    # the right order: the door is closed while it boots, so the same moment is "did not answer"
    d.power_off()
    r = api.update_camera(SERIAL, {"name": "main-gate"}, idempotency_key="k2")
    assert r["pending"] is True

    # …and `boot` keeps that order: the door is still shut while the first publish is being made
    shut_during_publish = []
    publish = d.publish
    d.publish = lambda: (shut_during_publish.append(not d.door_open), publish())
    d.boot()
    assert shut_during_publish == [True] and d.door_open


def test_a_kept_edit_reaches_a_real_camera_when_it_boots():
    """Lesson 9, end to end, on a device: the camera goes off, the edit is kept beside its grants, the camera
    boots, its agent carries the edit home and its console applies it as the operator, and the domain clears
    what landed."""
    wall = Clock()
    fed, room, devices = _domain(wall, SERIAL)
    d = devices[SERIAL]
    _grant(fed, d.name, "anna", wall)
    agent = DomainAgent(d.name, fed.domain_cluster.vars, d.flash, now=wall, console=d, current=d.current)
    agent.sync()
    view = ReadView(fed, wall=wall)
    view.refresh()
    pending = PendingEdits(fed.domain_cluster.vars, wall)
    api = ConsoleAPI(DomainDirectory(fed, wall=wall), lambda name: d, verifier=lambda token: token,
                     pending=pending, last_known=view.last_known)

    d.power_off()
    view.refresh()
    assert api.update_camera(SERIAL, {"name": "main-gate"}, idempotency_key="k1", token="anna")["pending"]
    wall.advance(3600)
    _grant(fed, d.name, "anna", wall)                    # grants are renewed while it is off; they expire otherwise
    d.boot()
    agent.sync()
    assert d.row()["name"] == "main-gate"
    pending.collect(fed)
    assert pending.of(d.name) == {}
