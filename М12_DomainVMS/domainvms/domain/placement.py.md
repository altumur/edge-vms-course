# placement.py — Lesson 1: the domain picks the CLUSTER for a new camera by reachability, stores the decision with a reason under `domain/placement/<camera>` by CAS, and never names a worker

**Role in the module.** Lesson 1, placement at the level above the one М11 built. The docstring's three-line table is the design: Nomad picks the *server* on resources (it knows the servers), the cluster's controller picks the *worker* on capacity (only it sees its workers' headroom — М11 Lesson 10, `SpecController.place`), and the domain picks the *cluster* on **reachability** — a camera on the warehouse VLAN can be reached from the warehouse cluster and from nowhere else; capacity only breaks ties among clusters that can see it at all. Only place when you must: a new camera, or an operator asked. A dead server is not a trigger (Nomad moves the worker; its cameras follow its slot name), and a dead cluster is not a trigger either — its cameras cannot be reached from anywhere else. The decision is *stored*, with a reason and a time, in the domain cluster's Variables under `domain/placement/<camera>`, by check-and-set, so two placers racing are harmless. Depends on `federation.Cluster.reaches` and `Federation.domain_cluster`, and on М11's `Variables` CAS contract (`Conflict`). Its stored decision is what `api.ConsoleAPI.create_camera` receives as `cluster` and what `shadow.Shadow.compare` receives as `placed`. No production process in this package instantiates a `ClusterPlacer` (see the report); the tests do.

## `class CameraSite` (frozen dataclass)
What the placer is told about a new camera.
- `camera: int` — the domain's ref for it.
- `network: str` — where the camera's packets can be seen from, e.g. `"vlan:cctv-b"`; matched against `Cluster.reaches`.
- `load: float = 1.0` — its weight in units of I (М9 Lesson 7); carried but not used by this placer.

## `class ClusterPlacement` (dataclass)
The stored decision, as read back.
- `cluster: str` — the chosen cluster.
- `reason: str` — the sentence stored beside it (`only cluster reaching vlan:a`, `most headroom (40.0) among 2 reaching vlan:b`).
- `at: float` — the placer's clock at the time.
- `rev: int` — the domain-wide placement revision from `_rev()`.

## `class Refused(Exception)`
"No cluster can reach that network — 'the domain cannot place it', never 'cluster X is full'." The two messages are in `place()`.

## `class ClusterPlacer`

### `__init__(self, fed, vars_=None, headroom=None, clock=time.time)`
`fed` is the federation (candidates come from its clusters' `reaches`); `vars_` is where placements are stored — default the domain cluster's Variables (`fed.domain_cluster.vars`, which raises if none is designated); `headroom(cluster_name) -> float` is the cluster's spare capacity *in units of I, from its own placement service* — the docstring: the domain does not measure it (the test comment names the source: each cluster's console exports `vms_headroom`); default is a constant 1.0, i.e. no tie-breaking. `clock` stamps `at`.

### `path(self, camera) -> str` — `domain/placement/<camera>`.

### `current(self, camera) -> ClusterPlacement | None`
Reads the Variable; `None` if absent or without a `cluster` item. Items come back stringified (Nomad Variables are strings), hence the `float()`/`int()` casts.

### `candidates(self, site, unreachable=frozenset()) -> list[Cluster]`
Every cluster whose `reaches` contains `site.network` and whose name is not in the caller's `unreachable` set.

### `place(self, site, unreachable=frozenset()) -> ClusterPlacement`
Place ONE new camera; an existing placement is returned untouched (so calling it again, or after the chosen cluster died, changes nothing — `test_a_dead_cluster_is_not_a_trigger`). No candidates: if some cluster does reach the network but is in `unreachable`, `Refused("… the only cluster(s) reaching vlan:b (south) are unreachable; not placing elsewhere — nothing else can see it")`; if none reaches it at all, `Refused("… no cluster in the domain reaches vlan:z")`. Otherwise the best candidate is the one with the most `headroom`, ties going to the alphabetically first name (`max` over a name-sorted list keeps the first maximum). The reason is `only cluster reaching <net>` when there was one candidate, else `most headroom (<h>) among <n> reaching <net>`. Then `_store`.

### `_store(self, camera, cluster, reason, retries=5) -> ClusterPlacement`
The CAS loop the README calls "correctness from CAS, never from instance count". Up to five times: re-read the path; if somebody placed it meanwhile, return *their* decision (the comment: "somebody placed it while we thought"); otherwise take a revision, `put` `{cluster, reason, at, rev}` with `cas=idx` (0 when absent — create-only), and on `Conflict` loop — "the other placer won; re-read and agree with it". Five conflicts is `RuntimeError`. `test_two_placers_racing_agree_by_cas` runs two threads with opposite headroom preferences over forty cameras and asserts each camera ended in exactly one cluster.

### `_rev(self) -> int`
A domain-wide counter in the Variable `domain/placement` (`{"rev": n}`), bumped by CAS; on `Conflict` it recurses until it wins. Each placement records the value it got, so placements are totally ordered across placers.

### `rebalance_across_clusters(self, *a, **k)`
Always `NotImplementedError`: "a worker never crosses a cluster (М11); the domain moves a camera between clusters only when an operator asks, and then as delete-here/create-there". The method exists so the refusal is stated in code.

## Notes
- `test_the_cluster_then_places_on_a_worker_and_the_domain_never_named_one`: after `place(CameraSite(7, "vlan:b"))` chose south, south's real `ClusterController` places the forwarded camera (`ref=7`, `labels=["vlan:b"]`) on one of its workers with its own reason (`reaching vlan:b … on srv-…`), the directory reports that worker, and the domain cluster's Variables under `domain/placement/` contain only `domain/placement/7` — the domain stored its level and nothing about workers.
- `deploy/signer-policy.hcl` grants `domain/placement` and `domain/placement/*` to the signer job, which implies the placer is meant to run inside the signer process; `signer_service.py` does not do so.
