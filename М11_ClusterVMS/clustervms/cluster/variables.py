"""Nomad Variables — the cluster's small, consistent store: М10's
`w2cplatform.variables.Variables` contract, implemented by raft.

`NomadVariables` speaks the HTTP API with the task's own workload-identity
token (NOMAD_TOKEN). `FakeVariables` is the same contract in memory, with
the semantics the docs promise: a raft-assigned ModifyIndex, PUT ?cas=<index>
succeeding only if the index still matches, 409 otherwise. Nothing in the
fake is Nomad; everything in it is what Nomad promises, and the tests run
against it in milliseconds.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from urllib.parse import quote
from typing import Protocol


from w2cplatform.variables import Conflict, Forbidden   # noqa: E402  the platform's exceptions: one class, so a CAS retry catches ours too
from w2cplatform.limits import check
from w2cplatform.variables import items_bytes, register_scheme, safe_path


class Variables(Protocol):
    def get(self, path: str) -> tuple[dict | None, int]: ...
    def put(self, path: str, items: dict, cas: int | None = None) -> int: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete(self, path: str, cas: int | None = None) -> None: ...


# `nomad://host:port` — the scheme this module answers for. Registered at the bottom of the file, so a
# process that imports `cluster` can say `CONFIG_URL=nomad://…` and the platform never names Nomad.
# `writer`/`acl` are ignored here: on a cluster the ACL is the task's own workload identity, enforced by
# the server, not by the client (which is the point of a real store).
def _open_nomad(url: str, writer: str | None = None, acl: dict | None = None) -> "NomadVariables":
    rest = url.split("://", 1)[1]
    return NomadVariables(addr=f"http://{rest}" if rest else None)


# What Nomad will hold in one Variable: every key and every value in it, together. The number is the
# scheduler's constant `maxVariableSize = 65536`, not ours — the request to make it configurable has been
# open since 2022, answered with "we don't want to give users a new way to break their clusters". This is
# the line that turns it from prose into something the platform can read (М10A Lesson 26).
NOMAD_MAX_VARIABLE_BYTES = 65536


class NomadVariables:
    max_bytes = NOMAD_MAX_VARIABLE_BYTES

    def __init__(self, addr: str | None = None, token: str | None = None, namespace: str = "default",
                 timeout: float = 5.0):
        self.addr = (addr or os.environ.get("NOMAD_ADDR", "http://127.0.0.1:4646")).rstrip("/")
        self.token = token or os.environ.get("NOMAD_TOKEN", "")
        self.namespace = namespace
        self.timeout = timeout

    def _req(self, method: str, url: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-Nomad-Token", self.token)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            if e.code == 409:
                raise Conflict(e.read().decode(errors="replace")[:200]) from None
            if e.code == 403:
                raise Forbidden(url) from None
            if e.code == 404:
                return 404, None
            raise

    # The key, checked and then percent-encoded for a URL. `safe_path` is the platform's rule and it
    # matters MORE here than on a box: this path goes into a URL, where a `..` meets HTTP normalisation —
    # ours, a proxy's, or the server's — and the ACL that let it through was matched against the string
    # BEFORE that. `quote` covers the rest: a space or a `#` in a unit's name would otherwise end the path.
    def _key(self, path: str) -> str:
        return quote(safe_path(path), safe="/")

    def get(self, path: str) -> tuple[dict | None, int]:
        status, body = self._req("GET", f"{self.addr}/v1/var/{self._key(path)}?namespace={self.namespace}")
        if status == 404 or body is None:
            return None, 0
        return dict(body["Items"]), int(body["ModifyIndex"])

    def put(self, path: str, items: dict, cas: int | None = None) -> int:
        # Refused here, by the number this store declares, rather than by a 400 from the server after the
        # round trip: the caller gets the size and the limit, and nothing was sent.
        check(path, items_bytes(items), self.max_bytes)
        q = f"namespace={self.namespace}" + (f"&cas={cas}" if cas is not None else "")
        _, body = self._req("PUT", f"{self.addr}/v1/var/{self._key(path)}?{q}", {"Items": {k: str(v) for k, v in items.items()}})
        return int(body["ModifyIndex"])

    def list(self, prefix: str) -> list[str]:
        status, body = self._req("GET", f"{self.addr}/v1/vars?prefix={quote(prefix, safe=chr(47))}&namespace={self.namespace}")
        return [v["Path"] for v in (body or [])]

    def delete(self, path: str, cas: int | None = None) -> None:
        q = f"namespace={self.namespace}" + (f"&cas={cas}" if cas is not None else "")
        self._req("DELETE", f"{self.addr}/v1/var/{self._key(path)}?{q}")


class FakeVariables:
    """One raft log for the whole cluster, in memory. Optional ACL: a writer
    id may only put under the prefixes it was granted."""

    def __init__(self):
        self._lock = threading.Lock()
        self._raft_index = 1000
        self._items: dict[str, tuple[dict, int]] = {}
        self.acl: dict[str, list[str]] = {}        # writer -> allowed prefixes
        self.writer: str | None = None            # "who am I" for the ACL check

    def as_writer(self, writer: str, allowed: list[str] | None = None) -> "FakeVariables":
        """The same raft seen through one identity — what a task's workload
        identity token is under a Nomad ACL policy."""
        v = FakeVariables.__new__(FakeVariables)
        v.__dict__ = self.__dict__.copy(); v.writer = writer
        if allowed is not None:
            self.acl[writer] = list(allowed)
        return v

    def _acl(self, path):
        if self.writer is not None and self.acl:
            allowed = self.acl.get(self.writer, [])
            if not any(path == p or (p.endswith("*") and path.startswith(p[:-1])) for p in allowed):
                raise Forbidden(f"{self.writer} may not write {path}")

    def get(self, path):
        safe_path(path)
        with self._lock:
            if path not in self._items:
                return None, 0
            items, idx = self._items[path]
            return dict(items), idx

    def put(self, path, items, cas=None):
        safe_path(path)          # the same key rule as the real store: the fake may not be laxer
        self._acl(path)
        with self._lock:
            _, current = self._items.get(path, (None, 0))
            if cas is not None and cas != current:
                raise Conflict(f"cas={cas} but ModifyIndex={current}")
            self._raft_index += 1
            self._items[path] = ({k: str(v) for k, v in items.items()}, self._raft_index)
            return self._raft_index

    def list(self, prefix):
        with self._lock:
            return sorted(p for p in self._items if p.startswith(prefix))

    def delete(self, path, cas=None):
        safe_path(path)
        self._acl(path)
        with self._lock:
            _, current = self._items.get(path, (None, 0))
            if cas is not None and cas != current:
                raise Conflict(f"cas={cas} but ModifyIndex={current}")
            self._items.pop(path, None)
            self._raft_index += 1


register_scheme("nomad", _open_nomad)
