"""The config store in memory, with the same contract as the file one — and
therefore right exactly when the contract suite is green against it."""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # memvariables.py — `memory://`, the second backend, and what having two of them proves
#
# **Role in the module.** Lesson 3 said the store is a URL and nothing in the platform may know which
# backend answers it. That is a claim about design, and a design claim with one implementation is a hope.
# This is the second implementation: the same five clauses, in memory, in fifty lines.
#
# It is not a mock and it is not test scaffolding. It is registered on the same seam `file://` is, chosen
# the same way (`CONFIG_URL=memory://`), and held to the same standard — `test_variables_contract.py` runs
# against it unchanged:
#
#     CONTRACT_URL=memory:// python3 tests/run.py
#
# What it is FOR, in order of how often it matters: a dev box where nothing should survive a restart; a
# test that wants the real store and not a fake of it; and М11, where `FakeVariables` is this same thing
# with raft's answers — which is the point. A fake that implements a contract is a backend.
#
# ## Naming, and why a bare `memory://` is different
# A URL names a store, so two opens of one URL must be one store — as two opens of `file://<dir>` are.
# - `memory://<name>` — one store per name, per process. Three identities in one process (a controller, a
#   console and a worker in one dev binary) open the same name and see each other's writes: sharing an
#   address space is not sharing a writer, and the ACL still holds between them.
# - `memory://` — no name, so nothing to share by: a fresh private store on every open. That is what the
#   contract suite needs, since every clause starts from an empty store.
#
# The named stores live for the life of the process and are never collected. For a dev box and a test that
# is the whole of the requirement; anything that needs them collected wanted `file://`.
#
# ## What it deliberately copies from FileVariables
# Not the implementation — the BEHAVIOUR, down to the details a caller can see: an index is never reused,
# not even by a path that was deleted and came back; `put` checks the ACL and
# `delete` does not; `get` of an absent path is `(None, 0)`; a bad path is refused by `safe_path` before
# anything else happens. Where those differ, the contract suite is what says so — which is the entire
# argument for having written the suite before the second backend.
# ================================================================================================
from __future__ import annotations

import threading

from .variables import Conflict, Forbidden, register_scheme, safe_path

# One store per name, per process. Module-level because that is what "per process" means; the lock is
# around the registry, not around a store — each store has its own.
_NAMED: dict[str, "_MemState"] = {}
_NAMED_LOCK = threading.Lock()


class _MemState:
    """What the handles share. A handle is an identity over this."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.index = 1000           # matched to the file store so two backends read alike; a store that dies
                                    # with its process has no "before the restart", so the value is cosmetic here
        self.items: dict[str, tuple[dict, int]] = {}
        self.acl: dict[str, list[str]] = {}


class MemVariables:
    # `writer` is this handle's identity (None means unrestricted); `acl` is `{writer: [prefixes]}`. Both
    # mean exactly what they mean in `FileVariables`.
    def __init__(self, state: "_MemState | None" = None, writer: str | None = None,
                 acl: dict[str, list[str]] | None = None):
        self._s = state or _MemState()
        self.writer = writer
        if acl:
            with self._s.lock:
                self._s.acl.update(acl)

    # The same store seen through another identity, allowed only these prefixes.
    def as_writer(self, writer: str, allowed: list[str]) -> "MemVariables":
        with self._s.lock:
            self._s.acl[writer] = allowed
        return MemVariables(self._s, writer)

    @property
    def acl(self) -> dict[str, list[str]]:
        return self._s.acl

    def _refuse(self, path: str) -> None:
        if self.writer is None or not self._s.acl:
            return
        allowed = self._s.acl.get(self.writer, [])
        if not any(path == p or (p.endswith("*") and path.startswith(p[:-1])) for p in allowed):
            raise Forbidden(f"{self.writer} may not write {path}")

    # `(items, index)`, or `(None, 0)` for a path never written. The items are a COPY: a caller that
    # mutated what it read would be editing the store without an index, which is the one thing CAS exists
    # to prevent — and on the file backend it is impossible, so it must be impossible here.
    def get(self, path: str) -> tuple[dict | None, int]:
        safe_path(path)
        with self._s.lock:
            e = self._s.items.get(path)
            return (dict(e[0]), e[1]) if e else (None, 0)

    def put(self, path: str, items: dict, cas: int | None = None) -> int:
        safe_path(path)
        self._refuse(path)
        with self._s.lock:
            current = self._s.items.get(path, (None, 0))[1]
            if cas is not None and cas != current:
                raise Conflict(f"{path}: cas={cas} but ModifyIndex={current}")
            self._s.index += 1
            self._s.items[path] = ({k: str(v) for k, v in items.items()}, self._s.index)
            return self._s.index

    # The same CAS check as `put`, and — as on the file backend — no ACL check.
    def delete(self, path: str, cas: int | None = None) -> None:
        safe_path(path)
        with self._s.lock:
            current = self._s.items.get(path, (None, 0))[1]
            if cas is not None and cas != current:
                raise Conflict(f"{path}: cas={cas} but ModifyIndex={current}")
            self._s.items.pop(path, None)
            self._s.index += 1          # a delete burns an index too: the file backend's does

    def list(self, prefix: str) -> list[str]:
        with self._s.lock:
            return sorted(p for p in self._s.items if p.startswith(prefix))


# The seam takes a URL and an identity, never a class name. `memory://` is all a process says to keep
# nothing across a restart.
def _open(url: str, writer: str | None = None, acl: dict[str, list[str]] | None = None) -> MemVariables:
    name = url[len("memory://"):] if url.startswith("memory://") else ""
    if not name:
        return MemVariables(None, writer, acl)          # no name, no sharing: private to this open
    with _NAMED_LOCK:
        state = _NAMED.setdefault(name, _MemState())
    return MemVariables(state, writer, acl)


register_scheme("memory", _open)
