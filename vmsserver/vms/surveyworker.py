"""The survey worker — the sixth subsystem's worker, and the only one whose input
is an archive we do not own.

It watches a camera's DEVICE archive — the card, the NVR — forever, and copies
nothing. The video stays where it is; what comes out is events:

    survey/watches/<name>        the unit: cam, kind, params — written by the console
    survey/workers/<s>           the assignment, written by the survey controller
    survey/<s>/heartbeat         capacity, headroom, labels; per-unit status: phase, lag, events
    <archive>/survey/<name>/e<epoch>/…events.jsonl   what the model saw
    <archive>/survey/<name>/frontier.json            how far it has watched

Three things shape it, and all three come from the archive being somebody else's.

The INDEX says where to look. A device recording on motion has mostly nothing
between its first and last minute, and walking that as if it were continuous
would spend the whole budget on silence.

The DOOR is the limit, not the GPU. A device allows two playback sessions and
they are shared with the operator watching and the recorder saving; when they
are gone this worker waits and says so, rather than retrying into a wall.

And the frontier MOVES ON. It is not a plan to finish but an edge to keep up
with — so what matters is not "did we cover everything" but how far behind the
device's newest minute we are, which is what `lag` in the heartbeat says.
"""
from __future__ import annotations

import logging
import os
import time
import urllib.error

from w2cplatform import runtime
from w2cplatform.console import holder_of
from w2cplatform.contract import Worker
from w2cplatform.events import EventLog
from w2cplatform.variables import Variables

from .config import SURVEY_SPEC
from .detworker import FakeModel
from .scan import SURVEY, Frontier, device_recordings, hit_spans

SURVEY_SUB = SURVEY_SPEC.sub
log = logging.getLogger("vms.surveyworker")


class SurveyWorker(Worker):
    """`name` is a slot (`s-1`); `models` maps a kind to a factory `(row) -> Model`,
    the same registry the live detector and the scan use."""

    # Seconds of MEDIA one pass may consume per watch. The same argument as the scan's budget of stretches
    # (Lesson 21): a pass that ran until it caught up is a worker that stops heartbeating while it does.
    SECONDS_PER_PASS = 600.0
    STEP = 1.0                     # seconds of media between two looks
    SETTLE = 30.0                  # never closer to the device's newest minute than this: it is still being written
    # How many kept stretches the heartbeat carries. The same window as the recorder's `closed` (Lesson 22)
    # and for the same reason: a heartbeat is one object under a ceiling, and the console's decision is
    # idempotent on its own because the request's id is the range.
    HITS_REPORTED = 32

    def __init__(self, name: str | None, vars_: Variables, objects, models: dict | None = None,
                 capacity: int | None = None, clock=time.monotonic, wall=time.time, server: str | None = None,
                 archive_root: str | None = None, env: dict | None = None, step: float | None = None,
                 fetch=None, index=None):
        env = dict(os.environ if env is None else env)
        super().__init__(SURVEY_SUB, None, vars_, objects, clock=clock, wall=wall)
        self.claim_slot(prefer=name if name is not None else runtime.slot(env, "SURVEY_NAME", "s"))
        self.models = models if models is not None else {"motion": FakeModel, "linecross": FakeModel, "lpr": FakeModel}
        self.capacity = capacity if capacity is not None else int(env.get("SURVEY_CAPACITY", "2"))
        self.server = runtime.server(env, server)
        self.labels = runtime.labels(env, "gpu")
        self.archive_root = archive_root or env.get("ARCHIVE", "/data/archive")
        self.step = self.STEP if step is None else float(step)
        self.fetch = fetch or _fetch_bytes           # the door: reading, and what costs a session
        self.index = index or device_recordings      # the listing: where the footage is, and it costs none
        self.running: dict[str, object] = {}
        self.status_by_unit: dict[str, dict] = {}
        self.events_written = 0
        self.hits: list[str] = []                    # `<cam>|<from>|<to>` a model liked — the console asks the recorder

    def watch_row(self, unit: str) -> dict | None:
        it, _ = self.vars.get(SURVEY_SUB.config("watches", unit))
        return SURVEY_SPEC.row(it) if it and it.get("deleted") != "true" else None

    # Where the device's footage is and where to read it: `(index_url, playback_url, oldest, newest)`, all
    # from the HOLDER's heartbeat, which is how everything here is found.
    def device(self, cam) -> tuple[str, str, float, float] | None:
        found = holder_of(self.objects, "vms/", str(cam), self.wall(), field="coverage")
        if found is None or not found[2].get("coverage") or not found[2].get("index_url"):
            return None
        cov = found[2]["coverage"]
        return found[2]["index_url"], found[2].get("playback_url", ""), float(cov["from"]), float(cov["to"])

    # -- the pass: advance every watch by at most a budget of media seconds ----------------------------
    def reconcile_once(self, now: float | None = None) -> list[str]:
        now = self.wall() if now is None else now
        wanted = set(self.assignment().units)
        for unit in sorted(wanted):
            row = self.watch_row(unit)
            if row is None:
                continue
            if not row["enabled"]:
                self._stop(unit); self.status_by_unit[unit] = self._status(unit, row, "pending"); continue
            if row["kind"] not in self.models:
                self.status_by_unit[unit] = self._status(unit, row, "unsupported"); continue

            dev = self.device(row["cam"])
            if dev is None:
                self._stop(unit)
                self.status_by_unit[unit] = self._status(unit, row, "waiting",
                                                         why="nobody holds this camera, or its device has no archive")
                continue
            index_url, play_url, oldest, newest = dev
            front = Frontier(self.archive_root, unit)
            at = front.read()
            if at is None:
                # The first time. `earliest` means thirty days of backlog on the day somebody enables it,
                # which is a decision the row makes and not a default this code picks.
                at = oldest if row["start"] == "earliest" else newest

            edge = newest - self.SETTLE          # the last minutes are being written; reading them gets a torn end
            if at >= edge:
                self.status_by_unit[unit] = self._status(unit, row, "running", front=front, newest=newest)
                continue

            want = (at, min(at + self.SECONDS_PER_PASS, edge))
            try:
                spans = self.index(index_url, want[0], want[1])
            except Exception:                            # noqa: BLE001 — the holder is there and not answering
                self.status_by_unit[unit] = self._status(unit, row, "waiting", why="the holder is not answering",
                                                         front=front, newest=newest)
                continue

            if unit not in self.epochs:
                self.take_epoch(unit)                    # one writer of survey/<unit>/… at a time
            model = self.running.get(unit)
            if model is None:
                model = self.running[unit] = self.models[row["kind"]](row)
            if not self.may_write(unit):
                continue

            busy, fired, watched = False, [], []
            for a, b in (spans if spans is not None else [want]):
                try:
                    self.fetch(play_url, a, b)           # the door: this is what costs a session
                except _Busy:
                    busy = True
                    break
                watched.append((a, b))
                for ts, kind, fields in self._watch(model, a, b):
                    EventLog(self.archive_root, SURVEY, unit, self.epochs[unit]).append(
                        ts, kind, cam=int(row["cam"]), watch=unit, source="device", **fields)
                    self.events_written += 1
                    fired.append(ts)
            # The frontier moves over the whole window, not from span to span. A gap in the device's own
            # recording is nothing to watch and nothing to come back for — leaving the frontier at its edge
            # would park the survey in front of every quiet night for ever. When the door closed half way, it
            # moves to the end of what WAS watched: those events are written, and watching them again would
            # write them twice.
            through = want[1] if not busy else (watched[-1][1] if watched else None)
            if through is not None:
                front.set(through)
                if row["keep"] == "hits" and fired:
                    # Watch everything, copy what a model liked. The stretches are reported, not written: a
                    # worker's token writes no configuration, and the row that asks the recorder for a range
                    # is configuration (`rec/requests/<id>` — Lesson 21). The console turns these into requests.
                    for a, b in hit_spans(fired, watched, row["pre"], row["post"], row["join"]):
                        self.hits = (self.hits + [f"{row['cam']}|{a:.0f}|{b:.0f}"])[-self.HITS_REPORTED:]
            if busy:
                # Not a failure and not a retry: the two sessions belong to the operator watching this gap and
                # to the recorder saving it, and a survey is the one of the three that can wait.
                self.status_by_unit[unit] = self._status(unit, row, "waiting", why="the device has no free session",
                                                         front=front, newest=newest)
                continue
            self.status_by_unit[unit] = self._status(unit, row, "running", front=front, newest=newest)

        for unit in list(self.running):
            if unit not in wanted:
                self._stop(unit); self.release(unit)
        for unit in list(self.status_by_unit):
            if unit not in wanted:
                self.status_by_unit.pop(unit, None)
        self.renew_leases()
        return sorted(self.running)

    def _watch(self, model, a: float, b: float):
        ts = a
        while ts < b:
            for kind, fields in model.observe(ts):
                yield ts, kind, fields
            ts += self.step

    # `lag` is the answer this subsystem exists to give about itself: how far behind the device's newest
    # minute we are. A survey that cannot keep up is not broken and not finished — it is behind, and the
    # only way anyone finds out is if it says so.
    def _status(self, unit: str, row: dict, phase: str, why: str = "", front=None, newest: float | None = None) -> dict:
        st = {"id": unit, "cam": row["cam"], "kind": row["kind"], "phase": phase}
        if why:
            st["why"] = why
        if front is not None:
            at = front.read()
            st["watched_through"] = at if at is not None else 0.0
            if newest is not None and at is not None:
                st["lag"] = max(0.0, newest - at)
        st["events"] = self.events_written
        return st

    def _stop(self, unit: str) -> None:
        m = self.running.pop(unit, None)
        if m is not None:
            m.close()

    def headroom(self) -> int:
        return max(0, self.capacity - len(self.running))

    def heartbeat_once(self) -> None:
        self.heartbeat(list(self.status_by_unit.values()), server=self.server, instance=self.instance,
                       labels=",".join(self.labels), capacity=self.capacity, headroom=self.headroom(),
                       conflicts=self.conflicts(), events=self.events_written, hits=",".join(self.hits))

    def run(self, poll: float = 2.0, stop=None) -> None:
        import threading
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.reconcile_once(); self.heartbeat_once()
            except Exception:                            # noqa: BLE001
                log.exception("survey pass failed")
            stop.wait(poll)
        for unit in list(self.running):
            self._stop(unit)
        self.release_slot()


class _Busy(Exception):
    """The device's two playback sessions are both in use."""


# One read through the holder's door. The bytes are not kept: this subsystem copies nothing, and what it
# took the session for is the decoding, not the file.
def _fetch_bytes(url: str, t0: float, t1: float, timeout: float = 30.0) -> bytes:
    import urllib.request
    if not url:
        raise _Busy("no playback url")
    try:
        with urllib.request.urlopen(f"{url}?from={t0}&to={t1}", timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 503:
            raise _Busy(str(e)) from e                   # the device's ceiling, not ours
        raise
