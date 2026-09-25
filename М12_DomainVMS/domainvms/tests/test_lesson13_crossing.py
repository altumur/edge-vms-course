"""Lesson 13 — a stream from another cluster.

A server's recorder recording a camera that is a cluster of its own. The recorder is unchanged in what it
does and changed only in where it finds the camera: not in its own cluster's heartbeats, which do not
contain it, but in a source book the domain publishes for its cluster and the cluster's agent carries home
— so the recording goes on with the domain switched off. Data crosses between clusters; work does not.
"""
from cluster.variables import FakeVariables

from domain.agent import DomainAgent
from domain.api import ApiError
from domain.crossing import Crossings, NotResolvable, plan_backfill, resolve
from domain.device import DeviceCluster
from domain.federation import Federation
from domain.readview import ReadView
from tests.conftest import Clock, Running, make_cluster

SERIAL = "SN4471"


def _site(wall):
    fed = Federation()
    north, north_link = make_cluster("north", domain=True)          # the domain's home
    south, _ = make_cluster("south")                                 # a server room with a recorder
    fed.add(north); fed.add(south)
    room = Running(south, wall)
    room.create(201)
    cam = DeviceCluster(SERIAL, FakeVariables(), wall=wall, address="10.1.0.71")
    cam.coverage = {"from": wall() - 3600, "to": wall()}           # an hour on the card
    cam.boot()
    fed.add(cam.cluster())
    view = ReadView(fed, wall=wall)
    view.refresh()
    crossings = Crossings(north.vars, view, wall)
    agent = DomainAgent("south", north.vars, south.vars, now=wall)
    return fed, north_link, south, cam, view, crossings, agent


def test_the_recorder_finds_a_camera_of_another_cluster_in_its_own_clusters_book():
    """The domain decides that south records the camera, publishes south's book from what it last read of
    the camera's cluster, and south's agent carries it home. The recorder resolves `ref:SN4471` against
    south's own Variables. And the camera's store is exactly as it was: nobody wrote into it."""
    wall = Clock()
    fed, north_link, south, cam, view, crossings, agent = _site(wall)
    writes = cam.flash.writes
    assert crossings.record(SERIAL, on="south") == {"camera": SERIAL, "recorded_by": "south", "from": f"cam-{SERIAL}"}
    crossings.publish()
    agent.sync()
    src = resolve(south.vars, f"ref:{SERIAL}", wall())
    assert (src.cluster, src.live_url, src.playback_url) == (f"cam-{SERIAL}", "rtsp://10.1.0.71/live", "http://10.1.0.71/playback")
    assert cam.flash.writes == writes                    # data will cross; nothing was written across
    assert resolve(south.vars, "rtsp://south-cam/201", wall()) is None       # its own sources: found as always


def test_one_camera_one_recording_cluster():
    """A camera serves one live session and one backfill. A second cluster recording it would take them
    from the first — so which cluster records it is the domain's decision, stored, and a second asker is
    refused with the reason, as Lesson 1 refuses a second placement."""
    wall = Clock()
    fed, north_link, south, cam, view, crossings, agent = _site(wall)
    crossings.record(SERIAL, on="south")
    assert crossings.record(SERIAL, on="south")["recorded_by"] == "south"      # asking again is the same answer
    for on, status in (("north", 409), (f"cam-{SERIAL}", 400)):
        try:
            crossings.record(SERIAL, on=on)
            raise AssertionError(f"{on} must be refused")
        except ApiError as e:
            assert e.status == status
    try:
        crossings.record("SN9999", on="south")
        raise AssertionError("an unseen camera cannot be recorded")
    except ApiError as e:
        assert e.status == 404


def test_the_recording_goes_on_with_the_domain_switched_off_and_says_how_old_its_address_is():
    """The thesis, for crossings: the recorder never asks the domain, it asks its own cluster's copy. With
    the domain gone the address is the one last carried, and its age grows — shown, never hidden."""
    wall = Clock()
    fed, north_link, south, cam, view, crossings, agent = _site(wall)
    crossings.record(SERIAL, on="south"); crossings.publish(); agent.sync()
    north_link.up = False
    wall.advance(6 * 3600)
    assert agent.sync() is False
    src = resolve(south.vars, f"ref:{SERIAL}", wall())
    assert src.live_url == "rtsp://10.1.0.71/live" and src.age >= 6 * 3600


def test_a_camera_that_moved_while_the_domain_was_off_is_found_again_when_it_is_back():
    """The failure this design accepts, stated: a camera that changes its address while the domain is off
    is recorded from the old address until the domain is back — the recorder sees a dead URL, reports it,
    and has nothing better. When the domain returns, one pass and one carry and the book is right again."""
    wall = Clock()
    fed, north_link, south, cam, view, crossings, agent = _site(wall)
    crossings.record(SERIAL, on="south"); crossings.publish(); agent.sync()
    north_link.up = False
    cam.address = "10.1.0.99"; cam.publish()                         # a new lease from DHCP
    assert resolve(south.vars, f"ref:{SERIAL}", wall()).live_url == "rtsp://10.1.0.71/live"
    north_link.up = True
    view.refresh(); crossings.publish(); agent.sync()
    assert resolve(south.vars, f"ref:{SERIAL}", wall()).live_url == "rtsp://10.1.0.99/live"


def test_backfill_trusts_the_book_to_plan_and_the_card_to_fetch():
    """The book says the card holds the last hour; south holds all but twenty minutes of it. That is the
    plan. Then the device is asked, and the card — a ring — has overwritten its oldest part since the book
    was carried: what is still there is fetched, what is not is dropped with its reason, not retried."""
    wall = Clock(100_000.0)
    fed, north_link, south, cam, view, crossings, agent = _site(wall)
    crossings.record(SERIAL, on="south"); crossings.publish(); agent.sync()
    now = wall()
    src = resolve(south.vars, f"ref:{SERIAL}", now)
    ours = [(now - 3600, now - 1800), (now - 600, now)]              # a gap from −30 to −10 minutes
    fetch, dropped = plan_backfill(src, ours, now, keep_days=30, settle=60,
                                   ask_device=lambda: {"from": now - 1500, "to": now})
    assert fetch == [(now - 1500, now - 600)]
    assert dropped == [((now - 1800, now - 1500), "no longer on the card")]


def test_a_camera_the_book_does_not_hold_is_said_by_name():
    wall = Clock()
    fed, north_link, south, cam, view, crossings, agent = _site(wall)
    try:
        resolve(south.vars, f"ref:{SERIAL}", wall())
        raise AssertionError("nothing was carried yet")
    except NotResolvable as e:
        assert SERIAL in str(e) and "source book" in str(e)
