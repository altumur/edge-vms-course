# console.py — the cluster console: М10's `SpecConsole` over the VMS spec plus two registered routes, the merged timeline and the proxied segment, and the merged event query in place of an index

**Role in the module.** Lesson 5. The console is not rewritten for a cluster: it is `psimplatform.console.SpecConsole` (see `../../../М10_ServerVMS/vmsserver/psimplatform/console.py.md`) run from `vms.subsystem.yaml` — the page, `/spec`, `/cameras`, `/where`, `/resources`, `/unplaceable`, `/events`, `/metrics`, `/marks`, the POST/PUT/DELETE writes and the Idempotency-Key store. What a cluster changes is *where the bytes are*: a camera's footage may sit on two servers' resources, so this module registers (never subclasses) an `extra` handler with two GET routes and passes `media=True` so the page draws a timeline and a player. It runs as its own `system` job with its own token (`deploy/console.nomad.hcl`, `deploy/console-policy.hcl`): the operator's rows, never placement — a write it must not make is a 403 from raft, not a rule here. Used by `__main__.console` and by `test_the_console_over_http`.

## Module-level names
- `SpecConsole`, `heartbeats` — imported from `psimplatform.console`; `heartbeats` is re-exported (`noqa: F401`) so a caller can say `from cluster.console import heartbeats`.
- `current_epoch`, `resources_seen` — the platform's readers: a camera's current epoch from `vms/epoch/<id>`, and every resource's last heartbeat keyed by server.

## Functions

### `cluster_routes(ctl, reader=None) -> extra`
Returns the `extra(handler, method, path, q)` callable that `SpecConsole` consults for every request its own routes do not claim. `reader` defaults to `timeline.ManifestReader()` (HTTP); tests pass a directory reader. Only `GET` is handled; anything else returns `None` (→ the console's 404).
- `GET /timeline/<id>?from=&to=` — `cid` from the path; `cur` = `current_epoch(ctl.vars, ctl.sub.epoch_key(cid))` or `None` when the camera has never had an epoch; returns `200` and the dict from `merged_timeline(resources_seen(ctl.objects), reader, cid, from|0, to|1e12, cur, ctl.wall())` — segments from every resource that reports the camera, each tagged with its `server` and `fenced`, plus the `unreachable` list and the "unavailable … not lost" note (see `timeline.py.md`).
- `GET /segment/<path>?server=<s>` — the bytes of one segment, fetched from *that* server's resource job. Looks the server up in `resources_seen()`; a path containing `..` or an unknown server is `404 {"error": "no such resource"}` (the test asserts 404 for a server that has never heartbeaten). Builds `GET <res.url>/segment/<path>`, forwarding the client's `Range` header if present, 10-second timeout. An `HTTPError` from the resource is passed through with its code and a message naming the server; any `OSError` (connection refused, timeout — `URLError` is an `OSError`) is `503 "… is not answering — unavailable, not lost"`. On success returns `(status, data, headers)` with `Content-Type: video/mp4`, `Accept-Ranges: bytes` and the resource's `Content-Range` when it sent one, so a `206` with `bytes 0-9/256` reaches the browser intact.
- Any other path → `None`.

### `class MergedIndex(objects, fetch=None, wall=time.time, lost_after=45.0, timeout=3.0)`
What stands behind the console's `/events` on a cluster: nothing of its own. `query(t0, t1, cam, kind, subsystem, unit, current_epochs, limit)` reads `resources_seen(objects)`, asks every LIVE resource `GET <url>/events?from&to&cam&kind&subsystem&unit&limit` (`fetch(url, params) -> dict`; HTTP by default, the tests pass a call into the resource's `ResourceIndex`), and merges: events sorted by `t` and cut to `limit`; an event whose `server` is not the resource that answered is a mirror copy — dropped if its owner is live (the owner answers for itself), deduplicated by `(server, bucket, t, kind, unit)` when two peers hold it, and the owner listed in `from mirror`; `fenced` set per event from `current_epochs[(subsystem, unit)]`; `state` is `"live"` plus `; a, b unreachable` (live by heartbeat but not answering, or silent with nobody holding copies) and `; c from mirror`. The same `{events, state}` shape `EventIndex.query` returns, so `SpecConsole` cannot tell the difference.

### `make_console(ctl, reader=None, worst_failover=0.0, index=None, archive_root=None) -> SpecConsole`
`SpecConsole(ctl, marks_root=archive_root, index=index or MergedIndex(ctl.objects, wall=ctl.wall), worst_failover=worst_failover, extra=cluster_routes(ctl, reader), media=True)`. `archive_root` is this server's resource, where the console's own `console/<instance>/…` marks bucket is written; `index` is what `/events` queries — a `MergedIndex` over the resources unless a test passes its own; `worst_failover` is the datasheet number `/metrics` reports as `vms_failover_seconds{kind="worst"}`.

### `metrics_text(ctl, worst_failover) -> str`
A throwaway `SpecConsole` just to render the Prometheus text — for a test or a script that wants `/metrics` without a socket.

### `serve(ctl, host="127.0.0.1", port=8080, reader=None, worst_failover=0.0, index=None, archive_root=None) -> ThreadingHTTPServer`
`make_console(...).serve(host, port)`: a `ThreadingHTTPServer` already serving on a daemon thread; the caller keeps the handle to `shutdown()`. `port=0` in the test binds an ephemeral port read back from `server_address`.

## Notes
- The `extra` reply protocol is the platform's: `(status, dict)` is sent as JSON, `(status, bytes, headers)` raw — both are used here.
- The `/segment` proxy reads the whole segment into memory before replying (`r.read()`); segments are 600-second MP4s, so a Range request from the player is the normal case.
- `test_the_console_over_http` proves through this file: a POST with an Idempotency-Key retried is answered once (`vms/idem/*` in raft, so *any* instance answers the retry), `PUT {"worker": …}` is refused with 400, `/where/1` agrees with the directory and names the server, `/metrics` carries the worst failover and live counts, a mark lands in the console's bucket on `srv-a`'s resource and is found by the index on its `cam` field, and a `Range: bytes=0-9` on `/segment/<path>?server=srv-a` comes back `206` with `Content-Range: bytes 0-9/256`.
