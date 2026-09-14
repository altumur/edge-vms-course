# resource.py — the resource as a platform job: heartbeat, buckets over HTTP, retention by each subsystem's row, mirror to a peer, restore

**Role in the module.** Lesson 3. One resource per server, pinned there for as long as the server exists. It knows the shape of what every subsystem leaves on the server's disks — `<root>/<subsystem>/<unit>/e<epoch>/...` — and nothing about what it means; the VMS keeps media and a manifest beside its buckets and the resource neither reads nor names them. It writes its own heartbeat object (`platform/resources/<server>/heartbeat`), serves buckets over HTTP, and runs a policy pass on a timer: each subsystem's registered hook first (the VMS registers repair, close and media retention via `Resource.register`), then bucket retention by each subsystem's own `<sub>/retention[/<unit>]` row, then the mirror. Mirroring is a knob (`platform/mirror`), and peers are chosen by a rule — the next `copies` live resources after mine in sorted order — so nobody assigns them. `restore` is the reverse, run by the owner at start. No controller is involved in any of it. `eventindex.py` reads the same HTTP routes; `console.py` reads `resources_seen`.

## Module-level names
- `MIRROR_DIR = ".mirror"` — under a resource root, `.mirror/<server>/<sub>/<unit>/e<epoch>/…` holds copies of another server's closed buckets. Hidden so `subsystems_under` never counts it as this server's data.
- `MIRROR_KEY = "platform/mirror"` — the Variable `{enabled, copies}`.
- `RESOURCES = "platform/resources"` — the object-store prefix for resource heartbeats.

## Functions
### `bucket_from_line(line) -> Bucket`
Parses the JSON line `Bucket.line()` produced (types coerced back). Used by `PeerClient` and `eventindex.ResourceReader`.

### `mirror_settings(vars_) -> dict`
Reads the knob: `enabled` is true only if the row exists and says `"true"`; `copies` defaults to 1.

### `retention_days(vars_, subsystem, unit, default=365.0) -> float`
The unit's days if its subsystem set `<sub>/retention/<unit>`, else the subsystem's `<sub>/retention`, else a year. For the VMS the per-unit row is the derived row `vms/retention/<id>` written by `SpecController._derived` from `events_retention_days`; on delete it becomes `{days: 0}` so the buckets go on the next pass. Each subsystem's controller owns its row; the resource only reads.

### `peers_of(server, live, copies) -> list[str]`
The rule that replaces a map: sort the other live servers, take those after mine then wrap around, and keep the first `copies`. `test_the_resource_is_a_platform_job…`: with `srv-a, srv-b, srv-c`, `srv-a`'s peer is `srv-b` and `srv-c`'s is `srv-a`.

### `resources_seen(objects) -> dict[str, dict]`
Every resource heartbeat under `platform/resources/`, keyed by `server`, whatever its age. Callers filter by `ts`.

### `mirrored_servers(root) -> list[str]`
The server names present under `<root>/.mirror/`.

### `mirrored_buckets(root, server, bucket_seconds=600) -> list[Bucket]`
Copies this resource holds of `server`'s buckets, parsed relative to `.mirror/<server>` so `path` is the original path on `server`. Line counts are taken from the copy.

## `class PeerClient`
How one resource talks to another: HTTP. Tests substitute an in-process client with the same three methods over directories.

### `__init__(self, timeout=5.0)`
### `mirrored(self, url, server) -> list[Bucket]`
`GET <url>/mirrored/<server>` — which of `server`'s buckets the peer already holds.
### `put(self, url, server, path, data) -> None`
`PUT <url>/mirror/<server>/<path>` with the bucket's bytes; anything but 200/201/204 raises `IOError`.
### `get(self, url, server, path) -> bytes`
`GET <url>/events/.mirror/<server>/<path>` — pull a copy back (restore).

## `class Resource`
One server's resource: its tree, its heartbeat, its policy pass.

### `__init__(self, root, server, url, vars_, objects, bucket_seconds=600, wall=time.time, peers=None, lost_after=45.0)`
`root` is the tree (created), `server` the name that goes into heartbeats and peer selection, `url` how others reach this resource's HTTP. `hooks` starts empty. `lost_after` is how old a peer's heartbeat may be to count as live.

### `register(self, subsystem, hook) -> None`
A subsystem installs an object with `pass_(now) -> dict` for its own part of the tree — the same "code under a name" door `spec.register_constraint` opens.

### `units(self) -> dict[str, list[str]]`
`subsystems_under(root)` — what is here, from the directories.

### `closed_buckets(self) -> list[Bucket]`
Every bucket of every unit whose `end <= now`. Only these are mirrored.

### `usage(self) -> int`
Total bytes under `root`, for the heartbeat.

### `heartbeat(self) -> dict`
Writes `{server, ts, url, usage, units, mirrors: {server: n copies}}` to `platform/resources/<server>/heartbeat` and returns it. `units` is how the index discovers subsystems; `mirrors` is how `restore` and the index find who holds copies.

### `live_resources(self) -> dict[str, dict]`
`resources_seen` filtered to heartbeats younger than `lost_after`.

### `retain(self) -> int`
For each subsystem and unit, delete bucket files whose `end` is older than `retention_days` — files only; a subsystem that indexes its buckets (the VMS's manifest) drops the lines in its own hook. Returns the count. The test sets `other/retention {days: 1}`, advances three days and sees exactly the `other` bucket go.

### `mirror(self) -> dict`
The knob. If disabled, `{enabled: False, mirrored: 0, peers: []}`. Otherwise, for each peer from `peers_of`, ask what it already holds and `put` every closed bucket it lacks — any subsystem's, exactly once each, by the server that owns it. Returns `{enabled, mirrored, peers}`. The test shows two buckets mirrored the first pass and zero the second.

### `restore(self) -> dict`
The reverse, run by the owner: for every live peer whose heartbeat lists me under `mirrors`, pull each of my buckets it holds that I do not have (tmp + rename), then, if anything came back, run every registered hook once so the subsystem re-indexes. Returns `{pulled, <sub>.<key>: …}`. In the test, `srv-a` with a wiped disk pulls 2 buckets; the open bucket that was never mirrored is the RPO.

### `pass_(self) -> dict`
The timer's body, in order: each subsystem's hook (it may index or drop lines), then `retain`, then `mirror`; results flattened into one dict (`<sub>.<key>`, `removed`, `enabled`, `mirrored`, `peers`).

## Functions (continued)
### `serve(resource, host="0.0.0.0", port=8090, extra=None) -> ThreadingHTTPServer`
The resource over HTTP, in a daemon thread. `extra(path, headers) -> (status, bytes[, headers]) | None` lets a subsystem add its own reads (the VMS: manifests and footage).

#### `class H(BaseHTTPRequestHandler)` (nested)
- `log_message` — silenced.
- `_raw(status, body, headers=())` — send a status, `Content-Length`, optional headers and the bytes.
- `do_GET`:
  - `GET /buckets/<sub>/<unit>` — `buckets_under` for that unit, one `Bucket.line()` per line, 200.
  - `GET /mirrored/<server>` — `mirrored_buckets` for that server, same format.
  - `GET /events/<path>` — the raw bytes of one bucket; `path` may begin with `.mirror/<server>/`. 404 if it contains `..`, does not end in `.events.jsonl`, or is not a file.
  - anything else — `extra(path, headers)` if given and it answers; otherwise 404.
- `do_PUT`:
  - `PUT /mirror/<server>/<path>` — another resource leaves a copy of one of its closed buckets. 400 if `..`, empty server, or not `.events.jsonl`; writes to `.mirror/<server>/<path>` via tmp + rename (a copy appears whole or not at all); 204. Any other PUT is 404.

## Notes
- The heartbeat's `units` and `mirrors` are derived from the tree on every call — the resource keeps no state a restart could lose.
- `retain` and `mirror` both walk the tree each pass; on one box that is cheap, and it keeps the job stateless.
- The console's `/resources` route is `resources_seen` with a `live | silent` label by `lost_after`; `/metrics` counts `<sub>_resources_live` the same way.
