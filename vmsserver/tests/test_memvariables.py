"""The second backend, held to the first one's standard — by running the first
one's tests against it, not by writing new ones that agree with it."""
from __future__ import annotations

import inspect

from w2cplatform.variables import Conflict, Forbidden, open_vars  # noqa: F401
from tests import test_variables_contract as contract


def test_the_memory_backend_passes_the_whole_contract_suite():
    """A backend is accepted when the contract suite is green against it — Lesson 3's rule, executed.

    Not "a suite like it" and not "the parts that seemed relevant": these are the very functions that hold
    the file backend to the contract, called here with `_store` pointing at `memory://`. If the two ever
    disagree about what an absent key reads as, or which racer wins, or whether a refused write is refused
    rather than ignored, this fails — and it fails in the clause that disagrees, by name.

    A bare `memory://` is used on purpose: every clause starts from an empty store, and a bare URL has no
    name to share by, so each call gets its own."""
    cases = [(n, f) for n, f in vars(contract).items()
             if n.startswith("test_") and inspect.isfunction(f) and f.__module__ == contract.__name__]
    assert len(cases) >= 10, f"the contract suite shrank to {len(cases)}: this test is only as good as it"

    original = contract._store
    # `url=` is how clause 8 asks for a store with a ceiling; everything else gets the bare private store.
    contract._store = lambda writer=None, acl=None, url=None: open_vars(url or "memory://", writer=writer, acl=acl)
    try:
        for name, fn in sorted(cases):
            kwargs = {"monkeypatch": _NoPatch()} if "monkeypatch" in inspect.signature(fn).parameters else {}
            try:
                fn(**kwargs)
            except Exception as e:                        # noqa: BLE001
                raise AssertionError(f"memory:// fails the contract at {name}: {e!r}") from e
    finally:
        contract._store = original


class _NoPatch:
    """`tests/run.py` hands a monkeypatch to whoever asks; borrowing its shape is cheaper than importing it."""

    def __init__(self): self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()


def test_a_named_store_is_shared_and_a_bare_one_is_private():
    """A URL names a store, so two opens of one URL are one store — as they are for `file://<dir>`.

    This is what lets one process run three identities over one store: a dev binary with a controller, a
    console and a worker in it opens the same name three times. Sharing an address space is not sharing a
    writer, so the ACL still holds between them — which is the second half of this test and the half that
    would quietly stop being true if `as_writer` ever started copying the state instead of the handle."""
    ctl = open_vars("memory://shared-test", writer="ctl", acl={"ctl": ["vms/*"]})
    wrk = open_vars("memory://shared-test", writer="wrk", acl={"wrk": ["vms/epoch/*"]})
    ctl.put("vms/workers/w-1", {"units": "7"})
    items, _ = wrk.get("vms/workers/w-1")
    assert items == {"units": "7"}, "a second open of the same name is a different store"

    try:
        wrk.put("vms/workers/w-1", {"units": "8"})
        raise AssertionError("the worker wrote the controller's path")
    except Forbidden:
        pass

    # …and a bare memory:// names nothing, so there is nothing to share by.
    a, b = open_vars("memory://"), open_vars("memory://")
    a.put("k", {"x": "1"})
    assert b.get("k") == (None, 0), "two bare memory:// opens share a store"


def test_an_index_is_never_reused_inside_one_store():
    """A deleted path that comes back must not come back with an index somebody is still holding.

    Otherwise a CAS written before the delete matches after it, and a retry that should have conflicted
    lands on a row it has never seen. The file backend gets this from a counter it never rewinds; this one
    from the same counter in memory.

    Note what is NOT asserted: that the counter starts at 1000. It does, so that two backends read alike
    side by side — but a store that dies with its process has no "before the restart", so the starting
    value is cosmetic here and there is nothing honest to check. On the file backend it is load-bearing,
    and that is where the test for it lives."""
    v = open_vars("memory://")
    first = v.put("a", {"x": "1"})
    v.delete("a")
    assert v.put("a", {"x": "2"}) != first, "an index was reused after a delete"
    seen = {first}
    for i in range(20):
        idx = v.put(f"k{i}", {"x": str(i)})
        assert idx not in seen, "two writes got one index"
        seen.add(idx)
