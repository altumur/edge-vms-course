# readview.py — Lesson 3: the camera list built from every cluster's worker heartbeats and its published configuration, held in memory, with an age on every row and one cause per dead server

**Role in the module.** Lesson 3, "the camera list, and where the console gets it". The docstring rules out the two easy answers: not a fan-out to N cluster consoles (waits for the slowest, breaks on the first dead one) and not status in Variables (raft is not for frequent, large data). The read model reads what each cluster's *workers* already publish beside their heartbeat — `vms/<worker>/heartbeat`, carrying the worker's status per camera, its server and its epochs (`../../../vmsserver/vms/worker.py.md`, `heartbeat_once`) — holds them in memory and serves list, search and pagination from there: no call to any worker or controller on any request; a cache that admits to being one, rebuilt by one pass over the objects on restart. Staleness is shown, never hidden: every row carries the age of its heartbeat, a worker older than `lost_after` is "stale — last known state" with its cameras still listed, and a cluster that did not answer keeps its rows from the last successful pass and is reported as unreachable — never as empty. Grouped by failure domain: the heartbeat carries the server, so when a server dies its workers go silent together and the console shows ONE cause. Used by `console.Console` (a refresher thread calls `refresh()`; the routes call `list()` and `causes()`) and the Lesson 3 tests. It reads a SECOND source for the cameras no worker reports at all: each cluster's `vms/snapshot/*`. A heartbeat is an observation (*this camera is running, at this revision, on this worker*); the snapshot is the configuration (*this camera is supposed to exist*). A camera in both appears once, from the observation; a camera only in the snapshot — nothing placed it, or the worker holding it has never reported — appears marked `configured`, and that is the whole of "set up but not working". Until this was written those cameras were not in the list at all: an operator could add one, watch it fail to be placed, and see nothing in the domain — absence, which is the most confusing shape a fault can take.

## `class Row` (dataclass)
One camera as the console lists it.
- `camera: int` — `st["id"]` from the heartbeat: the cluster-local id (see Notes).
- `ref: str` — the name the DOMAIN knows the camera by. It is also the dedup key between the two sources, because the cluster's number is the cluster's: every cluster has its own camera 7.
- `name: str`, `worker: str`, `cluster: str`, `server: str` — where it is.
- `phase: str`, `position: str` — the worker's own words (`running`/`pending`/`failed`, `converged`/`lagging`/`stalled`), passed through untouched (the API docstring insists on this).
- `revision: int`, `observed_revision: int`, `epoch: int` — from the status entry.
- `age: float` — seconds since the heartbeat this row came from.
- `worker_state: str` — `live` | `stale` | `unreachable` (the cluster did not answer on the last pass).

### `to_json(self) -> dict`
The fields plus `as_of`: `as of 3 s ago` when live, else `<state> — last known state, 100 s old`.

## `class Cause` (dataclass)
Silence explained at the largest failure domain.
- `scope: str` — `cluster` | `server` | `worker`.
- `name: str` — the cluster, `<cluster>/<server>`, or `<cluster>/<worker>`.
- `silent_for: float` — seconds since the most recent heartbeat in the group (for a cluster: since the first failed pass).
- `workers: list[str]`, `cameras: int` — what the silence covers.
### `sentence(self) -> str` — `server silent: north/srv-1 for 100 s — 2 worker(s), 100 camera(s)`.

## `class Snapshot` (dataclass)
The last heartbeat seen from one worker, as kept in memory: `worker`, `cluster`, `ts`, `server`, `status` (the raw list of status dicts), and `doors` — the worker's published `live_url`, `playback_url` and `coverage` (`DOORS`), kept since Lesson 13 because the source book is built from them. Not to be confused with a cluster's `vms/snapshot` object.

## `class ReadView`

### `__init__(self, fed, lost_after=45.0, wall=time.time, lanes=1, backoff=0.0, backoff_max=60.0)`
`lost_after` is the same 45 s as М11's `disconnect.lost_after` and the resource's `lost_after`: a heartbeat older than this means the worker is gone. State: `snapshots: {(cluster, worker): Snapshot}`, `configured: {cluster: [snapshot rows]}` and `configured_at: {cluster: ts}` (the second source and its age) (survives across passes — that is what keeps an unreachable cluster's rows), `cluster_ok: {cluster: last good pass time}`, `cluster_down_since: {cluster: first failed pass time}`, `passes` (reported by `/healthz`). Lesson 11: `lanes` members are read at once; `backoff` > 0 makes a silent member wait `backoff · 2^(failures−1)` seconds, capped at `backoff_max`, before it is asked again (`failures`, `retry_at`). The defaults are Lesson 3's pass: one lane, every member every pass.

### `_read(c)` (static) and `_try(self, c)`
One member's part of a pass: `c.heartbeats()` and `c.snapshot()`, each read ONCE — the pass used to call `snapshot()` twice (for the rows and for their age), a third of every pass at three hundred members (`test_one_pass_asks_each_member_four_things_and_no_more`). `_try` turns `Unreachable` into `None`.

### `refresh(self) -> None`
The one pass, over the members whose `retry_at` has come. Read in a thread pool of `lanes` when `lanes > 1`, in turn otherwise; the results are applied in member order either way (`test_reading_in_lanes_changes_the_time_and_nothing_else`; `test_lanes_are_real_threads_not_only_arithmetic` times real sleeping stores). For a member that answered: every worker's heartbeat replaces its `Snapshot`, the snapshot's rows and `ts` replace `configured`/`configured_at`, `cluster_ok` is stamped, and `cluster_down_since`, `failures`, `retry_at` are cleared. For one that did not: `cluster_down_since` (kept at its first value), and with `backoff` the next `retry_at`. Its rows stay as they were. Increments `passes`.

### `last_known(self, camera) -> tuple[str, dict] | None`
Lesson 9. The cluster whose last snapshot carried the camera the domain calls `camera` (its `ref`), and that row — kept when the cluster stops answering, which is the point: a kept edit is measured against what the domain last saw.

### `where(self, camera) -> Answer`
Lesson 11. "Where is camera X" from this pass's memory instead of `DomainDirectory.where`'s scan of every member — which at three hundred members is two calls per member per edit (`test_where_from_the_directory_scans_every_member_on_every_edit`: 28 550 calls for fifty edits through the directory, none from memory). Exactly as honest: found only in a member that answered, `unreachable` lists the silent ones and the never-read, two claimants raise.

### `rows(self) -> list[Row]`
From memory: for each snapshot, `age = now − ts`, `worker_state` = `unreachable` if the cluster is down, else `stale` if `age > lost_after`, else `live`; one `Row` per status entry. Sorted by (cluster, worker, camera).

### `list(self, q="", page=1, size=50, cluster=None) -> dict`
Filters rows by cluster and by `q` (case-insensitive substring of `name`, or exact match on the id), paginates, and returns `{total, page, size, rows: [to_json], clusters: {name: "ok"|"unreachable"}, complete: no cluster down}`. `test_two_hundred_cameras_from_heartbeats_nobody_called`: 200 rows from four heartbeat objects, `as of 3 s ago`, search `cam175` finds south/w-0. `test_unreachable_cluster_keeps_last_known_rows_and_says_so`: south down → its 50 rows still listed as `unreachable`, `clusters.south == "unreachable"`, `complete == False`.

### `causes(self) -> list[Cause]`
"Silence grouped by the largest failure domain that explains it." First, every down cluster is one `cluster` cause covering all its known workers and cameras. Then, among reachable clusters, the silent workers (`now − ts > lost_after`) are grouped by (cluster, server): if *every* worker known on that server is silent and there is more than one of them, that is one `server` cause (silent for `now − max(ts)`); otherwise each silent worker is its own `worker` cause. Sorted by most cameras first, then name. `test_kill_a_server_one_cause_displayed`: srv-1's two workers stop heartbeating, the others continue → exactly one cause, `server silent: north/srv-1 for 100 s`, workers `[w-0, w-1]`, 100 cameras, and the 100 rows are still listed as `stale` with `last known state`.

## Notes
- `Row.camera` is the heartbeat's `id` — the cluster-local id — and the row carries no `ref`. The README says the read model keys on `ref`; here two clusters each running an id 1 produce two rows numbered 1, distinguishable only by the `cluster` column, and `q="1"` matches both. The tests use ids equal to refs so this is not exercised. See the report.
- A server with a single known worker that goes silent is reported as a `worker` cause, not a `server` cause (the `len(group) > 1` condition): the code will not claim "server" from one witness.
- `cluster_ok` is written on every good pass but read by nothing.
- `refresh()` catches only `Unreachable`; any other exception (e.g. `NotImplementedError` from an `HttpObjectStore.list`) escapes to `Console._refresher`, which swallows it — the view then never fills. See `deploy/console.nomad.hcl.md`.
