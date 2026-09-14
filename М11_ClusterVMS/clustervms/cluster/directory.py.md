# directory.py — where is camera 7: the cluster directory as one scan of `vms/workers/*`

**Role in the module.** Lesson 5. The assignment rows `vms/workers/<worker>` are written by the controller into one raft and read by every worker from the same raft; scanning that prefix *is* the cluster directory, and it is consistent because it is one store. The docstring names the contrast with М12: a domain aggregates several clusters' snapshots and *cannot* be consistent, which is why "where is camera 7" is answered here, inside the cluster, and not above it. `SpecConsole.directory()`/`where()` in М10 does the same scan through the controller (`ctl.assignments()`); this class is the standalone form the lesson used, with an explicit TTL and a scan counter so a test can prove "nine answers, one scan". Depends only on `Variables.list/get` and the platform's `Assignment` record.

## `class Directory`
A cached view of every worker's assignment: `{worker: [unit ids as strings]}`. Holds the Variables handle, a TTL, a clock, the prefix, the cache, the time of the last scan and `scans` (how many scans were made — the test's proof).

### `__init__(self, vars_, ttl=5.0, clock=time.monotonic, prefix="vms/workers/")`
`vars_` is any `Variables` (the console's token is enough: read + list on `vms/*`). `ttl` is how long an answer may be served from the cache — 5 s, the controller's own pass period, so a directory is never staler than one placement pass. `clock` is injectable for tests. `_at = -1e9` forces the first call to scan.

### `scan(self, force=False) -> dict[str, list[str]]`
If `force` or the cache is older than `ttl`: lists `prefix`, and for each path reads the items and decodes them with `Assignment.from_items(worker, items).units`; stores the result, stamps the time and increments `scans`. Returns the cache. One `list` plus one `get` per worker — for a dozen workers a dozen raft reads every five seconds at most.

### `where(self, camera_id) -> str | None`
The workers whose units contain `str(camera_id)`. Exactly one → its name; none → `None`; more than one → the names joined with `+` in sorted order — the comment says why: during a reassignment window (the controller has written the destination and not yet removed the source) a camera legitimately shows as on both, and the directory reports that rather than picking one.

### `holdings(self, worker) -> list[int]`
The sorted integer ids of the units assigned to `worker` (`[]` for an unknown worker). Used by the test to check that the three workers' holdings partition cameras 1–9.

## Notes
- `test_where_is_camera_7_in_one_scan`: nine `where()` calls and three `holdings()` calls, `scans == 1`, and every answer agrees with `ctl.where()`.
- The class never reads heartbeats: an assignment to a dead worker is still the answer, because that is what the controller wrote; liveness is the console's `/cameras` rows (`worker_state`), not the directory's.
