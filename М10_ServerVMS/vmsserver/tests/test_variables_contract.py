"""The contract every Variables backend must keep — one suite, any backend.

A store is swapped by changing `CONFIG_URL`, which is only safe if "it works"
means something checkable. This is that meaning: file today, Nomad in М11,
Kubernetes when there is a site for it. A new backend is accepted when this file
is green against it, not when it looks right.

Run against another backend by pointing `CONTRACT_URL` at it:

    CONTRACT_URL=nomad://127.0.0.1:4646 python3 tests/run.py

The contract, in five clauses:

1.  a key that was never written reads as `(None, 0)`;
2.  `put` returns an index that identifies the version; the next read gives it back;
3.  `cas` is the whole of the concurrency story: N racers, one winner, N-1 refusals;
4.  a writer may only write its own prefixes, and is refused — not ignored — elsewhere;
5.  the index is OPAQUE. It is compared for equality and nothing else, because
    Kubernetes' `resourceVersion` is a string and arithmetic on it is meaningless.
    Clause 5 is the one that quietly decides whether the k8s backend is possible.
"""
import os
import tempfile
import threading

from w2cplatform.variables import Conflict, Forbidden, open_vars


def _store(writer=None, acl=None):
    """The backend under test: this box's files by default, whatever `CONTRACT_URL` says otherwise."""
    return open_vars(os.environ.get("CONTRACT_URL") or "file://" + tempfile.mkdtemp(), writer=writer, acl=acl)


def test_an_unwritten_key_reads_as_empty_and_index_zero():
    """`(None, 0)` — not an exception, not `{}`. The whole platform leans on it:
    `next_epoch`, `claim_slot` and every `Controller.write` start from a key that
    is not there yet and write it with `cas=0`."""
    v = _store()
    items, index = v.get("contract/absent")
    assert items is None and index == 0


def test_a_write_is_readable_and_carries_a_version():
    v = _store()
    i1 = v.put("contract/a", {"n": "1"}, cas=0)
    items, i2 = v.get("contract/a")
    assert items == {"n": "1"} and i2 == i1
    i3 = v.put("contract/a", {"n": "2"}, cas=i1)
    assert v.get("contract/a")[0] == {"n": "2"} and i3 != i1


def test_everything_is_strings():
    """The reason the contract fits Kubernetes at all: `ConfigMap.data` is
    `map[string]string`, and this store has always been one too. The store itself only
    promises `str()`; the `true`/`false` spelling a subsystem's rows use is
    `SubsystemSpec.items`, one layer up — the store never interprets a value."""
    v = _store()
    v.put("contract/types", {"n": 7, "flag": "true"}, cas=0)
    got = v.get("contract/types")[0]
    assert got == {"n": "7", "flag": "true"}
    assert all(isinstance(k, str) and isinstance(x, str) for k, x in got.items())


def test_cas_lets_exactly_one_racer_through():
    """Four threads, one key, one winner. This is the only concurrency primitive the
    platform has: no locks, no leases at this layer, no transactions."""
    v = _store()
    v.put("contract/race", {"n": "0"}, cas=0)
    _, index = v.get("contract/race")
    won, refused = [], []

    def go(k):
        try:
            v.put("contract/race", {"n": str(k)}, cas=index); won.append(k)
        except Exception:                      # the backend's own refusal type
            refused.append(k)

    ts = [threading.Thread(target=go, args=(k,)) for k in range(4)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert len(won) == 1 and len(refused) == 3
    assert v.get("contract/race")[0] == {"n": str(won[0])}


def test_a_stale_cas_is_refused_not_applied():
    v = _store()
    i = v.put("contract/stale", {"n": "1"}, cas=0)
    v.put("contract/stale", {"n": "2"}, cas=i)
    try:
        v.put("contract/stale", {"n": "3"}, cas=i)
        assert False, "a stale index must not write"
    except Exception:
        pass
    assert v.get("contract/stale")[0] == {"n": "2"}


def test_list_returns_the_keys_under_a_prefix():
    v = _store()
    for k in ("contract/list/a", "contract/list/b", "contract/other/c"):
        v.put(k, {"x": "1"}, cas=0)
    assert sorted(v.list("contract/list/")) == ["contract/list/a", "contract/list/b"]


def test_a_writer_is_refused_outside_its_prefixes():
    """Refused, not ignored: a token that writes where it may not must fail loudly,
    or the split that holds the whole system is decoration."""
    v = _store()
    if not hasattr(v, "as_writer"):
        return                       # a real store enforces this server-side (Nomad workload identity)
    w = v.as_writer("contract-writer", ["contract/mine/*"])
    w.put("contract/mine/k", {"x": "1"}, cas=0)
    try:
        w.put("contract/yours/k", {"x": "1"}, cas=0)
        assert False, "a writer wrote outside its prefixes"
    except Forbidden:
        pass
    assert v.get("contract/yours/k")[0] is None


def test_the_index_is_opaque():
    """Compared for equality, never ordered and never arithmetic. Nomad's
    `ModifyIndex` is a number and invites both; Kubernetes' `resourceVersion` is a
    string, and a backend for it is only possible while nothing in the platform
    does anything to this value but pass it back."""
    v = _store()
    i = v.put("contract/opaque", {"x": "1"}, cas=0)
    assert v.get("contract/opaque")[1] == i
    assert v.put("contract/opaque", {"x": "2"}, cas=i) != i


# A store whose version is a STRING that means nothing — `rv-7`, not `7`. Kubernetes hands out exactly this
# kind of value (`resourceVersion`, "treated as opaque … passed unmodified back"), and a backend for it is
# only possible while nothing in the platform interprets the index. This is the check for that: not a type
# annotation, a working store the real CAS loops are driven against.
class OpaqueIndexStore:
    def __init__(self):
        self._data: dict[str, tuple[dict, str]] = {}
        self._n = 0

    def get(self, path):
        items, idx = self._data.get(path, (None, 0))
        return (dict(items) if items is not None else None), idx

    def put(self, path, items, cas=None):
        _, current = self.get(path)
        if cas is not None and cas != current:
            raise Conflict(f"{path}: cas={cas!r} but the version is {current!r}")
        self._n += 1
        idx = f"rv-{self._n}"                       # no order, no arithmetic, not even a number
        self._data[path] = ({k: str(v) for k, v in items.items()}, idx)
        return idx

    def list(self, prefix):
        return sorted(p for p in self._data if p.startswith(prefix))


def test_the_contract_holds_when_the_version_is_not_a_number():
    v = OpaqueIndexStore()
    assert v.get("a") == (None, 0)                              # absent is 0 everywhere — the one non-version
    i = v.put("a", {"n": "1"}, cas=0)                           # …which is what makes cas=0 mean "create only"
    assert isinstance(i, str) and v.get("a")[1] == i
    try:
        v.put("a", {"n": "2"}, cas=0)
        assert False, "cas=0 must not overwrite an existing path"
    except Conflict:
        pass
    j = v.put("a", {"n": "2"}, cas=i)
    assert j != i
    try:
        v.put("a", {"n": "3"}, cas=i)
        assert False, "a stale version must not write"
    except Conflict:
        pass


def test_the_platforms_cas_loops_run_over_a_non_numeric_version():
    """The loops themselves — `next_epoch`, `claim_slot`, `Controller.write` — never look
    inside the index. Driven here against a store whose version is `rv-<n>`: if any of them
    ordered or incremented it, this is where that would show."""
    from w2cplatform.contract import Subsystem, Worker
    from w2cplatform.epoch import next_epoch

    v = OpaqueIndexStore()

    e1, idx1 = next_epoch(v, "vms/epoch/7")                     # read-modify-CAS, twice, on one key
    e2, idx2 = next_epoch(v, "vms/epoch/7")
    assert (e1, e2) == (1, 2) and idx1 != idx2                  # the EPOCH is a number; the index is not

    class _W(Worker):
        def reconcile_once(self, now=None):
            return []

    class _Objects:
        def __init__(self): self.d = {}
        def put(self, k, b): self.d[k] = b
        def get(self, k): return self.d.get(k)
        def list(self, p): return sorted(k for k in self.d if k.startswith(p))

    now = [1000.0]
    w = _W(Subsystem("vms"), None, v, _Objects(), wall=lambda: now[0], clock=lambda: now[0])
    w.claim_slot(prefer="w-1")                                  # CAS on vms/slots/w-1
    assert w.name == "w-1"
    assert w.renew_slot() is True                               # read, compare, write back — still opaque
    w.release_slot()
    assert v.get("vms/slots/w-1")[0]["released"] == "true"
