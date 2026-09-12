"""Lesson 8, Steps 4–5 — the AppHost dies mid-change; and the fencing rule
in its smallest form: on restart, never resume the previous segment."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

from apphost.pipeline import CameraPipeline, FakeActuator
from apphost.reconciler import Reconciler
from tests.conftest import FakeStore, cam, settings


def test_kill_mid_change_converges():
    store = FakeStore([cam(1), cam(2)])
    r1 = Reconciler(store, FakeActuator())
    r1.reconcile(now=0)
    store.rows.append(cam(9))
    # SIGKILL "mid-reconcile": nothing was written, r1 is simply gone.
    r2 = Reconciler(store, FakeActuator())               # systemd restarted it
    actions = r2.reconcile(now=0)
    assert set(actions) == {("start", 1), ("start", 2), ("start", 9)}
    assert r2.reconcile(now=1) == []                     # converged within one pass


def test_restart_opens_a_new_segment_never_resumes():
    """Two AppHost instances naming a segment file for the same camera must
    never pick the same path — the restarted one opens a NEW segment."""
    s = settings(archive_dir=tempfile.mkdtemp())
    p1 = CameraPipeline(cam(7), s, None, on_segment_closed=lambda *a: None)
    p2 = CameraPipeline(cam(7), s, None, on_segment_closed=lambda *a: None)
    path1 = p1._format_location(None, 0)
    import time; time.sleep(1.05)
    path2 = p2._format_location(None, 0)
    assert path1 != path2
    assert os.path.dirname(path1) == os.path.join(s.archive_dir, "7", "e1")   # epoch in the path
    assert path1.endswith("Z.mp4")


def test_segment_close_is_indexed_only_on_close():
    """Index on segment CLOSE, not open: a truncated open segment must never
    be offered as complete."""
    s = settings(archive_dir=tempfile.mkdtemp())
    rows = []
    p = CameraPipeline(cam(3), s, None, on_segment_closed=lambda *a: rows.append(a))
    path = p._format_location(None, 0)
    assert rows == []                                    # opened: nothing indexed
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * 4096)
    p._closed(path)
    assert len(rows) == 1
    cid, start, end, got_path, size = rows[0]
    assert (cid, got_path, size) == (3, path, 4096)
    assert start <= end and start.tzinfo is timezone.utc
    assert p.segments_written == 1
