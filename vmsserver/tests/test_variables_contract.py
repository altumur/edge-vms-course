"""The contract every Variables backend must keep — one suite, any backend.

A store is swapped by changing `CONFIG_URL`, which is only safe if "it works"
means something checkable. This is that meaning: file today, Nomad in М11,
Kubernetes when there is a site for it. A new backend is accepted when this file
is green against it, not when it looks right.

Run against another backend by pointing `CONTRACT_URL` at it:

    CONTRACT_URL=nomad://127.0.0.1:4646 python3 tests/run.py

The contract, in seven clauses:

1.  a key that was never written reads as `(None, 0)`;
2.  `put` returns an index that identifies the version; the next read gives it back;
3.  `cas` is the whole of the concurrency story: N racers, one winner, N-1 refusals;
4.  a writer may only write its own prefixes, and is refused — not ignored — elsewhere;
5.  the index is OPAQUE. It is compared for equality and nothing else, because
    Kubernetes' `resourceVersion` is a string and arithmetic on it is meaningless.
    Clause 5 is the one that quietly decides whether the k8s backend is possible.
6.  a key has exactly ONE spelling: `..` and a leading `/` are REFUSED, not repaired.
7.  what a read hands back is a COPY. A caller that mutates it must not have edited
    the store — an edit without an index is the one thing CAS exists to prevent, and a
    backend that returns its own state gives it away for free.
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


def test_a_read_hands_back_a_copy():
    """Clause 7, and the one a backend gets for free only by accident.

    A file backend parses JSON on every read, so what a caller holds is already its own; a backend that
    keeps its state in memory — this process's `memory://`, a cache in front of raft, anything that does
    not re-parse — hands back the store itself unless it deliberately does not. Then a caller that edits
    the dict it read has written to the store **with no index**: no CAS, no conflict, no version, and the
    next reader sees a change nobody can point at.

    This clause exists because it was missing. `memory://` copied correctly from the first line, and
    removing the copy broke NOTHING in the suite — which meant the suite was not the contract it claimed
    to be. A clause nothing tests is a comment."""
    v = _store()
    v.put("contract/copy", {"n": "1"}, cas=0)

    items, idx = v.get("contract/copy")
    items["n"] = "999"
    items["sneaked"] = "yes"

    again, idx2 = v.get("contract/copy")
    assert again == {"n": "1"}, f"a read handed back the store's own row: {again}"
    assert idx2 == idx, "the store changed version without anyone writing to it"

    # and the same for what a caller PUT: keeping the dict it passed would be the same hole, from the
    # other side. `put` is given a row and must not go on sharing it.
    row = {"n": "2"}
    v.put("contract/copy", row, cas=idx)
    row["n"] = "666"
    assert v.get("contract/copy")[0] == {"n": "2"}, "put kept the caller's dict"


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


def test_a_key_has_one_spelling():
    """Refused, not repaired, and by every backend — because the same text becomes three
    things: the key, the prefix an ACL is matched against, and (through `events.unit_dir`)
    a directory on a resource's disk. Un-normalised, `vms/a/../b` and `vms/b` are two keys
    one person reads as one, each with its own index, so two writers both win their CAS.
    Normalised downstream — a URL, a tree — they collapse into one, and the ACL was matched
    against the string BEFORE that. A store that silently accepts either shape cannot keep
    one-writer-per-prefix, whatever its ACL says."""
    v = _store()
    for bad in ("vms/a/../b", "../etc/passwd", "/vms/a", ""):
        try:
            v.put(bad, {"x": "1"}, cas=0)
            assert False, f"a store accepted {bad!r} as a key"
        except ValueError:
            pass
        except Exception as e:                       # a remote store may refuse it its own way
            assert "not a key" in str(e) or "400" in str(e) or "404" in str(e), e
    v.put("vms/..foo", {"x": "1"}, cas=0)            # dots that are not a segment are just a name
    assert v.get("vms/..foo")[0] == {"x": "1"}


def test_two_spellings_are_never_one_place():
    """The same rule arriving through the encoding instead of through the path: a backend that
    maps `/` to `%2F` without escaping `%` first makes the key `a%2Fb` and the key `a/b` the
    same file — and `list` then reports one of them, so the collision is invisible."""
    v = _store()
    v.put("a/b", {"who": "slash"}, cas=0)
    v.put("a%2Fb", {"who": "percent"}, cas=0)
    assert v.get("a/b")[0] == {"who": "slash"}
    assert v.get("a%2Fb")[0] == {"who": "percent"}
    assert sorted(v.list("a")) == ["a%2Fb", "a/b"]


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
