"""The cluster's object store — М10's `w2cplatform.objects.ObjectStore`
contract, which holds three small things: worker heartbeats, resource
heartbeats, and the snapshot the domain's read model is built from.

On a cluster of this size the implementation is `VariablesObjectStore`:
objects as Nomad Variables under `objects/…`. A heartbeat is ~10 KB every
ten seconds from a dozen workers and three resources — a couple of raft
writes a second — which is not the volume the "keep raft small" rule was
about, and it removes a whole store (MinIO, its quorum, its credentials)
from the cluster. The contract is the point: `vms/` and `w2cplatform/` do
not know which one they are talking to. When a cluster grows to where its
heartbeats are a raft load, `open_store("s3+http://…")` is the same three
calls against MinIO or S3 (`s3.py`), and that is also the adapter a rented
cluster uses (М12 Lesson 8). Footage never goes to any of these.

Two adapters with one contract. `HttpObjectStore` PUTs and GETs against any
endpoint that accepts plain HTTP object semantics (MinIO with a bucket
policy, nginx with dav, an S3 presigned pattern behind a proxy).
`FsObjectStore` is a directory — the tests, and a bench with a shared mount.
An S3 adapter with signed requests is a twenty-line boto3 wrapper on the
same two methods; it is not here because the appliance image carries no boto3.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import Protocol

from w2cplatform.limits import check


class ObjectStore(Protocol):
    max_bytes: int                      # what one object may weigh here; 0 means no ceiling (М10A Lesson 26)

    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes | None: ...
    def list(self, prefix: str) -> list[str]: ...


class HttpObjectStore:
    max_bytes = 0                       # no ceiling worth naming
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def put(self, key: str, data: bytes) -> None:
        req = urllib.request.Request(f"{self.base}/{key}", data=data, method="PUT")
        req.add_header("Content-Type", "application/octet-stream")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            if r.status not in (200, 201, 204):
                raise IOError(f"PUT {key}: {r.status}")

    def get(self, key: str) -> bytes | None:
        try:
            with urllib.request.urlopen(f"{self.base}/{key}", timeout=self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def list(self, prefix: str) -> list[str]:
        raise NotImplementedError("plain HTTP has no listing; use s3+http:// for the heartbeat prefix")


class FsObjectStore:
    max_bytes = 0                       # no ceiling worth naming
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def put(self, key: str, data: bytes) -> None:
        p = os.path.join(self.root, key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "wb") as f:
            f.write(data)
        os.replace(p + ".tmp", p)                  # an object appears whole or not at all

    def get(self, key: str) -> bytes | None:
        p = os.path.join(self.root, key)
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            return f.read()

    def list(self, prefix: str) -> list[str]:
        out = []
        for d, _, files in os.walk(self.root):
            for f in files:
                if f.endswith(".tmp"):
                    continue
                key = os.path.relpath(os.path.join(d, f), self.root)
                if key.startswith(prefix):
                    out.append(key)
        return sorted(out)


class VariablesObjectStore:
    """Objects as Variables: `<prefix>/<key>` -> {data: <utf-8 text>}. Keys are
    the platform's (`vms/w-1/heartbeat`); the store is whatever Variables
    the caller holds, with the ACL that comes with its token."""

    def __init__(self, vars_, prefix: str = "objects"):
        self.vars, self.prefix = vars_, prefix.strip("/")
        # An object here IS a Variable, so it inherits the Variable's ceiling — and says so, rather than
        # letting a caller find it out in production. The `data` key and its value are what gets charged.
        self.max_bytes = max(0, getattr(vars_, "max_bytes", 0) - len("data"))

    def _path(self, key: str) -> str:
        if ".." in key or key.startswith("/"):
            raise ValueError(key)
        return f"{self.prefix}/{key}"

    def put(self, key: str, data: bytes) -> None:
        check(key, len(data), self.max_bytes)
        self.vars.put(self._path(key), {"data": data.decode("utf-8")})       # no cas: the last heartbeat wins, as it should

    def get(self, key: str) -> bytes | None:
        items, _ = self.vars.get(self._path(key))
        return items["data"].encode("utf-8") if items and "data" in items else None

    def list(self, prefix: str) -> list[str]:
        base = f"{self.prefix}/"
        return sorted(p[len(base):] for p in self.vars.list(base + prefix))

    def delete(self, key: str) -> None:
        self.vars.delete(self._path(key))


def open_store(url: str) -> ObjectStore:
    """file:///path · http(s)://host/bucket (anonymous) · s3+http(s)://host/bucket?region=r (SigV4,
    credentials from AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY — on a server, from a Variable)."""
    if url.startswith(("s3+http://", "s3+https://")):
        from urllib.parse import parse_qs, urlsplit
        from .s3 import S3ObjectStore
        u = urlsplit(url[3:])
        region = parse_qs(u.query).get("region", ["us-east-1"])[0]
        return S3ObjectStore(f"{u.scheme}://{u.netloc}", u.path.strip("/"), region)
    if url.startswith(("http://", "https://")):
        return HttpObjectStore(url)
    if url.startswith("file://"):
        return FsObjectStore(url[len("file://"):])
    if url.startswith("variables://"):
        from .variables import NomadVariables
        return VariablesObjectStore(NomadVariables(), url[len("variables://"):] or "objects")
    return FsObjectStore(url)
