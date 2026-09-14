# shadow.py — Lesson 2: shadow mode — the divergence report between what the domain placed and what the clusters' workers report, and the written exit criterion

**Role in the module.** Lesson 2, "the domain that writes nothing". Before the domain is allowed to write, it computes what its directory *would* say, observes what every cluster's workers report, and emits a divergence report; it changes nothing. The docstring's taxonomy, per camera (М9's unit was a recorder with its own database; here it is a camera on a worker): `lagging` (a worker has not yet applied a revision, within grace — not a fault), `stalled` (behind past grace with no progress — the real "it did not take effect"), `orphaned` (placed by the domain, no worker runs it — fault), `unmanaged` (a cluster runs a camera the domain never placed — in shadow mode a *measurement*), `conflict` (two clusters or two workers claim one camera — always a fault: placement or fencing), `stale_epoch` (a worker reports under a superseded epoch — the fence catching a writer that should have stopped). The one number is `unmanaged == 0`; ordering beats equality ("behind by 4 for 40 minutes" is an incident, "diverged" is an alert you learn to ignore). Inputs come from `federation.Cluster.heartbeats()` and each cluster's `vms/epoch/*` Variables (М10's epoch rows); the placement map comes from `placement.py`'s stored decisions. Nothing here is wired into a process; the tests drive it.

## `class WorkerReport` (dataclass)
What one worker's heartbeat says, reduced to what the shadow needs.
- `worker: str`, `cluster: str`, `server: str` — where the report is from.
- `cameras: list[tuple[str, int, int, int]]` — `(ref, epoch, revision, observed_revision)` per running camera; the ref is the domain's name (the type comment says so), never the cluster's id.
- `ts: float` — the heartbeat's timestamp (carried, not used by `compare`).

## `class Finding` (dataclass)
- `kind: str` — one of the six kinds.
- `camera: int | None` — annotated `int` but in practice the ref string (see Notes).
- `where: str | None` — `<cluster>/<worker>` for a worker-level finding, the cluster name for `orphaned`, `None` for a multi-claimant `conflict`.
- `detail: str` — the sentence with the distance and the time.

## `class DivergenceReport` (dataclass)
- `findings: list[Finding]`.
### `count(self, kind) -> int` — how many findings of that kind.
### `unmanaged` (property) — `count("unmanaged")`, the one number.
### `faults` (property) — the findings whose kind is `stalled`, `orphaned`, `conflict` or `stale_epoch` (`lagging` and `unmanaged` are not faults).
### `summary(self) -> str` — `lagging=0  stalled=0  orphaned=1  unmanaged=1  conflict=0  stale_epoch=0`, one line for a log.

## `class Shadow`
The comparer; it keeps one piece of state across passes — when each (worker, camera) last made progress — because "stalled" is a statement about time. The docstring names the three inputs: `placed` (ref → cluster the domain's placement says), `epochs` ((cluster, ref) → current epoch from that cluster's `vms/epoch/<id>`, keyed by the ref the snapshot maps the id to) and `reports` (every worker's heartbeat, every cluster). Everything is keyed by ref: two clusters both have an id 1, and the domain never compares by it.

### `__init__(self, grace_seconds=60.0)`
`grace` separates lagging from stalled. `_progress: {(worker, ref): (last observed_revision, since when)}`.

### `compare(self, placed, epochs, reports, now) -> DivergenceReport`
One pass, in three sweeps:
1. Per reported camera: the current epoch is `epochs[(cluster, ref)]`, defaulting to the reported epoch when the map has no entry. A report under an older epoch is a `stale_epoch` finding and is then *skipped* — "a fenced writer's claim counts for nothing" — so it neither claims the camera nor counts as progress. Otherwise the (cluster, worker) is recorded as a claimant; the progress table is updated if `observed_revision` moved (resetting `since` to `now`); and if `revision − observed > 0` the finding is `stalled` when `now − since > grace`, else `lagging`, with `behind by N revision(s) for Ts`. `test_slow_versus_stuck`: behind by 2 at t=100 is lagging; the same at t=200 (100 s, past a 60 s grace) is stalled; progress to 6 at t=210 is lagging again; caught up is nothing.
2. Per claimed camera: more than one claimant → `conflict` (`claimed by [...]`, `where=None`); one claimant but not in `placed` → `unmanaged` ("running, never placed by the domain"); one claimant in a cluster other than the placed one → `conflict` ("placed in south, running in north").
3. Per placed camera with no claimant → `orphaned` ("placed, and no worker in the domain runs it") — including a camera whose only claimant was fenced by rule 1 (`test_the_taxonomy`'s camera 9).

## Functions

### `reports_from(fed) -> (list[WorkerReport], dict[(cluster, ref), int], list[str])`
What the shadow job would read each pass, from every reachable cluster: for each worker heartbeat, every status entry with `phase == "running"` becomes `(ref, epoch, revision, observed_revision)`, where `ref` is `st["ref"]` or, for a camera created without one, `<cluster>/<id>` (unique across the domain, and visibly not the domain's); a `WorkerReport` per worker with its `server` and `ts`. Then every `vms/epoch/<id>` Variable in that cluster is read and keyed by `(cluster, ref)` using the id→ref map collected from the running entries (falling back to `<cluster>/<id>`). A cluster that raises `Unreachable` at any point lands in `down` and contributes nothing — the test comment: "a silent cluster is named, not counted as clean". `test_the_report_from_what_clusters_publish` runs two real М11 clusters and gets `unmanaged=1` for the camera south created that the domain never placed, with `where == "south/w-0"`.

### `exit_criterion(rep, consecutive_clean, required=3) -> (bool, str)`
The written criterion for switching the domain into write mode, in order: any `unmanaged` → no ("the model does not describe everything that runs"); any fault → no (names the kinds); fewer than `required` consecutive clean reports → no (`2/3 consecutive clean reports`); else yes: "unmanaged == 0, no faults, stable across reports: the domain may write". The caller keeps the consecutive-clean counter.

## Notes
- Type annotations lag the ref change: `Finding.camera: int | None`, `claims: dict[int, …]` and `_progress`'s `(str, int)` key are all annotated as ints, but every value that flows through is the ref string (`test_the_report_from_what_clusters_publish` asserts `findings[-1].camera == "3"`). Harmless at runtime.
- `_progress` is keyed by `(worker, ref)` without the cluster; since refs are domain-unique this is fine, and the `<cluster>/<id>` fallback keeps unplaced cameras from colliding across clusters that share worker names.
- The epoch map only receives the id→ref mapping for *running* cameras; an epoch row for a camera that is not running is keyed `<cluster>/<id>` and, having no claimant, is never consulted.
