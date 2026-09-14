# console.py — the console as data: SpecConsole over the same spec, with idempotent writes and the subsystem's registered extra routes

**Role in the module.** Lesson 5, the other half of `spec.py`. A subsystem's YAML already says what its units are, which fields the operator owns and what leaves the cluster; that is everything a console needs to list, edit and show them, so the console is one class run from the same spec. It serves `console.html`, `/spec` (what the page reads first), the rows with the read model, `/where`, `/resources`, `/unplaceable`, `/events` (if an `EventIndex` is behind it), `/metrics`, and the writes — POST/PUT/DELETE on the rows and POST `/marks` — with idempotency keys stored in Variables so a retry answered by another console instance is the same request. What a subsystem adds is registered, not subclassed: `extra(handler, method, path, query)` gets every request the built-in routes do not claim (the VMS: `/timeline` and `/segment`). The console holds the subsystem's `SpecController` with the *console's* token (the operator's rows, never placement), so a write it should not make is a 403 from the store, not a rule in this file. `vms/console.py` builds it via `make_console`; the deploy unit `vmsconsole.container` runs it as its own process.

## Module-level names
- `PAGE` — absolute path of `console.html` beside this file; served at `/`.

## Functions
### `send_file(handler, path, content_type) -> None`
A file, whole or by `Range` — what a `<video>` element asks for. Parses `bytes=a-b`, replies 206 with `Content-Range` and `Accept-Ranges` when a range was asked, 200 otherwise. Used by the VMS's `/segment/<path>` extra; the test asks `bytes=10-19` and gets 206 with `Content-Range: bytes 10-19/256`.

### `heartbeats(objects, prefix) -> dict[str, Heartbeat]`
Every worker's last heartbeat under `prefix`, whatever its age — the read model's and `/metrics`' source. Same scan as `Controller.workers_seen` without the age filter.

## `class IdempotencyKeys`
A retried POST must be the same POST whichever console answers it, so the key lives in the store, not in a process: `<sub>/idem/<key>` is claimed by a create-only CAS before the write and filled with the reply after it. A second instance that sees the claim waits for the reply and serves it; it never repeats the write. Keys older than `ttl` are pruned on the way past, at most once a minute.

### `__init__(self, vars_, prefix, wall, ttl=86400.0, clock=time.monotonic, sleep=time.sleep)`
`prefix` is `<sub>/idem/`; `wall` stamps `at`; `clock` rate-limits pruning; `sleep` is the wait between polls (injected for tests).

### `_path(self, key) -> str`
Refuses an empty key, one containing `/` or `..`, or longer than 200 chars (`Refused`: must be one path segment) and returns `prefix + key`.

### `claim(self, key) -> None | (status, body)`
Try `put({state: pending, at}, cas=0)`: success means ours to answer — return `None` (the caller does the write, then `store`). On `Conflict`, another instance holds it: poll up to 40 × 50 ms; if the row vanished (pruned or the claimant crashed mid-flight) claim again; if `state == done` return `(status, body)`; after the polls, `409 in flight`. `test_a_retry_that_lands_on_another_console_is_one_camera` covers all of it: the second console returns the first's 201 body; a key with a pending claim makes console A wait and then serve B's reply without creating a camera; a key `a/b` is 400.

### `store(self, key, resp) -> None`
Overwrite the row with `{state: done, status, body: json, at}` (no CAS: the claimant owns it).

### `prune(self) -> int`
At most once per 60 s of monotonic time: delete every key under the prefix whose `at` is older than `ttl`, by CAS (a conflict skips it). The test advances a day and sees 2 pruned, then 0, then a re-used key create a new camera — a forgotten key is a new request, by design.

## `class SpecConsole`
One console for every subsystem.

### `__init__(self, ctl, marks_root=None, index=None, worst_failover=0.0, wall=None, extra=None, media=False, lost_after=45.0)`
`ctl` is the subsystem's `SpecController` holding the console's token; `marks_root` is this server's resource root — if given, `self.marks` is an `EventLog(marks_root, "console", <hostname:pid>, epoch 1)` (the console's own log; one writer, so epoch 1 forever); `index` is an optional `EventIndex`; `worst_failover` is a number exported on `/metrics`; `extra` is the subsystem's route function; `media` tells the page it may draw a timeline and play. `seen` is the `IdempotencyKeys` over `<sub>/idem/`. `_scan` caches the assignment directory; `scans` counts cache refreshes.

### `describe(self) -> dict`
`/spec`'s body: `{name, rows, id, media, fields: [{name, type, default, required}], metrics: {prefix, running}}` — the page's only knowledge of the subsystem.

### `directory(self) -> dict[str, list[str]]`
`{worker: units}` from one scan of the assignments, cached for 5 s of monotonic time.

### `where(self, uid) -> str | None`
Every worker whose assignment lists the unit, joined with `+` — a reassignment window shows as both. Compared against the placement row in `/where`.

### `metrics_text(self) -> str`
Prometheus text, all prefixed with the subsystem name: `<p>_workers_live`; `<p>_worker_headroom{worker,server}` per live worker and the total `<p>_headroom` (what the autoscaler sums); `<p>_worker_load{worker}` = `1 − headroom/capacity` per live worker (assigned/capacity: what a target-value policy scales on); `<p>_epoch_conflicts{worker}` counter from every heartbeat; `<p>_failover_seconds{kind="worst"}`; `<p>_resources_live`; and `<p>_<running_gauge>` — the count of status entries in phase `running` on live workers (`vms_cameras_recording`). The page reads two of these for its status line.

### `create(self, body) -> (status, dict)`
`ctl.create(body)` → 201 with the row plus `worker: None` (placed by the controller's next pass, never by the console); `Refused` → 400 `{detail, error}`.

### `update(self, uid, body) -> (status, dict)`
`ctl.update` → 200 with the row; `Refused` → 400; `KeyError` → 404.

### `delete(self, uid) -> (status, dict)`
404 if the unit is absent; else `ctl.delete(uid)` and 200 `{deleted: uid}`.

### `mark(self, body, user) -> (status, dict)`
An operator's observation. 503 if there is no resource on this server; 400 unless the body names `cam` or `unit`. Appends `{kind: mark, user, note, cam|unit}` at `wall()` to the console's own bucket — `cam` is stored as an int because it is the field the index joins on — and returns 201 `{subsystem: "console", unit: <instance>, bucket: <relative path>}`. It is the console's event, into `console/<instance>/e1/…` on this server's resource, never a worker's bucket.

### `handler(self)`
Builds and returns the request handler class bound to this console.

#### `class H(BaseHTTPRequestHandler)` (nested)
- `log_message` — silenced.
- `_send(status, body, raw=False)` — JSON (or raw text) with `Content-Type` and `Content-Length`.
- `_body()` — the JSON request body, `{}` if empty.
- `_uid()` — the last path segment (query stripped) through `spec.parse_id`.
- `_extra(method, path, q) -> bool` — call the subsystem's `extra`; `None` means not ours (return False). `()` means the extra wrote the reply itself (`send_file`). `(status, dict|list)` is sent as JSON; `(status, bytes)` raw; `(status, bytes, headers)` raw with headers.
- `do_GET` — routes, in order:
  - `GET /` or `/index.html` — `console.html`.
  - `GET /spec` — `describe()`.
  - `GET /<rows>` — `{rows: ctl.read_model(lost_after), configured: ctl.units()}`: the heartbeats' view over the configured rows.
  - `GET /where/<id>` — `{worker, reason}` from the stored placement (404 with nulls if unplaced), plus `directory` (the assignments' answer) and `scans`.
  - `GET /resources` — every resource heartbeat with `state: live | silent` by `lost_after`.
  - `GET /unplaceable` — `ctl.unplaceable()`.
  - `GET /events?from&to&cam|unit&kind&subsystem` — 503 if no index; else builds `current_epochs` from every `<sub>/epoch/*` row and calls `index.query`. A numeric `unit` is treated as `cam`; a non-numeric one is passed as `unit`.
  - `GET /metrics` — `metrics_text()` as `text/plain`.
  - otherwise `_extra("GET", …)`; then 404 `{detail, error}`.
- `_idem() -> key | None` — for POST: 400 if `Idempotency-Key` is missing or malformed; if `claim` returns a prior reply, send it and return `None`; else return the key.
- `do_POST`:
  - `POST /<rows>` (Idempotency-Key required) — `create(body)`; the reply is stored under the key and sent.
  - `POST /marks` (Idempotency-Key required; `X-User` header, default `operator`) — `mark(body, user)`; stored and sent.
  - any other path — `_extra("POST", …)` or 404.
- `do_PUT`:
  - `PUT /<rows>/<id>` — `update(uid, body)`; `Idempotency-Key` is optional here: if present the same claim/store dance applies. Any other path is 404.
- `do_DELETE`:
  - `DELETE /<rows>/<id>` — `delete(uid)`; no idempotency key (a second delete is 404, "gone is gone"). Any other path is 404.

### `serve(self, host="127.0.0.1", port=8080) -> ThreadingHTTPServer`
Starts the server in a daemon thread and returns it (tests use `port=0` and read `server_address`).

## Notes
- `test_the_console_over_http` walks the whole surface: POST twice with one key is one camera; the console's controller cannot `place` (`Forbidden`); PUT `{"worker": "w-9"}` is 400; `/cameras` rows show `phase running` and `server srv-1`; `/where/1` agrees with the directory; `/spec` says `rows cameras, media true`; `/metrics` contains `vms_cameras_recording 1`; `/marks` writes to `console/<instance>/e1/`; the page mentions `/spec`, `/timeline/`, `/segment/`, `<video>` and never the word camera outside its comment; `/timeline/1` and a ranged `/segment/` come from the VMS extra; PUT `{"enabled": false}` bumps revision to 2; DELETE marks the row and the placement waits for `unplace_deleted`.
- Idempotency covers POST always, PUT optionally, DELETE never; the page sends a fresh key with every request (including DELETE, where it is ignored).
- `/events` relies on the resource's HTTP and an `EventIndex`; on one box without one it is an honest 503, and the page tolerates that.
