"""The detector worker — the second subsystem's worker. A unit is one model on
one camera (`7-linecross`); the worker subscribes to the camera's RTSP fan-out
the way the gateway does (`live_url` from the VMS worker's heartbeat, never by
calling it), decodes, runs the model, and writes what the model saw into the
unit's bucket on the resource — `det/<unit>/e<epoch>/…events.jsonl`, under
the epoch it holds, so a stale instance's events are identifiable like a
stale writer's segments. Capacity is streams a GPU can carry; labels say
where it may run. The model is a `Model` — `FakeModel` here (fires on a
schedule, so the events and their buckets can be tested), a decode-and-infer
pipeline on a box.

    det/units/<name>        the unit: cam, kind, params, enabled — written by the console with the operator's token
    det/workers/<d>         the assignment, written by the det controller (SpecController from det.subsystem.yaml)
    det/<d>/heartbeat       capacity, headroom, labels, url-less; per-unit status: phase, events written
"""
from __future__ import annotations

import logging
import os
import time

from w2cplatform import runtime
from w2cplatform.console import holder_of
from w2cplatform.contract import Worker
from w2cplatform.events import EventLog
from w2cplatform.variables import Variables

from .config import DET_SPEC

DET = DET_SPEC.sub
log = logging.getLogger("vms.detector")


# A model: `observe(now) -> [ (kind, fields) ]` per pass; `close()`. The fake fires one event every `every`th
# pass, with the pass number, so a test can count buckets and lines.
class FakeModel:
    def __init__(self, unit: dict, every: int = 3):
        self.unit, self.every, self.passes = unit, every, 0

    def observe(self, now: float) -> list[tuple[str, dict]]:
        self.passes += 1
        return [(self.unit["kind"], {"pass": self.passes, "cam": int(self.unit["cam"])})] if self.passes % self.every == 0 else []

    def close(self) -> None:
        pass


class DetWorker(Worker):
    """`name` is a slot (`d-1`); `models` maps a kind to a factory `(unit) -> Model`; unknown kinds are
    reported as `phase: unsupported` and run nothing."""

    def __init__(self, name: str | None, vars_: Variables, objects, models: dict | None = None, capacity: int | None = None,
                 clock=time.monotonic, wall=time.time, server: str | None = None, archive_root: str | None = None,
                 env: dict | None = None):
        env = dict(os.environ if env is None else env)
        super().__init__(DET, None, vars_, objects, clock=clock, wall=wall)
        self.claim_slot(prefer=name if name is not None else runtime.slot(env, "DET_NAME", "d"))
        self.models = models if models is not None else {"motion": FakeModel, "linecross": FakeModel, "lpr": FakeModel}
        self.capacity = capacity if capacity is not None else int(env.get("CAPACITY", "8"))
        self.server = runtime.server(env, server)
        self.labels = runtime.labels(env, "gpu")
        self.archive_root = archive_root or env.get("ARCHIVE", "/data/archive")   # this server's resource: where the buckets go
        self.running: dict[str, object] = {}                                     # unit -> model
        self.status_by_unit: dict[str, dict] = {}
        self.events_written = 0

    # -- where the camera's RTP is: the VMS heartbeat, never a call to the worker ---------------------
    def rtp_source(self, cam: str):
        """`(server, live_url)` of the worker holding the camera — its RTSP fan-out, on any server."""
        found = holder_of(self.objects, "vms/", cam, self.wall(), phase="running", field="live_url")
        return None if found is None else (found[1].extra.get("server", "?"), found[2]["live_url"])

    def unit_row(self, unit: str) -> dict | None:
        it, _ = self.vars.get(DET.config("units", unit))
        return DET_SPEC.row(it) if it and it.get("deleted") != "true" else None

    # -- the reconcile pass: running models equal the assignment's enabled, reachable units ------------------
    def reconcile_once(self, now: float | None = None) -> list[str]:
        now = self.wall() if now is None else now
        wanted = set(self.assignment().units)
        for unit in wanted:
            row = self.unit_row(unit)
            if row is None:
                continue
            if not row["enabled"]:
                self._stop(unit); self.status_by_unit[unit] = {"id": unit, "cam": row["cam"], "kind": row["kind"], "phase": "pending"}; continue
            if row["kind"] not in self.models:
                self.status_by_unit[unit] = {"id": unit, "cam": row["cam"], "kind": row["kind"], "phase": "unsupported"}; continue
            src = self.rtp_source(row["cam"])
            if src is None:
                self._stop(unit); self.status_by_unit[unit] = {"id": unit, "cam": row["cam"], "kind": row["kind"], "phase": "waiting", "why": "camera held by nobody"}; continue
            if unit not in self.running:
                if unit not in self.epochs:
                    self.take_epoch(unit)                                       # one writer of det/<unit>/… at a time
                self.running[unit] = self.models[row["kind"]](row)
                self.status_by_unit[unit] = {"id": unit, "cam": row["cam"], "kind": row["kind"], "phase": "running", "events": 0, "server": src[0], "source": src[1]}
            if self.may_write(unit):
                for kind, fields in self.running[unit].observe(now):           # what the model saw, into the unit's bucket under its epoch
                    EventLog(self.archive_root, DET.name, unit, self.epochs[unit]).append(now, kind, **fields)
                    self.status_by_unit[unit]["events"] = self.status_by_unit[unit].get("events", 0) + 1; self.events_written += 1
        for unit in list(self.running):
            if unit not in wanted:
                self._stop(unit); self.status_by_unit.pop(unit, None); self.release(unit)
        for unit in list(self.status_by_unit):
            if unit not in wanted:
                self.status_by_unit.pop(unit, None)
        self.renew_leases()
        return sorted(self.running)

    def _stop(self, unit: str) -> None:
        m = self.running.pop(unit, None)
        if m is not None:
            m.close()

    def headroom(self) -> int:
        return max(0, self.capacity - len(self.running))

    def heartbeat_once(self) -> None:
        self.heartbeat(list(self.status_by_unit.values()), server=self.server, instance=self.instance, labels=",".join(self.labels),
                       capacity=self.capacity, headroom=self.headroom(), conflicts=self.conflicts(), events=self.events_written)

    def run(self, poll: float = 2.0, stop=None) -> None:
        import threading
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.reconcile_once(); self.heartbeat_once()
            except Exception:                                                # noqa: BLE001
                log.exception("detector pass failed")
            stop.wait(poll)
        for unit in list(self.running):
            self._stop(unit)
        self.release_slot()
