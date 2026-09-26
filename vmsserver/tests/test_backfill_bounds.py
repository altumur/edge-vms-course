"""Lesson 16, from the product — the three bounds backfill was missing.

Written after the course's recorder ran on a real box (ОБРАТНАЯ-СВЯЗЬ-ИЗ-ПРОДУКТА.md, points P, Q and S):

    P  what a clean fetch found nowhere is remembered, per source, and not planned again — a summary
       cannot say where a card's holes are, and without this the same empty range is fetched every pass
    Q  the upper bound is the end of what we can SEE, not a guess called `settle` that held only while
       `settle > segment + grace`, an inequality written nowhere
    S  planned backfill fills holes INSIDE what a recording recorded — before its first second it was not
       recording by design, and filling that turns a recording on events into a recording always
"""
from vms.archive import ArchiveResource, Manifest, subtract
from vms.config import REC_SPEC
from vms.recworker import RecWorker
from vms.worker import FakeActuator, FakeDevice
from w2cplatform.contract import Heartbeat
from w2cplatform.spec import SpecController
from tests.test_lesson11_edge import CARD, _box, _holder, _ours

NOW = 1_000_000.0


def _recorder(act=None, settle=1000.0, card=(0.0, NOW)):
    box, ctl, con, con_vars = _box()
    w = _holder(box, lambda k: FakeDevice(k, channels=["1"], coverage={"1": (*card, 5)}))
    con.create_camera({"name": "front", "source": CARD})
    ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()
    rec_ctl = SpecController(REC_SPEC, box.vars.as_writer("reccontroller", REC_SPEC.acl_controller()), box.objects, wall=box.wall)
    con_rec = SpecController(REC_SPEC, con_vars, box.objects, wall=box.wall)
    con_rec.create({"name": "1", "cam": "1"})
    r = RecWorker("r-1", box.vars.as_writer("recworker", ["rec/epoch/*", "rec/slots/*"]), box.objects,
                  act or FakeActuator(), archive=ArchiveResource(box.spool, box.archive, wall=box.wall),
                  clock=box.clock, wall=box.wall, server="srv-1", env={}, keep_days=1.0, settle=settle)
    r.heartbeat_once(); rec_ctl.ensure_placed(); r.reconcile_once()
    return box, r, con_rec


def test_a_range_the_card_does_not_have_is_fetched_once_and_then_remembered():
    """We lost an hour; so did the card, for twenty minutes of it — both were down when the site lost
    power. The card's summary says it holds the whole day. The first pass fetches the hour, gets forty
    minutes, and remembers the twenty it found nowhere. The next pass plans nothing: before this it planned
    the same twenty minutes every pass, and each plan opened one of the camera's one or two sessions."""
    act = FakeActuator()
    hole = (NOW - 74000, NOW - 72800)
    act.available = lambda source, t0, t1: subtract((t0, t1), [hole])
    box, r, _ = _recorder(act)
    _ours(box, r, 1, ((NOW - 80000, NOW - 76400), (NOW - 70000, NOW - 66400)))

    done = r.backfill(budget=1, now=NOW, force=True)
    assert done[0]["segments"] > 0 and r.nowhere[("1", "device")] == [hole]
    asked = len(act.fetched)
    assert r.backfill(budget=1, now=NOW, force=True) == []                  # nothing left to plan
    assert len(act.fetched) == asked
    r.heartbeat_once()
    hb = Heartbeat.from_bytes(box.objects.get(REC_SPEC.sub.heartbeat_key("r-1")))
    assert hb.extra["nowhere_seconds"] == 1200                              # "of what we lost, 1200 s were not on the card either"


def test_a_fetch_that_failed_half_way_is_not_remembered_as_nowhere():
    """A pipeline that died says nothing about what the card holds. Remembering its range as nowhere would
    lose footage the card still has, silently, for good."""
    class Broken(FakeActuator):
        def record_range(self, *a, **kw):
            self.range_error = "the playback session was refused"
            return []
    act = Broken()
    box, r, _ = _recorder(act)
    _ours(box, r, 1, ((NOW - 80000, NOW - 76400), (NOW - 70000, NOW - 66400)))
    done = r.backfill(budget=1, now=NOW, force=True)
    assert done[0]["error"] == "the playback session was refused" and r.nowhere == {}
    assert r.backfill(budget=1, now=NOW, force=True)                         # asked again next pass


def test_backfill_stops_at_what_is_visible_not_at_a_guess():
    """Twenty-minute segments, `settle` of two minutes. The segment being written is in no manifest for up
    to twenty minutes, so by the old bound everything between the end of our visible footage and two
    minutes ago was a gap — the recorder fetched from the card what it was recording that minute, and the
    overlap check could not catch it: there was nothing in the manifest to overlap. The end of what we can
    see is the lag, measured; nothing after it is planned."""
    box, r, _ = _recorder(settle=120.0)
    _ours(box, r, 1, ((NOW - 7200, NOW - 1200),))                           # the last visible second: 20 min ago
    assert r.gaps(1, {"from": 0.0, "to": NOW}, NOW) == []
    assert r.backfill(budget=1, now=NOW, force=True) == []


def test_planned_backfill_fills_holes_inside_what_was_recorded_and_not_before_it():
    """The recording was created an hour ago; the card holds the whole day. The day before the first second
    is not a hole — nothing was meant to record it — and filling it would make every recording on events a
    recording always, a night late. A hole after the first second is one. An operator who wants the morning
    anyway asks for it, and a request is not planned: it is fetched."""
    box, r, con_rec = _recorder()
    _ours(box, r, 1, ((NOW - 3600, NOW - 2400), (NOW - 1800, NOW - 1200)))
    assert r.gaps(1, {"from": 0.0, "to": NOW}, NOW) == [(NOW - 2400, NOW - 1800)]

    con_rec.vars.put(REC_SPEC.sub.request_key("1-morning"),
                     {"unit": "1", "cam": "1", "from": str(NOW - 30000), "to": str(NOW - 28000), "at": str(NOW), "by": "anna"})
    r.backfill_budget = 0
    r.pump_once()
    assert r.fetched == ["1-morning"]
    assert any(s.start == NOW - 30000 for s in Manifest(box.archive, 1).read())
