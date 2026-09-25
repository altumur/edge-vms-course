"""Lesson 14 — alarms from every member.

One list of alarms across cameras that are each their own cluster: merged by the domain from each member's
door, newest first, with what it could not reach said as part of the answer. And a second copy, because the
camera that is off is the camera whose last alarms matter: a neighbour keeps its closed alarm buckets,
pulled, never pushed, and the list says up to when the copy knows.
"""
from cluster.variables import FakeVariables

from domain.agent import DomainAgent
from domain.alarms import Card, DomainAlarms, EventDoor, MirrorPlan, mirror_once
from domain.device import DeviceCluster
from domain.federation import Federation
from tests.conftest import Clock, make_cluster

NOW = 1_001_000.0                     # the open bucket is [1 000 800, 1 001 400)


def _site(wall, nets=("vlan:a", "vlan:a", "vlan:b")):
    fed = Federation()
    north, _ = make_cluster("north", domain=True)
    fed.add(north)
    devices, cards = {}, {}
    for i, net in enumerate(nets):
        d = DeviceCluster(f"SN{i}", FakeVariables(), wall=wall, reaches=(net,))
        d.boot()
        fed.add(d.cluster())
        devices[d.name], cards[d.name] = d, Card()
    doors = lambda name: EventDoor(devices[name], cards[name])
    plan = MirrorPlan(north.vars, copies=1)
    plan.publish(fed)
    for name, d in devices.items():
        DomainAgent(name, north.vars, d.flash, now=wall).sync()             # carries `domain/mirrors` home
    return fed, north, devices, cards, doors, plan


def test_one_list_newest_first_each_line_naming_its_member():
    wall = Clock(NOW)
    fed, north, devices, cards, doors, plan = _site(wall)
    cards["cam-SN0"].observe(1, NOW - 1500, "door_forced", alarm=True)
    cards["cam-SN1"].observe(1, NOW - 900, "stream_lost", alarm=True)
    cards["cam-SN2"].observe(1, NOW - 100, "tamper", alarm=True)
    cards["cam-SN2"].observe(1, NOW - 90, "motion")                       # an observation: not an alarm
    out = DomainAlarms(fed, doors, plan, wall).list(since=NOW - 3600)
    assert [(e["member"], e["kind"]) for e in out["events"]] == [("cam-SN2", "tamper"), ("cam-SN1", "stream_lost"), ("cam-SN0", "door_forced")]
    assert out["complete"] and out["sentence"] == "every member answered"


def test_a_member_that_is_off_is_answered_from_its_neighbours_copy_up_to_when_it_knows():
    """SN0 raised an alarm twenty-five minutes ago and another a minute and a half ago, and then went off.
    Its neighbour on the same network had pulled its closed buckets: the first alarm is on the list, from
    the copy. The second was in the open bucket, in no copy — and the list does not pretend otherwise: it
    says up to when the copy knows, and that nothing is known since."""
    wall = Clock(NOW)
    fed, north, devices, cards, doors, plan = _site(wall)
    cards["cam-SN0"].observe(1, NOW - 1500, "door_forced", alarm=True)
    cards["cam-SN0"].observe(1, NOW - 90, "door_forced", alarm=True)
    holder = plan.holders("cam-SN0")[0]
    assert holder == "cam-SN1"                                             # the neighbour on vlan:a
    assert mirror_once(cards[holder], devices[holder].flash, doors, NOW) == 1
    devices["cam-SN0"].power_off()

    out = DomainAlarms(fed, doors, plan, wall).list(since=NOW - 3600)
    mine = [e for e in out["events"] if e["member"] == "cam-SN0"]
    assert [(e["t"], e["from_mirror_on"]) for e in mine] == [(NOW - 1500, "cam-SN1")]
    m = out["members"]["cam-SN0"]
    assert (m["state"], m["via"], m["known_until"]) == ("mirror", "cam-SN1", 999_600.0)
    assert not out["complete"] and "known up to" in out["sentence"] and "none known since" in out["sentence"]


def test_a_member_with_no_reachable_copy_is_said_to_be_missing_not_quiet():
    wall = Clock(NOW)
    fed, north, devices, cards, doors, plan = _site(wall)
    cards["cam-SN2"].observe(1, NOW - 1500, "tamper", alarm=True)
    holder = plan.holders("cam-SN2")[0]
    devices["cam-SN2"].power_off(); devices[holder].power_off()
    out = DomainAlarms(fed, doors, plan, wall).list(since=NOW - 3600)
    assert out["members"]["cam-SN2"]["state"] == "unreachable"
    assert "cam-SN2 off, and no copy of its alarms could be reached" in out["sentence"]


def test_the_copy_is_closed_alarm_buckets_only_and_taking_it_twice_takes_nothing():
    """Closed: an open bucket is still being written, and a copy of it would be a copy of half of it. Alarm
    lines only: a neighbour's flash is not a second card. And because a closed bucket never changes, the
    second pass finds everything already there and copies nothing."""
    wall = Clock(NOW)
    fed, north, devices, cards, doors, plan = _site(wall)
    src, holder = cards["cam-SN0"], plan.holders("cam-SN0")[0]
    src.observe(1, NOW - 2500, "door_forced", alarm=True)
    src.observe(1, NOW - 2400, "motion")
    src.observe(1, NOW - 1800, "motion")                                  # a closed bucket with no alarm in it
    src.observe(1, NOW - 50, "door_forced", alarm=True)                   # the open bucket
    assert mirror_once(cards[holder], devices[holder].flash, doors, NOW) == 1
    assert mirror_once(cards[holder], devices[holder].flash, doors, NOW) == 0
    copy = cards[holder].mirrored("cam-SN0", 0, NOW + 1, 100)["events"]
    assert [(e["t"], e["kind"]) for e in copy] == [(NOW - 2500, "door_forced")]


def test_who_keeps_whose_copy_is_stable_as_the_site_grows():
    """Three hundred cameras on three networks, one copy each, kept by a camera on the same network. Add
    one camera and the plan changes where the newcomer outranks an existing holder — a handful of pairs,
    not a reshuffle of three hundred copies across the site's uplinks."""
    plan = MirrorPlan(FakeVariables(), copies=1)
    members = {f"cam-SN{i:03d}": frozenset({f"vlan:{i % 3}"}) for i in range(300)}
    before = plan.choose(members)
    assert all(a not in bs and members[a] & members[bs[0]] for a, bs in before.items())
    members["cam-SN999"] = frozenset({"vlan:0"})
    after = plan.choose(members)
    moved = [a for a in before if before[a] != after[a]]
    assert len(moved) <= 5


def test_a_storm_on_one_camera_does_not_push_the_others_off_the_page():
    """One camera raising a hundred and fifty alarms in an hour is a camera with a problem, and it is one
    line of news. Each member is asked for at most a page; the one that had more says so; the alarm from
    the quiet camera next to it is still on the list."""
    wall = Clock(NOW)
    fed, north, devices, cards, doors, plan = _site(wall)
    for i in range(150):
        cards["cam-SN1"].observe(1, NOW - 3000 + i * 10, "stream_lost", alarm=True)
    cards["cam-SN0"].observe(1, NOW - 3500, "door_forced", alarm=True)
    out = DomainAlarms(fed, doors, plan, wall, per_member=100).list(since=NOW - 3600)
    assert ("cam-SN0", "door_forced") in [(e["member"], e["kind"]) for e in out["events"]]
    assert out["members"]["cam-SN1"]["truncated"] and not out["members"]["cam-SN0"]["truncated"]
    assert "cam-SN1 had more alarms than one page holds" in out["sentence"]
