"""The scan worker — the fifth subsystem's worker, and the first whose work ENDS.

A unit is a job: one model over one interval of one recording. The worker does
not subscribe to anything. It plans the interval out of the recording's manifest
(`vms/scan.py`), decodes each stretch in turn, writes what the model saw into the
job's own bucket on the resource, and appends a line to the job's manifest for
every stretch it finishes:

    detjob/jobs/<job>          the job: cam, rec, kind, from, to, state — written by the console
    detjob/workers/<j>         the assignment, written by the detjob controller
    detjob/<j>/heartbeat       capacity, headroom, labels; per-job status: phase, done_through, events
    <archive>/detjob/<job>/e<epoch>/…events.jsonl   what the model saw
    <archive>/detjob/<job>/manifest.jsonl           how far it got — durable, so a restart resumes

Three things separate it from `DetWorker`, and all three come from the work
having both ends. It runs a BUDGET of stretches per pass rather than a frame:
a job is hours of video and a pass that finished it would starve the heartbeat.
Its events are stamped with MEDIA time, not the clock. And it is the only worker
that can say `done` — which nothing in the placement vocabulary reads yet, so the
row stays placed until the console's reaper moves its state (Lesson 21).

The MODEL is the same object as the live detector's, and that is the point: it
takes a timestamp and answers what it saw. Nothing in it knows whether the frame
behind that timestamp came off a fan-out or off a disk.
"""
from __future__ import annotations

import logging
import os
import time

from w2cplatform import runtime
from w2cplatform.contract import Worker
from w2cplatform.events import EventLog
from w2cplatform.variables import Variables

from .config import DETJOB_SPEC
from .detworker import FakeModel
from .scan import ScanLog, covered, plan, remaining

DETJOB = DETJOB_SPEC.sub
TERMINAL = ("done", "failed")
log = logging.getLogger("vms.detjobworker")


class DetJobWorker(Worker):
    """`name` is a slot (`j-1`); `models` maps a kind to a factory `(row) -> Model`,
    the same registry `DetWorker` uses."""

    # Stretches per reconcile pass. A scan is bounded work, and bounded work run to completion inside one
    # pass is a worker that stops heartbeating for an hour: its lease lapses, the controller calls it dead
    # and places the job somewhere else, and now two workers write the same job's events. The budget is
    # what keeps a long job and a live subsystem in the same process.
    STRETCHES_PER_PASS = 4
    STEP = 1.0                     # seconds of media between two looks; a real model decodes, this one counts

    def __init__(self, name: str | None, vars_: Variables, objects, models: dict | None = None,
                 capacity: int | None = None, clock=time.monotonic, wall=time.time, server: str | None = None,
                 archive_root: str | None = None, env: dict | None = None, step: float | None = None):
        env = dict(os.environ if env is None else env)
        super().__init__(DETJOB, None, vars_, objects, clock=clock, wall=wall)
        self.claim_slot(prefer=name if name is not None else runtime.slot(env, "DETJOB_NAME", "j"))
        self.models = models if models is not None else {"motion": FakeModel, "linecross": FakeModel, "lpr": FakeModel}
        self.capacity = capacity if capacity is not None else int(env.get("SCAN_CAPACITY", "2"))
        self.server = runtime.server(env, server)
        self.labels = runtime.labels(env, "gpu")
        self.archive_root = archive_root or env.get("ARCHIVE", "/data/archive")
        self.step = self.STEP if step is None else float(step)
        self.running: dict[str, object] = {}                # job -> model, kept between passes
        self.status_by_unit: dict[str, dict] = {}
        self.events_written = 0

    def job_row(self, job: str) -> dict | None:
        it, _ = self.vars.get(DETJOB.config("jobs", job))
        return DETJOB_SPEC.row(it) if it and it.get("deleted") != "true" else None

    # -- one stretch, decoded from the file's head and reported only inside the window ------------------
    #
    # `ts` starts at the SEGMENT's start and not the stretch's: a file opened at 10:00 has to be decoded
    # from 10:00 even when the operator asked from 10:05, because there is no other way into it. So the
    # model sees those five minutes, and `accepts` is what keeps what it saw there out of the answer.
    def _stretch(self, model, sc):
        ts = sc.seg.start
        while ts < sc.t1:
            for kind, fields in model.observe(ts):
                if sc.accepts(ts):
                    yield ts, kind, fields
            ts += self.step

    # -- the pass: advance every assigned job by at most a budget of stretches -------------------------
    def reconcile_once(self, now: float | None = None) -> list[str]:
        now = self.wall() if now is None else now
        wanted = set(self.assignment().units)
        for job in sorted(wanted):
            row = self.job_row(job)
            if row is None:
                continue
            if row["kind"] not in self.models:
                self.status_by_unit[job] = self._status(job, row, "unsupported")
                continue
            if row["state"] in TERMINAL:                    # the reaper has read this and not yet unplaced it
                self.status_by_unit[job] = self._status(job, row, row["state"])
                continue

            scans = plan(self.archive_root, row["rec"], row["from"], row["to"])
            log_ = ScanLog(self.archive_root, job)
            if not scans:
                # Not "no events": no FOOTAGE, here. The recording may live on another server — `near` is a
                # preference, so a job can be placed away from what it reads — and saying which of the two
                # it is, is the whole difference between "nothing happened" and "I could not look".
                self._stop(job)
                self.status_by_unit[job] = self._status(job, row, "waiting",
                                                        why="no footage for that interval on this server — the recording may be on another server")
                continue

            left = remaining(scans, log_)
            if not left:
                self._stop(job)
                self.status_by_unit[job] = self._status(job, row, "done", scans=scans, log=log_)
                continue

            if job not in self.epochs:
                self.take_epoch(job)                        # one writer of detjob/<job>/… at a time
            model = self.running.get(job)
            if model is None:
                model = self.running[job] = self.models[row["kind"]](row)
            if self.may_write(job):
                for sc in left[:self.STRETCHES_PER_PASS]:
                    n = 0
                    for ts, kind, fields in self._stretch(model, sc):
                        EventLog(self.archive_root, DETJOB.name, job, self.epochs[job]).append(
                            ts, kind, cam=int(row["cam"]), job=job, source="archive", **fields)
                        n += 1
                    log_.append(sc, n, self.wall())         # the line AFTER the events: a crash costs one re-scan
                    self.events_written += n
            self.status_by_unit[job] = self._status(job, row, "running", scans=scans, log=log_)

        for job in list(self.running):
            if job not in wanted:
                self._stop(job); self.release(job)
        for job in list(self.status_by_unit):
            if job not in wanted:
                self.status_by_unit.pop(job, None)
        self.renew_leases()
        return sorted(self.running)

    # What the console and the controller read. `done_through` and `covered` are here and not computed by
    # a reader, because the log they come from is a file on THIS server's disk and nobody else can see it.
    def _status(self, job: str, row: dict, phase: str, why: str = "", scans=None, log=None) -> dict:
        st = {"id": job, "cam": row["cam"], "rec": row["rec"], "kind": row["kind"], "phase": phase,
              "from": row["from"], "to": row["to"]}
        if why:
            st["why"] = why
        if log is not None:
            st["done_through"] = log.done_through()
            st["events"] = log.events()
        if scans is not None:
            st["covered"] = covered(scans)                  # seconds of the interval that footage exists for
            st["asked"] = max(0.0, row["to"] - row["from"])
        return st

    def _stop(self, job: str) -> None:
        m = self.running.pop(job, None)
        if m is not None:
            m.close()

    def headroom(self) -> int:
        return max(0, self.capacity - len(self.running))

    def heartbeat_once(self) -> None:
        self.heartbeat(list(self.status_by_unit.values()), server=self.server, instance=self.instance,
                       labels=",".join(self.labels), capacity=self.capacity, headroom=self.headroom(),
                       conflicts=self.conflicts(), events=self.events_written)

    def run(self, poll: float = 2.0, stop=None) -> None:
        import threading
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.reconcile_once(); self.heartbeat_once()
            except Exception:                               # noqa: BLE001
                log.exception("scan pass failed")
            stop.wait(poll)
        for job in list(self.running):
            self._stop(job)
        self.release_slot()
