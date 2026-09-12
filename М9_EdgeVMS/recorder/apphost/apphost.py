"""The AppHost: one process, three tasks per concern — not one per camera.

    reconcile()    every POLL_INTERVAL, and on NOTIFY   desired (Postgres) vs actual (dict)
    pump_buses()   every BUS_TICK                        non-blocking pop on each pipeline's bus
    report()       every REPORT_INTERVAL                 write observed_revision + conditions back
    retention()    every RETENTION_INTERVAL              partitions ahead, drop old, policy under pressure

The timer is correctness. The notification is latency.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from datetime import datetime, timezone

from .config import Settings
from .pipeline import FAILED, IDLE, RUNNING, STARTING, GstActuator
from .reconciler import CONVERGED, LAGGING, STALLED, Reconciler
from .retention import RealFs, enforce_retention
from .secrets import ColumnKey
from .store import Desired, PgStore

log = logging.getLogger("apphost")
MIGRATIONS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


class AppHost:
    def __init__(self, settings: Settings, store: PgStore, actuator=None, key: ColumnKey | None = None):
        self.settings = settings
        self.store = store
        self.key = key
        self.desired = Desired()
        self.actuator = actuator or GstActuator(settings, key, self._on_segment_closed)
        self.reconciler = Reconciler(self.desired, self._actuate,
                                     max_backoff=settings.max_backoff,
                                     stall_failures=settings.stall_failures)
        self.recording_allowed = True          # Lesson 8: the disk-full policy may clear this
        self.pending_index: list[tuple] = []   # segment rows, written by report()
        self.wake = asyncio.Event()
        self.stopping = asyncio.Event()
        self.started_at = time.monotonic()
        self.passes = 0

    # -- glue -------------------------------------------------------------
    def _actuate(self, verb: str, cam: dict) -> bool:
        # Storage unavailable is a REASON, not a phase: the camera stays
        # desired, converges as far as it can, and the condition explains why.
        if verb in ("start", "restart") and not self.recording_allowed:
            return False
        return self.actuator(verb, cam)

    def _on_segment_closed(self, cid: int, start: datetime, end: datetime, path: str, size: int) -> None:
        # Called from a streaming thread once per segment. Queue only; the
        # database write happens on the asyncio loop in report().
        self.pending_index.append((cid, start, end, path, size, self.settings.epoch))

    def now(self) -> float:
        return time.monotonic() - self.started_at

    # -- tasks ------------------------------------------------------------
    async def reconcile_once(self) -> list[tuple[str, int]]:
        self.desired.rows = await self.store.fetch_desired()
        actions = self.reconciler.reconcile(now=self.now())
        self.passes += 1
        for verb, cid in actions:
            log.info("reconcile: %s camera %s", verb, cid)
        return actions

    async def reconcile(self) -> None:
        while not self.stopping.is_set():
            try:
                await self.reconcile_once()
            except Exception:                              # noqa: BLE001
                log.exception("reconcile pass failed; will retry")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.settings.poll_interval)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def pump_buses(self) -> None:
        while not self.stopping.is_set():
            for cid in self.actuator.pump():
                # A pipeline died: forget it, count the failure, retry with backoff.
                self.reconciler.lost(cid, self.now())
                self.wake.set()
            await asyncio.sleep(self.settings.bus_tick)

    def phases(self) -> dict[int, tuple[int, str]]:
        """camera_id -> (observed_revision, phase). Phase is a POSITION."""
        out = {}
        for cam in self.desired.rows:
            cid = cam["id"]
            if not cam["enabled"]:
                out[cid] = (cam["revision"], "pending")     # applied: disabled means stopped
                continue
            have = self.reconciler.actual.get(cid, {}).get("revision", 0)
            st = self.actuator.state(cid) if hasattr(self.actuator, "state") else (
                RUNNING if cid in self.reconciler.actual else IDLE)
            phase = {RUNNING: "running", STARTING: "starting", FAILED: "failed"}.get(st, "pending")
            if cid in self.reconciler.failures and st in (IDLE, FAILED):
                phase = "failed"
            out[cid] = (have, phase)
        return out

    async def report_once(self) -> None:
        rows = [(cid, rev, phase) for cid, (rev, phase) in self.phases().items()]
        await self.store.report(rows)
        # Conditions: reasons, on their own axis.
        for cam in self.desired.rows:
            cid = cam["id"]
            f = self.reconciler.failures.get(cid)
            p = getattr(self.actuator, "pipelines", {}).get(cid)
            reason = (p.last_error if p and p.last_error else None) or (f and f"failing, retry in {f['delay']:.0f}s")
            await self.store.set_condition(cid, "camera_reachable", f is None, reason if f else None)
            await self.store.set_condition(cid, "storage_available", self.recording_allowed,
                                           None if self.recording_allowed else "disk full; policy=stop_recording")
            await self.store.set_condition(cid, "licensed", True, None)   # М14 wires this
        # The spool became an archive: index rows for closed segments.
        pending, self.pending_index = self.pending_index, []
        for cid, start, end, path, size, epoch in pending:
            await self.store.index_segment(cid, start, end, path, size, epoch)

    async def report(self) -> None:
        while not self.stopping.is_set():
            try:
                await self.report_once()
            except Exception:                              # noqa: BLE001
                log.exception("report failed; convergence continues regardless")
            await asyncio.sleep(self.settings.report_interval)

    async def retention(self) -> None:
        fs = RealFs()
        while not self.stopping.is_set():
            try:
                rep = await enforce_retention(self.store, fs, self.settings)
                if rep.recording_allowed != self.recording_allowed:
                    self.recording_allowed = rep.recording_allowed
                    log.warning("recording_allowed -> %s (policy %s)", rep.recording_allowed, rep.policy_applied)
                    self.wake.set()
                if rep.partitions_dropped or rep.policy_applied:
                    log.info("retention: dropped=%s policy=%s unlinked=%d",
                             rep.partitions_dropped, rep.policy_applied, rep.files_unlinked)
            except Exception:                              # noqa: BLE001
                log.exception("retention pass failed")
            await asyncio.sleep(self.settings.retention_interval)

    # -- lifecycle --------------------------------------------------------
    async def run(self, serve_console: bool = True) -> None:
        ok = await self.store.migrate(MIGRATIONS)
        if not ok:
            log.error("running on the previous schema; the box keeps recording")
        listener = None
        try:
            listener = await self.store.listen("cameras", lambda _payload: self.wake.set())
        except Exception:                                  # noqa: BLE001
            log.exception("LISTEN failed; polling alone (slower, still correct)")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stopping.set)

        tasks = [asyncio.create_task(self.reconcile(), name="reconcile"),
                 asyncio.create_task(self.pump_buses(), name="pump_buses"),
                 asyncio.create_task(self.report(), name="report"),
                 asyncio.create_task(self.retention(), name="retention")]
        if serve_console:
            from console.app import create_app
            import uvicorn
            cfg = uvicorn.Config(create_app(self), host=self.settings.console_host,
                                 port=self.settings.console_port, log_level="warning")
            server = uvicorn.Server(cfg)
            tasks.append(asyncio.create_task(server.serve(), name="console"))
        await self.stopping.wait()
        log.info("stopping: closing pipelines (each finalizes its open segment)")
        self.actuator.stop_all()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.report_once()
        finally:
            if listener is not None:
                await self.store.pool.release(listener)


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = Settings()
    key = ColumnKey.load(settings.column_key_file) if os.path.exists(settings.column_key_file) else None
    if key is None:
        log.warning("no column key at %s; cameras with credentials will not start", settings.column_key_file)
    store = await PgStore.connect(settings.database_url)
    try:
        await AppHost(settings, store, key=key).run()
    finally:
        await store.close()
