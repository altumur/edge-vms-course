"""The AppHost's glue: three tasks per concern, controller-owned writes only,
segment rows queued from a streaming thread and written on the loop."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from apphost.apphost import AppHost
from apphost.pipeline import FakeActuator
from tests.conftest import cam, settings


class FakeAsyncStore:
    """The async surface of PgStore that AppHost touches."""

    def __init__(self, rows):
        self.rows = rows
        self.reports: list = []
        self.conditions: dict = {}
        self.segments: list = []
        self.events: list = []

    async def fetch_desired(self):
        return [dict(r) for r in self.rows]

    async def report(self, rows):
        self.reports.append(list(rows))

    async def set_condition(self, cid, condition, status, reason=None):
        self.conditions[(cid, condition)] = (status, reason)

    async def index_segment(self, *row):
        self.segments.append(row)

    async def log_event(self, kind, camera_id=None, payload=None):
        self.events.append((kind, camera_id, payload))


async def test_reconcile_and_report_write_only_controller_columns():
    store = FakeAsyncStore([cam(1), cam(2, enabled=False), cam(3)])
    host = AppHost(settings(), store, actuator=FakeActuator(failing={3}))
    actions = await host.reconcile_once()
    assert set(actions) == {("start", 1), ("failed", 3)}
    await host.report_once()
    rows = dict((cid, (rev, phase)) for cid, rev, phase in store.reports[-1])
    assert rows[1] == (1, "running")
    assert rows[2] == (1, "pending")            # disabled: applied, nothing to run
    assert rows[3] == (0, "failed")
    # reasons on their own axis, with a reason string
    assert store.conditions[(3, "camera_reachable")][0] is False
    assert "retry in" in store.conditions[(3, "camera_reachable")][1]
    assert store.conditions[(1, "camera_reachable")] == (True, None)
    assert store.conditions[(1, "storage_available")] == (True, None)


async def test_segment_closed_is_queued_then_indexed_on_the_loop():
    store = FakeAsyncStore([cam(1)])
    host = AppHost(settings(epoch=1), store, actuator=FakeActuator())
    await host.reconcile_once()
    t0 = datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 14, 9, 10, tzinfo=timezone.utc)
    host._on_segment_closed(1, t0, t1, "/data/archive/1/e1/x.mp4", 12345)   # "from a streaming thread"
    assert store.segments == []                 # nothing touched the database yet
    await host.report_once()
    assert store.segments == [(1, t0, t1, "/data/archive/1/e1/x.mp4", 12345, 1)]
    assert host.pending_index == []


async def test_storage_unavailable_is_a_reason_not_a_phase():
    store = FakeAsyncStore([cam(1)])
    act = FakeActuator()
    host = AppHost(settings(), store, actuator=act)
    host.recording_allowed = False               # the stop_recording policy fired
    assert await host.reconcile_once() == [("failed", 1)]
    assert act.running == set()
    await host.report_once()
    assert store.conditions[(1, "storage_available")][0] is False
    assert "disk full" in store.conditions[(1, "storage_available")][1]
    host.recording_allowed = True
    t = host.reconciler.failures[1]["retry_at"]
    host.started_at -= t + 1                     # advance the clock past the backoff
    assert await host.reconcile_once() == [("start", 1)]


async def test_dead_pipeline_is_forgotten_and_restarted():
    store = FakeAsyncStore([cam(1)])
    act = FakeActuator()
    host = AppHost(settings(), store, actuator=act)
    await host.reconcile_once()
    dead = [1]
    act.pump = lambda: dead.pop() and [1] if dead else []     # one bus error, once
    task = asyncio.create_task(host.pump_buses())
    await asyncio.sleep(0.05)
    host.stopping.set(); task.cancel()
    assert 1 not in host.reconciler.actual
    assert host.wake.is_set()
    host.started_at -= 10
    assert await host.reconcile_once() == [("start", 1)]
