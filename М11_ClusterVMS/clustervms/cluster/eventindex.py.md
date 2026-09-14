# eventindex.py — a name for `vmsplatform.eventindex`: the platform's index over every subsystem's buckets on every resource

**Role in the module.** Lesson 3 (events). Two lines: the event index is a platform job, not a VMS one, so this module only re-exports `EventIndex` and `ResourceReader` from `vmsplatform.eventindex` (`noqa: F401`) under the name М11's lessons used. See `../../../М10_ServerVMS/vmsserver/vmsplatform/eventindex.py.md` for what they do: `EventIndex(reader, path=":memory:", wall, lost_after=45)` is a SQLite cache rebuilt from the resources' heartbeats (`rebuild(resources_seen)`), tailed on a timer (`tail(...)`) and queried by time, camera and kind (`query(t0, t1, cam=, kind=)`); `ResourceReader` is its HTTP client against a resource job's `/buckets/<sub>/<unit>`, `/events/<path>`, `/mirrored/<server>` and `/events/.mirror/<server>/<path>`.

## Module-level names
- `EventIndex`, `ResourceReader` — re-exports; no code of М11's own.

## Notes
- `__main__.console` imports them from `vmsplatform.eventindex` directly and runs the index beside the console (`deploy/console.nomad.hcl`: "the page, the API, the eventindex"); it is rebuilt on every start, which is what makes it a cache and not a store.
- The tests (`tests/test_lesson3_events.py`, `test_the_console_over_http`) also import from `vmsplatform.eventindex`, so nothing in the package depends on this alias.
