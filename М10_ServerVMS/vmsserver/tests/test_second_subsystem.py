"""Lesson 5 — the second subsystem: a controller and a worker that count
seconds, through the same platform code, with a different prefix. If this
works, the VMS is a subsystem and not the platform."""
from psimplatform.contract import Subsystem, Worker
from psimplatform.events import EventLog, read_bucket, subsystems_under
from psimplatform.spec import Refused, SpecController, SubsystemSpec
from tests.conftest import Box
import os

COUNTER = Subsystem("counter")

# The counter's controller is not written: it is this spec, run by the platform's SpecController —
# the same class the VMS runs from vms.subsystem.yaml, with a different prefix, id rule and fields.
COUNTER_SPEC = SubsystemSpec.from_dict({
    "name": "counter",
    "unit": {"rows": "units", "id": "name",                                   # named by the operator, not numbered
             "fields": {"name": {"type": "string", "required": True}, "step": {"type": "int", "default": 1}}},
    "placement": {"capacity": {"from": "capacity", "fallback": 10}},           # no constraint: any worker
    "snapshot": ["name", "step"],
})


class CounterController(SpecController):
    def __init__(self, box):
        super().__init__(COUNTER_SPEC, box.vars, box.objects, wall=box.wall)


class CounterWorker(Worker):
    def __init__(self, box, name):
        super().__init__(COUNTER, name, box.vars, box.objects, clock=box.clock, wall=box.wall)
        self.values: dict[str, int] = {}
        self.archive = box.archive                                       # this server's resource, shared with the VMS

    def reconcile_once(self, now=0.0):
        a = self.assignment()
        for unit in a.units:
            if unit not in self.epochs:
                self.take_epoch(unit)
            step = int(self.vars.get(COUNTER.config("units", unit))[0]["step"])
            self.values[unit] = self.values.get(unit, 0) + step
            if self.values[unit] % 10 == 0:                              # an observation, into the counter's own bucket
                EventLog(self.archive, COUNTER.name, unit, self.epochs[unit]).append(self.wall(), "round", value=self.values[unit])
        for unit in list(self.values):
            if unit not in a.units:
                del self.values[unit]; self.release(unit)
        self.heartbeat([{"id": u, "value": v, "phase": "counting"} for u, v in self.values.items()])
        return sorted(self.values)


def test_a_second_subsystem_through_the_same_platform():
    box = Box()
    ctl, w = CounterController(box), CounterWorker(box, "c-1")
    assert ctl.create({"name": "a", "step": 2})["id"] == "a" and ctl.create({"name": "b", "step": 5})["revision"] == 1
    for bad in ({"name": "c", "worker": "c-1"}, {"step": 3}, {"name": "a", "step": 9}):
        try:
            ctl.create(bad); raise AssertionError(bad)
        except Refused:
            pass                                                              # the platform's refusals, for free
    assert [u["id"] for u in ctl.units()] == ["a", "b"] and ctl.update("b", {"step": 5})["revision"] == 2
    w.heartbeat([], capacity=10)
    assert [p.worker for p in ctl.ensure_placed()] == ["c-1", "c-1"] and "on " in ctl.placement("a").reason   # placed like any unit
    assert ctl.snapshot()["units"][0] == {"id": "a", "name": "a", "step": 2, "revision": 1, "worker": "c-1", "server": "?"}
    assert w.reconcile_once() == ["a", "b"] and w.reconcile_once() == ["a", "b"]
    assert w.values == {"a": 4, "b": 10} and box.vars.get("counter/epoch/a")[0] == {"epoch": "1"}
    seen = ctl.workers_seen()
    assert seen["c-1"].status == [{"id": "a", "value": 4, "phase": "counting"}, {"id": "b", "value": 10, "phase": "counting"}]
    ctl.assign("c-1", ["b"])
    assert w.reconcile_once() == ["b"] and "a" not in w.epochs
    # its events sit on the same resource under its own prefix, written under its own epoch
    assert subsystems_under(box.archive) == {"counter": ["b"]}
    b = read_bucket(os.path.join(box.archive, "counter", "b", "e1", sorted(os.listdir(os.path.join(box.archive, "counter", "b", "e1")))[0]))
    assert b == [{"t": box.wall(), "kind": "round", "value": 10}]
    # the two subsystems do not see each other: prefixes, and nothing else
    assert box.vars.list("vms/") == [] and [p for p in box.vars.list("counter/") if "/placement/" not in p] == [
        "counter/epoch/a", "counter/epoch/b", "counter/units/a", "counter/units/b", "counter/workers/c-1"]


def test_the_second_subsystem_gets_a_console_for_free():
    """No console code for the counter either: SpecConsole over the same spec
    serves the page, /spec, /units, /where and the writes — with the counter's
    names, and no media because the counter registered none."""
    import json
    import urllib.error
    import urllib.request
    from psimplatform.console import SpecConsole
    box = Box(); ctl, w = CounterController(box), CounterWorker(box, "c-1")
    srv = SpecConsole(ctl).serve("127.0.0.1", 0); port = srv.server_address[1]
    def call(method, path, body=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r: return r.status, r.read().decode()
        except urllib.error.HTTPError as e: return e.code, e.read().decode()
    try:
        spec = json.loads(call("GET", "/spec")[1])
        assert spec["name"] == "counter" and spec["rows"] == "units" and spec["id"] == "name" and spec["media"] is False
        assert [f["name"] for f in spec["fields"]] == ["name", "step"] and spec["metrics"]["running"] == "units_running"
        assert call("POST", "/units", {"name": "a", "step": 2}, {"Idempotency-Key": "k1"})[0] == 201
        assert call("POST", "/units", {"step": 2}, {"Idempotency-Key": "k2"})[0] == 400                 # the spec's refusal, over HTTP
        assert call("POST", "/cameras", {"name": "x"}, {"Idempotency-Key": "k3"})[0] == 404             # not this subsystem's rows
        w.heartbeat([], capacity=10); ctl.ensure_placed(); w.reconcile_once()
        d = json.loads(call("GET", "/units")[1])
        assert d["configured"][0]["id"] == "a" and d["rows"][0]["worker"] == "c-1" and d["rows"][0]["phase"] == "counting"
        assert json.loads(call("GET", "/where/a")[1])["worker"] == "c-1"
        assert call("GET", "/timeline/a")[0] == 404 and call("GET", "/segment/x.mp4")[0] == 404         # no media was registered
        assert "counter_workers_live 1" in call("GET", "/metrics")[1] and "counter_units_running 0" in call("GET", "/metrics")[1]
        assert call("PUT", "/units/a", {"step": 3})[0] == 200 and ctl.unit("a")["step"] == 3
        assert call("DELETE", "/units/a")[0] == 200 and ctl.units() == []
        assert "<video" in call("GET", "/")[1]                                                          # one page; the spec hides it
    finally:
        srv.shutdown(); srv.server_close()
