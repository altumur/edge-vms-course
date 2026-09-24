"""The evaluator: the first worker in the course whose work is reading.

What it must get right is not the matching — that is `fires()`, tested beside
the language — but everything around a decision taken from a log that is
at-least-once, replayed after a restart and shared with five other subsystems.
"""
import json

from w2cplatform.contract import requests_acl
from w2cplatform.variables import Forbidden
from vms.auto import AutoController
from vms.autoworker import AutoWorker
from vms.config import AUTO_SPEC, REC_SPEC, SPEC as VMS_SPEC
from tests.conftest import Box


class _Log:
    """The merged event index, as a list. The real one asks every live resource
    over HTTP; what the evaluator needs from it is this shape."""
    def __init__(self, events=()):
        self.events = list(events)
        self.asked: list[tuple] = []

    def query(self, t0, t1, **kw):
        self.asked.append((t0, t1))
        return {"events": [e for e in self.events if t0 <= e["t"] <= t1], "state": "live"}


def ev(t, sub, unit, kind, **fields):
    return {"t": float(t), "sub": sub, "subsystem": sub, "unit": str(unit), "kind": kind, "epoch": 1,
            "server": "srv-a", "fenced": False, **fields}


DOOR = {"name": "door-on-badge",
        "when": [{"sub": "vms", "kind": "io.input", "unit": "12", "match": {"port": "1", "value": "closed"}},
                 {"sub": "det", "kind": "motion", "unit": "7"}],
        "within": 30,
        "then": [{"sub": "vms", "action": "output", "unit": "12", "port": 2, "pulse_ms": 500},
                 {"sub": "rec", "action": "record", "cam": "7", "minutes": 10}]}


def _worker(box, log, name="a-1"):
    """With the token a scenario evaluator actually gets: its own prefix, plus
    the one family of somebody else's it may write."""
    vars_ = box.vars.as_writer("autoworker", AUTO_SPEC.sub.acl_worker() + requests_acl("vms", "rec"))
    return AutoWorker(name, vars_, box.objects, index=log, clock=box.clock, wall=box.wall,
                      server="srv-a", archive_root=box.archive, env={})


def _assigned(box, scenario, worker="a-1"):
    ctl = AutoController(box.vars.as_writer("autocontroller", AUTO_SPEC.acl_controller()), box.objects, wall=box.wall)
    ctl.assign(worker, [scenario])
    return ctl


def _scenario(box, **patch):
    con = AutoController(box.vars.as_writer("console", AUTO_SPEC.acl_console()), box.objects, wall=box.wall)
    return con.create({**DOOR, **patch})


def test_two_triggers_inside_the_window_fire_once_and_file_what_was_asked():
    box = Box()
    t = box.wall()
    log = _Log([ev(t - 20, "det", 7, "motion"),
                ev(t - 5, "vms", 12, "io.input", port="1", value="closed")])
    _scenario(box); _assigned(box, "door-on-badge")
    w = _worker(box, log)

    assert w.reconcile_once() == ["door-on-badge"]

    rows = sorted(box.vars.list("vms/requests/") + box.vars.list("rec/requests/"))
    assert len(rows) == 2, "one firing, two actions, two rows"
    out, _ = box.vars.get([k for k in rows if k.startswith("vms/")][0])
    assert out["action"] == "output" and out["unit"] == "12" and out["port"] == "2"
    assert out["by"] == "auto/door-on-badge"
    assert float(out["valid_until"]) == (t - 5) + 30            # the scenario's own window, not a guess
    rec, _ = box.vars.get([k for k in rows if k.startswith("rec/")][0])
    assert rec["action"] == "record" and rec["cam"] == "7" and rec["minutes"] == "10"

    # …and the scenario says so in its own bucket: the answer to "why did the door open at 14:02"
    assert w.epochs["door-on-badge"] >= 1


def test_the_same_log_read_again_files_nothing_new():
    """At-least-once by construction: the window is re-read every pass, and the
    events that fired are still in it. The cursor says they were considered; the
    deterministic id says that even if it were wrong, the row would be the same
    row rather than a second door."""
    box = Box()
    t = box.wall()
    log = _Log([ev(t - 20, "det", 7, "motion"), ev(t - 5, "vms", 12, "io.input", port="1", value="closed")])
    _scenario(box); _assigned(box, "door-on-badge")
    w = _worker(box, log)

    w.reconcile_once()
    before = sorted(box.vars.list("vms/requests/") + box.vars.list("rec/requests/"))
    w.reconcile_once(); w.reconcile_once()
    assert sorted(box.vars.list("vms/requests/") + box.vars.list("rec/requests/")) == before

    # a FRESH process — the memory gone, the cursor kept — does not fire them again either
    w2 = _worker(box, log, name="a-1")
    w2.reconcile_once()
    assert sorted(box.vars.list("vms/requests/") + box.vars.list("rec/requests/")) == before


def test_outside_the_window_is_not_a_firing():
    """The window is the whole question. Two things that happened are not two
    things that happened together."""
    box = Box()
    t = box.wall()
    log = _Log([ev(t - 400, "det", 7, "motion"),                      # four hundred seconds earlier
                ev(t - 5, "vms", 12, "io.input", port="1", value="closed")])
    _scenario(box); _assigned(box, "door-on-badge")
    w = _worker(box, log)

    assert w.reconcile_once() == []
    assert box.vars.list("vms/requests/") == []


def test_a_bouncing_sensor_does_not_ring_the_door():
    """The ceiling, and why it is per scenario and per minute. A contact that
    chatters is not two hundred people at the door."""
    box = Box()
    t = box.wall()
    bounce = [ev(t - 30 + i * 0.2, "vms", 12, "io.input", port="1", value="closed") for i in range(50)]
    _scenario(box, name="buzz", when=[DOOR["when"][0]], within=0, rate_per_minute=3,
              then=[DOOR["then"][0]])
    _assigned(box, "buzz")
    w = _worker(box, _Log(bounce))

    for _ in range(5):
        w.reconcile_once()
    assert len(box.vars.list("vms/requests/")) == 3, "the ceiling holds across passes, not inside one"


def test_a_scenario_that_is_off_decides_nothing():
    box = Box()
    t = box.wall()
    log = _Log([ev(t - 5, "vms", 12, "io.input", port="1", value="closed")])
    _scenario(box, name="off", when=[DOOR["when"][0]], within=0, then=[DOOR["then"][0]], enabled=False)
    _assigned(box, "off")
    w = _worker(box, log)

    assert w.reconcile_once() == [] and box.vars.list("vms/requests/") == []
    assert w.status_by_unit["off"]["why"] == "disabled"


def test_the_token_reaches_requests_and_nothing_else():
    """The one grant that crosses a subsystem's name, and the reason it is safe
    to give: `requests` cannot change configuration, cannot place anything and
    cannot outlive the row it writes."""
    box = Box()
    vars_ = box.vars.as_writer("autoworker", AUTO_SPEC.sub.acl_worker() + requests_acl("vms", "rec"))
    vars_.put("vms/requests/x", {"unit": "12", "action": "output"})           # allowed
    vars_.put("rec/requests/y", {"unit": "7", "action": "record"})            # allowed
    for forbidden in ("vms/cameras/12", "rec/recordings/7", "vms/placement/12", "auto/scenarios/x"):
        try:
            vars_.put(forbidden, {"a": "b"})
            raise AssertionError(f"the evaluator wrote {forbidden}")
        except Forbidden:
            pass

    # and the grant is exactly two prefixes, named out loud
    assert requests_acl("vms", "rec") == ["rec/requests/*", "vms/requests/*"]


def test_a_fenced_event_is_not_evidence():
    """The merge marks events written under an epoch that is not current. A
    scenario must not fire on the word of a zombie: those minutes are already
    disputed, and acting on them is acting twice."""
    box = Box()
    t = box.wall()
    stale = ev(t - 5, "vms", 12, "io.input", port="1", value="closed"); stale["fenced"] = True
    _scenario(box, name="one", when=[DOOR["when"][0]], within=0, then=[DOOR["then"][0]])
    _assigned(box, "one")
    w = _worker(box, _Log([stale]))

    assert w.reconcile_once() == [] and box.vars.list("vms/requests/") == []
