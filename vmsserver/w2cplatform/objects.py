"""An object store: large, or frequent, never queried by key. A directory
here; MinIO or S3 in М11. An object appears whole or not at all."""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # objects.py — the object store: a directory on one box, MinIO/S3 in М11
#
# **Role in the module.** Lesson 1's second store. Where `variables.py` holds small rows that must be
# consistent and CAS-able, the object store holds things that are large or written often and are never
# queried by key: worker heartbeats (`<sub>/<worker>/heartbeat`), resource heartbeats
# (`platform/resources/<server>/heartbeat`) and the controller's snapshot (`<sub>/snapshot`). Its one
# promise is that an object appears whole or not at all. The `ObjectStore` Protocol is the interface
# `Controller`, `Worker`, `Resource` and `SpecController` type against; `FsObjectStore` is the one-box
# implementation and М11 replaces it with MinIO behind the same three methods.
#
# ## Module-level names
# None beyond the two classes.
#
# ## Notes
# - The walk in `list` is O(files under root), fine for heartbeats on one box; М11's MinIO gives a real
#   prefix listing.
# - No delete: nothing in the platform removes an object. A stale heartbeat simply ages, and readers filter
#   by `ts`.
# ================================================================================================
from __future__ import annotations

import os
from typing import Protocol


# A `typing.Protocol` with `put(key, data: bytes)`, `get(key) -> bytes | None`, `list(prefix) -> list[str]`.
# No CAS, no index — objects are last-writer-wins by design; anything needing ordering goes in Variables.
class ObjectStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes | None: ...
    def list(self, prefix: str) -> list[str]: ...


# Implements `ObjectStore` over a directory tree; a key with `/` becomes nested directories.
class FsObjectStore:
    # Stores `root` and creates it.
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    # Path for a key. Refuses `..` or a leading `/` (raises `ValueError`) so a key cannot leave `root`.
    def _p(self, key: str) -> str:
        if ".." in key or key.startswith("/"):
            raise ValueError(key)
        return os.path.join(self.root, key)

    # Creates parent directories, writes to `<path>.tmp`, then `os.replace` onto the final path. That rename
    # is the "whole or not at all" guarantee: a reader (a controller reading a heartbeat while the worker
    # writes it) sees the old bytes or the new bytes, never a truncated file.
    def put(self, key: str, data: bytes) -> None:
        p = self._p(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "wb") as f:
            f.write(data)
        os.replace(p + ".tmp", p)

    # Reads the file; a missing key returns `None` rather than raising, so callers such as
    # `Controller.workers_seen` can simply skip it.
    def get(self, key: str) -> bytes | None:
        try:
            with open(self._p(key), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    # Walks the whole tree, skips `.tmp` files (an in-flight `put`), and returns the sorted keys (paths
    # relative to `root`) that start with `prefix`. `Controller.workers_seen` lists `<sub>/` and keeps keys
    # ending in `/heartbeat`; `resource.resources_seen` does the same under `platform/resources/`.
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
