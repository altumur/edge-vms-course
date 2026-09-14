"""Nomad Variables — the cluster's small, consistent store: М10's
`psimplatform.variables.Variables` contract, implemented by raft.

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
from typing import Protocol


from psimplatform.variables import Conflict, Forbidden   # noqa: E402  the platform's exceptions: one class, so a CAS retry catches ours too


class Variables(Protocol):
    def get(self, path: str) -> tuple[dict | None, int]: ...
    def put(self, path: str, items: dict, cas: int | None = None) -> int: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete(self, path: str, cas: int | None = None) -> None: ...


class NomadVariables:
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

    def get(self, path: str) -> tuple[dict | None, int]:
        status, body = self._req("GET", f"{self.addr}/v1/var/{path}?namespace={self.namespace}")
        if status == 404 or body is None:
            return None, 0
        return dict(body["Items"]), int(body["ModifyIndex"])

    def put(self, path: str, items: dict, cas: int | None = None) -> int:
        q = f"namespace={self.namespace}" + (f"&cas={cas}" if cas is not None else "")
        _, body = self._req("PUT", f"{self.addr}/v1/var/{path}?{q}", {"Items": {k: str(v) for k, v in items.items()}})
        return int(body["ModifyIndex"])

    def list(self, prefix: str) -> list[str]:
        status, body = self._req("GET", f"{self.addr}/v1/vars?prefix={prefix}&namespace={self.namespace}")
        return [v["Path"] for v in (body or [])]

    def delete(self, path: str, cas: int | None = None) -> None:
        q = f"namespace={self.namespace}" + (f"&cas={cas}" if cas is not None else "")
        self._req("DELETE", f"{self.addr}/v1/var/{path}?{q}")


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
        with self._lock:
            if path not in self._items:
                return None, 0
            items, idx = self._items[path]
            return dict(items), idx

    def put(self, path, items, cas=None):
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
        self._acl(path)
        with self._lock:
            _, current = self._items.get(path, (None, 0))
            if cas is not None and cas != current:
                raise Conflict(f"cas={cas} but ModifyIndex={current}")
            self._items.pop(path, None)
            self._raft_index += 1
