# eventdatabase.py — a name for `w2cplatform.eventdatabase`: the event database each resource keeps over its own tree, and the console's merge

**Role in the module.** Lesson 3 (events). Three lines: the event database is the platform's and lives with the resource, so this module only re-exports `EventDatabase` and `MergedIndex` from `w2cplatform.eventdatabase` (`noqa: F401`) under the platform's name (М11's lessons once called it `eventindex`). `EventDatabase(root, server, wall, path=":memory:", bucket_seconds, interval)` is a SQLite cache over ONE resource's tree — its own buckets and the `.mirror/<server>/` copies it holds — rebuilt on start (`rebuild()`), tailed every few seconds (`tail()`, open buckets by the lines past what is held) and queried by time, camera, kind, subsystem and unit (`query(...)`); the resource job serves it as `GET /events`. `MergedIndex(objects, fetch, wall)` is what the console has instead of an index: `query(...)` asks every live resource's `/events`, merges by time, fences by the cluster's epochs, drops a peer's copy when the owner answered, and names the servers nobody answered for.

## Module-level names
- `EventDatabase`, `MergedIndex` — re-exports; no code of М11's own.

## Notes
- `__main__.resource` imports `EventDatabase` from `w2cplatform.eventdatabase` directly and starts it after `restore()` (`deploy/resource.nomad.hcl`); it is rebuilt on every start, which is what makes it a cache and not a store. `__main__.console` builds no index: `cluster.console.make_console` defaults `index` to the platform's `MergedIndex`.
- The tests (`tests/test_lesson3_events.py`, `test_the_console_over_http`) import from `w2cplatform.eventdatabase` and `cluster.console`, so nothing in the package depends on this alias.
