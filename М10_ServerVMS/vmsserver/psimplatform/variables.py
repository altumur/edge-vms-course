"""A config store with the semantics Nomad Variables promise — a
raft-assigned ModifyIndex, PUT with cas=<index> succeeding only if the index
still matches, a conflict otherwise — on one box, as files.

One JSON file per path under <root>/vars/, one counter file for the index,
one lock. Every write is atomic (write-then-rename) and serialised by the
lock, so two processes on the same box see exactly what two clients of one
raft would: one of them wins the CAS.
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # variables.py — a file-backed config store with the semantics of Nomad Variables: ModifyIndex,
# check-and-set, one writer per prefix
#
# **Role in the module.** Lesson 1's config store. It gives one box exactly what a raft-backed Nomad
# Variables store gives a cluster: every path has a `ModifyIndex`, a `put(cas=<index>)` succeeds only if the
# index still matches and raises `Conflict` otherwise, and a writer identity may be confined to a set of
# prefixes (the ACL policy). Everything in the platform that must be consistent — assignments, placement
# rows, epochs, slots, idempotency keys, unit rows — lives here; bulk or frequent data (heartbeats,
# snapshots, media) goes to `objects.py` instead. The store is a directory: one JSON file per path under
# `<root>/vars/`, one counter file for the index, one lock file. Every write is write-then-rename and
# serialised by an `fcntl` lock, so two processes on the same box see exactly what two clients of one raft
# would: one of them wins the CAS. М11 swaps this class for real Nomad Variables behind the same `Variables`
# Protocol.
#
# ## Module-level names
# - `Conflict` — exception: the `cas` index passed to `put`/`delete` did not equal the path's current
#   ModifyIndex. Every CAS loop in the platform (`epoch.next_epoch`, `Controller.write`,
#   `Worker.claim_slot`, `IdempotencyKeys.claim`) catches it and re-reads.
# - `Forbidden` — exception: this writer identity is not allowed to write that path. Raised by `put` when an
#   ACL is set; it is how "one writer per prefix" is enforced mechanically rather than by convention. The
#   console test proves a console token cannot write placement by expecting exactly this.
# - `Variables` — `typing.Protocol` with `get`, `put`, `list`: the interface every consumer types against.
#   `FileVariables` implements it here; М11's Nomad client will too. (Note `delete` is not in the Protocol
#   even though `FileVariables` has it; `IdempotencyKeys.prune` uses it.)
#
# ## Notes
# - Atomicity model: rename is atomic on one filesystem; the lock serialises read-check-write; therefore
#   `put(cas=idx)` is a true compare-and-set. `test_two_processes_one_cas_winner` runs four threads, each
#   with its own `FileVariables`, incrementing a counter by CAS and asserts the final value equals the
#   number of successful writes.
# - `test_the_config_store_survives_a_restart_and_refuses_a_stale_cas` shows a second handle on the same
#   directory reads the same items and index, a stale `cas` raises `Conflict`, and `get("nope")` is `(None,
#   0)`.
# - The ACL check is on the handle, not the file: an unrestricted handle over the same directory can still
#   write anything. The confinement is real only when each process gets only its own token — which is what
#   the Quadlet units under `deploy/` arrange.
# ================================================================================================
from __future__ import annotations

import fcntl
import json
import os
from typing import Protocol


class Conflict(Exception):
    """The cas index did not match the current ModifyIndex."""


class Forbidden(Exception):
    """This writer may not write that path — one writer per prefix."""


# The version of a path, as the store hands it out. OPAQUE: the platform compares it for equality, passes
# it back unmodified, and does nothing else with it — never orders it, never does arithmetic on it. That is
# not fastidiousness, it is what a backend needs. Nomad's `ModifyIndex` is the raft index and is a number;
# Kubernetes' `resourceVersion` is a string, and its API conventions require it: "This value MUST be treated
# as opaque by clients and passed unmodified back to the server" — it is "currently backed by etcd's
# mod_revision", but an application "should *not* rely on the implementation details of the versioning
# system". Typing this `int` would have made a Kubernetes backend bet on that detail.
#
# One value is NOT a version and is the same everywhere: `0` means the path does not exist. `get` returns it
# for a missing path, and `put(cas=0)` therefore means "create only" — the first claimant wins and everyone
# else conflicts (`SpecController.create`, `IdempotencyKeys.claim`).
Index = str | int


class Variables(Protocol):
    def get(self, path: str) -> tuple[dict | None, Index]: ...
    def put(self, path: str, items: dict, cas: Index | None = None) -> Index: ...
    def list(self, prefix: str) -> list[str]: ...


# Refuses a path containing `..` or starting with `/` (raises `ValueError`) and returns it unchanged. Called
# on every path before it is turned into a filename, so a caller cannot escape `<root>/vars/`.
def _safe(path: str) -> str:
    if ".." in path or path.startswith("/"):
        raise ValueError(path)
    return path


# The store. Holds only paths (`root`, `dir`, `index_file`, `lock_file`) plus an optional writer identity
# and ACL map; it keeps no cache, so any number of instances over the same directory — in one process or
# many — are equivalent.
# -- the seam: which store is behind the contract, said as a URL ------------------------------------
# A process is told `CONFIG_URL` and nothing else. `file://` is in-process — on a box there is no daemon,
# no hop and no second quorum, which is the whole reason this is a factory and not a service. Every other
# scheme is registered by the package that implements it (`cluster/variables.py` registers `nomad://` at
# import), so the platform names no vendor and adding Kubernetes later adds a file, not a branch here.
#
# This is the same seam Objects already have in М11 (`open_store(OBJECTS)`); Config just never got it, and
# that is why swapping the store meant editing sixteen constructors in three modules and two languages.
_SCHEMES: dict[str, object] = {}


def register_scheme(scheme: str, factory) -> None:
    """`factory(url, writer=…, acl=…) -> Variables`. A backend registers itself at import."""
    _SCHEMES[scheme] = factory


def open_vars(url: str, writer: str | None = None, acl: dict[str, list[str]] | None = None):
    """`file:///data/platform/config` · `nomad://127.0.0.1:4646` · whatever else registered.

    A bare path is read as `file://` so the box keeps working with no URL at all."""
    if "://" not in url:
        return FileVariables(url, writer, acl)
    scheme, rest = url.split("://", 1)
    if scheme == "file":
        return FileVariables(rest or "/", writer, acl)
    factory = _SCHEMES.get(scheme)
    if factory is None:
        known = ", ".join(sorted(["file"] + list(_SCHEMES)))
        raise ValueError(f"no Variables backend for {scheme}://  (have: {known})")
    return factory(url, writer=writer, acl=acl)


class FileVariables:
    # `root` is the store directory; `<root>/vars/` is created. `writer` is this handle's identity (None
    # means unrestricted). `acl` is `{writer: [allowed prefixes]}`; when both `writer` and a non-empty `acl`
    # are present, `put` checks them. Nothing is read at construction; the index counter file is created
    # lazily on the first write.
    def __init__(self, root: str, writer: str | None = None, acl: dict[str, list[str]] | None = None):
        self.root = root
        self.dir = os.path.join(root, "vars")
        os.makedirs(self.dir, exist_ok=True)
        self.index_file = os.path.join(root, "index")
        self.lock_file = os.path.join(root, "lock")
        self.writer, self.acl = writer, acl or {}

    # Returns a new handle on the same directory seen through another identity, allowed only the given
    # prefixes (`'vms/*'`, `'vms/epoch/*'` style: a trailing `*` means prefix match, otherwise exact path).
    # This is what a Nomad ACL policy does for a task's token. The tests build the controller with
    # `as_writer("vmscontroller", SPEC.acl_controller())` and the console with `as_writer("vmsconsole",
    # SPEC.acl_console())`.
    def as_writer(self, writer: str, allowed: list[str]) -> "FileVariables":
        """The same store seen through another identity, allowed only these
        prefixes ('vms/*', 'vms/epoch/*') — what a Nomad ACL policy does."""
        v = FileVariables(self.root, writer, dict(self.acl))
        v.acl[writer] = allowed
        return v

    # Maps a variable path to its file: `<root>/vars/<path with / encoded as %2F>.json`. One flat directory,
    # so `list` is a single `listdir`.
    def _file(self, path: str) -> str:
        return os.path.join(self.dir, _safe(path).replace("/", "%2F") + ".json")

    # Opens the lock file and takes an exclusive `fcntl.flock` on it; the returned file object is used as a
    # context manager, and closing it releases the lock. This serialises every `put`/`delete` across all
    # processes on the box.
    def _locked(self):
        f = open(self.lock_file, "a+")
        fcntl.flock(f, fcntl.LOCK_EX)
        return f

    # Reads the counter file (missing or empty means 1000), increments it, writes it back atomically (tmp +
    # `os.replace`) and returns the new value. Always called under the lock. The starting value 1000 is why
    # the very first write in the tests returns index 1001 — indices never restart from zero after a
    # restart, so a stale CAS from before a restart still conflicts.
    def _next_index(self) -> int:
        try:
            n = int(open(self.index_file).read() or 1000)
        except FileNotFoundError:
            n = 1000
        n += 1
        tmp = self.index_file + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(n))
        os.replace(tmp, self.index_file)
        return n

    # Reads the path's file and returns `(items, index)`; a missing path is `(None, 0)`. Not locked: a
    # reader sees either the old file or the new one, never a half-written one, because writes rename into
    # place. Items come back as a fresh dict of strings.
    def get(self, path: str) -> tuple[dict | None, int]:
        try:
            with open(self._file(path)) as f:
                d = json.load(f)
        except FileNotFoundError:
            return None, 0
        return dict(d["items"]), int(d["index"])

    # The write. First the ACL: if this handle has a writer and an ACL, the path must match one of the
    # writer's allowed patterns or `Forbidden` is raised — before taking the lock. Then, under the lock:
    # read the current index; if `cas` was given and differs, raise `Conflict`; otherwise take the next
    # index, dump `{"items": {k: str(v)}, "index": idx}` to a tmp file and rename it over the path's file;
    # return the new index. Two rules to remember: `cas=0` means "create only" (the path must not exist —
    # `SpecController.create` and `IdempotencyKeys.claim` rely on it); and every value is stringified on the
    # way in, which is why callers such as `Assignment.from_items` and `Slot.from_items` parse ints, floats
    # and `"true"`/`"false"` back out.
    def put(self, path: str, items: dict, cas: int | None = None) -> int:
        if self.writer is not None and self.acl:
            allowed = self.acl.get(self.writer, [])
            if not any(path == p or (p.endswith("*") and path.startswith(p[:-1])) for p in allowed):
                raise Forbidden(f"{self.writer} may not write {path}")
        with self._locked():
            _, current = self.get(path)
            if cas is not None and cas != current:
                raise Conflict(f"{path}: cas={cas} but ModifyIndex={current}")
            idx = self._next_index()
            tmp = self._file(path) + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"items": {k: str(v) for k, v in items.items()}, "index": idx}, f)
            os.replace(tmp, self._file(path))
            return idx

    # Under the lock: the same CAS check as `put`, then remove the file (a missing file is not an error) and
    # burn an index. Used by `IdempotencyKeys.prune`. No ACL check is applied here.
    def delete(self, path: str, cas: int | None = None) -> None:
        with self._locked():
            _, current = self.get(path)
            if cas is not None and cas != current:
                raise Conflict(f"{path}: cas={cas} but ModifyIndex={current}")
            try:
                os.remove(self._file(path))
            except FileNotFoundError:
                pass
            self._next_index()

    # Every stored path that starts with `prefix`, decoded from the filenames and sorted. Callers pass
    # prefixes ending in `/` (`"vms/workers/"`, `"vms/slots/"`, `"vms/idem/"`) to enumerate a row family.
    def list(self, prefix: str) -> list[str]:
        out = []
        for f in os.listdir(self.dir):
            if f.endswith(".json"):
                p = f[:-5].replace("%2F", "/")
                if p.startswith(prefix):
                    out.append(p)
        return sorted(out)
