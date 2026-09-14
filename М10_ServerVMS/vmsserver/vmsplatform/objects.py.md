# objects.py — the object store: a directory on one box, MinIO/S3 in М11

**Role in the module.** Lesson 1's second store. Where `variables.py` holds small rows that must be consistent and CAS-able, the object store holds things that are large or written often and are never queried by key: worker heartbeats (`<sub>/<worker>/heartbeat`), resource heartbeats (`platform/resources/<server>/heartbeat`) and the controller's snapshot (`<sub>/snapshot`). Its one promise is that an object appears whole or not at all. The `ObjectStore` Protocol is the interface `Controller`, `Worker`, `Resource` and `SpecController` type against; `FsObjectStore` is the one-box implementation and М11 replaces it with MinIO behind the same three methods.

## Module-level names
None beyond the two classes.

## `class ObjectStore`
A `typing.Protocol` with `put(key, data: bytes)`, `get(key) -> bytes | None`, `list(prefix) -> list[str]`. No CAS, no index — objects are last-writer-wins by design; anything needing ordering goes in Variables.

## `class FsObjectStore`
Implements `ObjectStore` over a directory tree; a key with `/` becomes nested directories.

### `__init__(self, root)`
Stores `root` and creates it.

### `_p(self, key) -> str`
Path for a key. Refuses `..` or a leading `/` (raises `ValueError`) so a key cannot leave `root`.

### `put(self, key, data)`
Creates parent directories, writes to `<path>.tmp`, then `os.replace` onto the final path. That rename is the "whole or not at all" guarantee: a reader (a controller reading a heartbeat while the worker writes it) sees the old bytes or the new bytes, never a truncated file.

### `get(self, key) -> bytes | None`
Reads the file; a missing key returns `None` rather than raising, so callers such as `Controller.workers_seen` can simply skip it.

### `list(self, prefix) -> list[str]`
Walks the whole tree, skips `.tmp` files (an in-flight `put`), and returns the sorted keys (paths relative to `root`) that start with `prefix`. `Controller.workers_seen` lists `<sub>/` and keeps keys ending in `/heartbeat`; `resource.resources_seen` does the same under `platform/resources/`.

## Notes
- The walk in `list` is O(files under root), fine for heartbeats on one box; М11's MinIO gives a real prefix listing.
- No delete: nothing in the platform removes an object. A stale heartbeat simply ages, and readers filter by `ts`.
