"""Lesson 8, Step 1 — camera offline; then 200 at once."""
from __future__ import annotations

import random

from apphost.pipeline import FakeActuator
from apphost.reconciler import Reconciler
from tests.conftest import FakeStore, cam


def test_one_camera_offline_then_back():
    store = FakeStore([cam(1)])
    offline = {1}
    r = Reconciler(store, FakeActuator(failing=lambda cid: cid in offline))
    assert r.reconcile(now=0) == [("failed", 1)]
    t = r.failures[1]["retry_at"]
    assert r.reconcile(now=t - 0.01) == []              # still in backoff
    offline.clear()                                     # plugged back in
    assert r.reconcile(now=t) == [("start", 1)]
    assert r.failures == {}
    assert r.reconcile(now=t + 1) == []


def test_switch_reboot_does_not_stampede():
    """200 cameras fail simultaneously. Retries must not arrive together."""
    r = Reconciler(FakeStore([cam(i) for i in range(200)]),
                   actuator=FakeActuator(failing=lambda cid: True))
    r.reconcile(now=0)
    retries = sorted(f["retry_at"] for f in r.failures.values())
    spread = retries[-1] - retries[0]
    assert spread > 0.5, f"retries bunched within {spread:.3f}s"


def test_without_jitter_they_bunch(monkeypatch):
    """The control: with random() pinned, the spread is exactly 0.000s."""
    monkeypatch.setattr(random, "random", lambda: 1.0)
    r = Reconciler(FakeStore([cam(i) for i in range(200)]),
                   actuator=FakeActuator(failing=lambda cid: True))
    r.reconcile(now=0)
    retries = sorted(f["retry_at"] for f in r.failures.values())
    assert retries[-1] - retries[0] == 0.0


def test_offline_camera_does_not_block_the_others():
    store = FakeStore([cam(i) for i in range(1, 51)])
    r = Reconciler(store, FakeActuator(failing={7}))
    actions = r.reconcile(now=0)
    assert ("failed", 7) in actions
    assert sum(1 for v, _ in actions if v == "start") == 49
