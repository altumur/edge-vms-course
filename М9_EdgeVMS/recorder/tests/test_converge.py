"""Lesson 6's seven — still passing, unchanged (Lesson 8, Step 6)."""
from __future__ import annotations

from apphost.pipeline import FakeActuator
from apphost.reconciler import CONVERGED, LAGGING, STALLED, Reconciler
from tests.conftest import FakeStore, cam


def test_1_converge_then_idle():
    store, act = FakeStore([cam(1), cam(2)]), FakeActuator()
    r = Reconciler(store, act)
    assert r.reconcile() == [("start", 1), ("start", 2)]
    assert r.reconcile() == []                        # a converged loop is silent
    assert r.reconcile() == []
    assert act.calls == [("start", 1), ("start", 2)]


def test_2_revision_bump_restarts():
    store, act = FakeStore([cam(1)]), FakeActuator()
    r = Reconciler(store, act)
    r.reconcile()
    store.rows[0]["revision"] = 2
    assert r.reconcile() == [("restart", 1)]
    assert r.reconcile() == []
    assert r.actual[1]["revision"] == 2


def test_3_disable_and_delete():
    store, act = FakeStore([cam(1), cam(2)]), FakeActuator()
    r = Reconciler(store, act)
    r.reconcile()
    store.rows[0]["enabled"] = False                  # disable
    assert r.reconcile() == [("stop", 1)]
    del store.rows[1]                                  # delete
    assert r.reconcile() == [("stop", 2)]
    assert r.actual == {}
    assert r.reconcile() == []


def test_4_restart_re_derives_actual():
    store = FakeStore([cam(1)])
    r1 = Reconciler(store, FakeActuator())
    r1.reconcile()
    r2 = Reconciler(store, FakeActuator())            # fresh process, empty actual
    assert r2.actual == {}
    assert r2.reconcile() == [("start", 1)]           # rebuilds from the store


def test_5_persisted_actual_is_a_cache_that_lies():
    """The mistake, made on purpose. Do not ship this class."""
    class Persisted(Reconciler):
        def __init__(self, *a, saved=None, **k):
            super().__init__(*a, **k)
            self.actual = saved or {}                 # loaded from disk on startup

    store, act = FakeStore([cam(1, revision=2)]), FakeActuator()
    liar = Persisted(store, act, saved={1: {"revision": 2}})
    assert liar.reconcile() == []                     # it does nothing
    assert liar.status()[1][0] == CONVERGED           # and reports success
    assert act.running == set()                       # and nothing is recording


def test_6_backoff_with_jitter_spreads_200_cameras():
    store = FakeStore([cam(i) for i in range(200)])
    r = Reconciler(store, FakeActuator(failing=lambda cid: True))
    r.reconcile(now=0)
    retries = sorted(f["retry_at"] for f in r.failures.values())
    spread = retries[-1] - retries[0]
    assert 1.0 <= retries[0] and retries[-1] <= 2.0   # base 2**1 = 2, jitter 50-100%
    assert spread > 0.5, f"retries bunched within {spread:.3f}s"
    # and nobody is retried before their retry_at
    assert r.reconcile(now=0.5) == []
    assert len(r.reconcile(now=2.0)) == 200
    # exponential: second failure's base is 4
    assert all(2.0 <= f["delay"] <= 4.0 for f in r.failures.values())


def test_7_lagging_vs_stalled():
    store = FakeStore([cam(1), cam(2)])
    r = Reconciler(store, FakeActuator(failing={2}), stall_failures=3)
    r.reconcile(now=0)
    assert r.status()[1] == (CONVERGED, 0)
    assert r.status()[2] == (LAGGING, 1)               # failed once: normal
    now = 0.0
    for _ in range(3):
        now = r.failures[2]["retry_at"] + 0.01
        r.reconcile(now=now)
    assert r.status()[2] == (STALLED, 1)               # failing repeatedly: not normal
    # a camera that DIED after running is forgotten, then restarted
    r.lost(1, now)
    assert 1 not in r.actual
    assert r.status()[1][0] == LAGGING


def test_max_backoff_caps_delay():
    store = FakeStore([cam(1)])
    r = Reconciler(store, FakeActuator(failing={1}), max_backoff=10)
    now = 0.0
    for _ in range(8):
        r.reconcile(now=now)
        now = r.failures[1]["retry_at"] + 0.01
    assert r.failures[1]["delay"] <= 10.0
