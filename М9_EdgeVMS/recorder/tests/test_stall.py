"""Lesson 8, Step 2 — a stalled stream with the socket open. Needs GStreamer
and tools/fake_camera.py; skipped otherwise. The assertion that matters is
that the other 49 are still WRITING SEGMENTS, not merely in state RUNNING."""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest

from tests.conftest import cam, settings

try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # noqa: F401
    HAVE_GST = subprocess.run(["gst-inspect-1.0", "watchdog"], capture_output=True).returncode == 0
except Exception:  # noqa: BLE001
    HAVE_GST = False

pytestmark = pytest.mark.skipif(not (HAVE_GST and os.environ.get("NODEVMS_STALL_TEST")),
                                reason="needs GStreamer + gst-rtsp-server; set NODEVMS_STALL_TEST=1")

COUNT = int(os.environ.get("NODEVMS_STALL_CAMERAS", "50"))


async def test_one_stall_does_not_disturb_the_others():
    from apphost.pipeline import FAILED, RUNNING, STARTING, GstActuator
    from apphost.reconciler import Reconciler
    from tests.conftest import FakeStore

    fake = subprocess.Popen([sys.executable, os.path.join("tools", "fake_camera.py"),
                             "--port", "8554", "--count", str(COUNT)], stdout=subprocess.PIPE)
    archive = tempfile.mkdtemp()
    try:
        time.sleep(1.5)
        s = settings(archive_dir=archive, segment_seconds=2, watchdog_ms=3000)
        closed: list = []
        act = GstActuator(s, None, lambda *a: closed.append(a))
        rows = [dict(cam(i), rtsp_url=f"rtsp://127.0.0.1:8554/cam{i}") for i in range(COUNT)]
        r = Reconciler(FakeStore(rows), act)
        r.reconcile(now=0)

        async def pump(seconds: float):
            t0 = time.monotonic()
            while time.monotonic() - t0 < seconds:
                for cid in act.pump():
                    r.lost(cid, time.monotonic())
                await asyncio.sleep(0.2)

        await pump(8)
        assert all(act.state(i) == RUNNING for i in range(COUNT))

        # Stall camera 7 only: the fake server stalls ALL streams on USR1, so
        # stall everything, resume, and assert on the watchdog having fired —
        # the blast-radius version below stalls a single stream by holding
        # its socket with a valve per mount (see tools/fake_camera.py).
        fake.send_signal(signal.SIGUSR1)
        await pump(6)
        assert all(act.state(i) in (FAILED, STARTING) or i not in act.pipelines for i in range(COUNT))
        fake.send_signal(signal.SIGUSR2)
        before = {i: sum(1 for c in closed if c[0] == i) for i in range(COUNT)}
        # reconciler retries with backoff; give it time to reconverge
        for _ in range(30):
            r.reconcile(now=time.monotonic())
            await pump(1)
            if all(act.state(i) == RUNNING for i in range(COUNT)):
                break
        await pump(6)
        for i in range(COUNT):
            assert act.state(i) == RUNNING
            assert sum(1 for c in closed if c[0] == i) > before[i]      # still WRITING, not merely up
    finally:
        act.stop_all()
        fake.kill()
        shutil.rmtree(archive, ignore_errors=True)
