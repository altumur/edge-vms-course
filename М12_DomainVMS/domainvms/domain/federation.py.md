# federation.py — Lesson 1: N clusters as a `Federation` of `Cluster` handles, and a `DomainDirectory` whose every `Answer` says what it could not reach

**Role in the module.** Lesson 1, "what a cluster cannot know". A domain is N clusters, each its own Nomad region with its own raft, Variables and object store; nothing replicates between them (`deploy/federation.hcl`). So the domain's directory is an *aggregation* over N cluster directories — partial, stale by a bounded amount, sometimes incomplete — and the honest answer to "where is camera 7" when a cluster is unreachable is "not found in the clusters I could reach", never a short list rendered as complete. What a cluster publishes for the domain (М11 Lesson 5) is one object, `vms/snapshot` — the controller's copy of every camera row with the worker and server it is placed on, carrying a `ts` (`SpecController.publish_snapshot`, see `../../../М10_ServerVMS/vmsserver/psimplatform/spec.py.md`) — plus its workers' heartbeats `vms/<w>/heartbeat` (`../../../М10_ServerVMS/vmsserver/vms/worker.py.md`). The domain never reads a cluster's Variables for rows: the rows stay in raft with one writer, and what leaves is a copy with an age. Used by `placement.py` (candidates, the domain cluster's Variables), `readview.py` (heartbeats), `shadow.reports_from`, `api.py` and `console.py` (`where`), `runtime.py` (construction), and every test through `conftest.make_domain`.

## Module-level names
- `SNAPSHOT = "vms/snapshot"` — the one object key `Cluster.snapshot()` reads; the same key М11's controller writes.

## `class Unreachable(Exception)`
"The region did not answer." The signal every reader in this package catches to mark a cluster as down rather than fail. The docstring says it is raised by a cluster's Variables/objects when the link is down and that the fakes raise it on demand — `tests/conftest.py`'s `LinkedVariables`/`LinkedObjects` raise it when their `Link.up` is False. Note that М11's production adapters (`NomadVariables`, `S3ObjectStore`, `HttpObjectStore`) raise `urllib` errors, not this class, and nothing in this package translates them (see the report).

## `class Cluster` (dataclass)
The domain's handle on one region. Holds no state of its own beyond the two adapters and two facts the domain knows about it.
- `name: str` — the region name (`north`, `south`); the key in `Federation.clusters`, the `cluster` in every `Answer`, `Row` and `Finding`.
- `vars: Variables` — that region's Nomad Variables (М11 contract: `get → (items, index)`, `put(..., cas=)`, `list`). The domain cluster's `vars` is where placement, the signer's keys and identity live; a member cluster's `vars` is where its agent writes `domain/*`.
- `objects: ObjectStore` — that region's object store (`put/get/list`): the snapshot and the heartbeats.
- `reaches: frozenset = frozenset()` — the networks this cluster can see, e.g. `{"vlan:cctv-a"}`; the input to placement by reachability. Set by the tests' `make_domain`; `runtime.py` leaves it empty.
- `is_domain_cluster: bool = False` — the one cluster that hosts the domain services (signer, placement Variables, identity). The comment: a stated decision, not a discovery.

### `snapshot(self) -> dict | None`
`objects.get("vms/snapshot")` decoded as JSON, or `None` if the cluster has never published one. The dict is М10's snapshot shape: `{"cluster", "ts", "cameras": [{id, ref, name, worker, server, …}]}`.

### `heartbeats(self) -> dict[str, dict]`
Lists `objects` under `vms/`, keeps keys that end in `/heartbeat` and have exactly two slashes (`vms/<worker>/heartbeat`, not a deeper key), decodes each, and keys the result by the heartbeat's own `worker` field — the same rule as М11's `controller.heartbeats()`. The value is М10's heartbeat: `status` per camera (with `id`, `ref`, `phase`, `position`, `revision`, `observed_revision`, `epoch`), `server`, `ts`, `capacity`, `headroom`, …. Requires an object store with `list()`.

## `class Answer` (dataclass)
"Where a camera is, and how much of the domain that claim covers." Incompleteness is a field, not an exception.
- `camera: int` — the domain's name for the camera (its `ref`), echoed back as given.
- `worker: str | None`, `server: str | None` — from the snapshot row (`None` if the cluster has not placed it yet, or not found).
- `cluster: str | None` — the owning cluster, `None` when not found.
- `searched: list[str]` — clusters whose snapshot was read (sorted).
- `unreachable: list[str]` — clusters that raised `Unreachable` (sorted).

### `complete` (property) → `not self.unreachable`.
### `found` (property) → `self.cluster is not None`.
### `sentence(self) -> str`
The four honest sentences: found and complete — `camera 7 is on w-0 (srv-9) in south`; found but incomplete — the same plus `(and south could not be asked)`; not found and complete — `camera 7 is in no cluster of the domain (2 clusters searched)`; not found and incomplete — `camera 7 was not found in the 1 cluster(s) I could reach; south unreachable — not 'not anywhere'`. `no worker yet` stands in when the row is unplaced. The console's `/api/where` and the API's 404/503 detail carry this text verbatim.

## `class Federation` (dataclass)
- `clusters: dict[str, Cluster]` — by name, in insertion order.

### `add(self, c)` — registers a cluster under its name (a second `add` with the same name replaces).
### `domain_cluster` (property) `-> Cluster`
The single `Cluster` with `is_domain_cluster=True`. Zero or more than one raises `RuntimeError("exactly one domain cluster must be designated; found […]")` — `test_exactly_one_domain_cluster_is_designated`. Callers: `ClusterPlacer` (its Variables), `console.main` (`ClusterTrust`), every Lesson 4–7 test (`Signer` over its Variables).

## `class DomainDirectory`
"A directory of directories. Reads each cluster's snapshot; never copies the rows; never claims more than it reached." Stateless: every question is a fresh scan.

### `__init__(self, fed, wall=time.time)`
`fed` is the federation; `wall` is the clock used only by `ages()`.

### `scan(self) -> (dict[str, dict], list[str])`
One `snapshot()` per cluster. A cluster without a snapshot yet counts as `{"cameras": [], "ts": 0}` (reached, empty); one that raises `Unreachable` goes into the `down` list and is absent from the dict. Everything else in the class is built on this pair.

### `where(self, camera) -> Answer`
The docstring is the identity rule of the whole module: `camera` is the *domain's* name — the `ref` the domain gave the cluster when it forwarded the create (an operator field in М10's schema, `vms.subsystem.yaml.md`) — never a cluster's own `id`, because two clusters both have an id 7. It compares `str(row["ref"]) == str(camera)` across every reached snapshot. Two clusters both claiming the ref is `RuntimeError("… a placement failure, not a tie")` (`test_two_clusters_claiming_a_camera_is_a_fault_not_a_tie`) — the directory refuses to pick. One hit returns the row's `worker`/`server`/cluster with `searched`/`unreachable` sorted; none returns an `Answer` with `cluster=None` and the same two lists, so the caller (and `sentence()`) can tell "nowhere" from "nowhere I could reach".

### `holdings(self) -> ({cluster: {worker: [refs]}}, list[str])`
The domain-wide holdings from the snapshots: per cluster, per worker (an unplaced row goes under `"(unplaced)"`), the list of refs; a row without a `ref` is listed as `<cluster>/<id>` so it is still visible and still unique. The second element is the silent clusters. `test_where_across_three_clusters…` asserts `holdings["south"] == {"w-0": ["7"]}`.

### `ages(self) -> dict[str, float]`
`now − snapshot.ts` per reached cluster, floored at 0: "the domain's RPO, shown, never hidden". A cluster without a snapshot has `ts = 0` and so an age of roughly `now`.

## Notes
- Refs are compared as strings (`str(row.get("ref", ""))`), which is why `Answer.camera` can be an `int` from the API and a `str` from the console's URL path and both find the same row; `holdings()` returns them as the strings the snapshot carries.
- `test_where_across_three_clusters_from_what_the_clusters_publish` runs three real М11 clusters (`conftest.Running`) and asserts the domain read exactly one object per cluster (`objects.list("vms/snapshot") == ["vms/snapshot"]`) and that south's row has `id == 1` while the domain asked by ref 7.
