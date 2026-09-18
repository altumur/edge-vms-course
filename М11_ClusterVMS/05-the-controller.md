# Lesson 5 — The Controller

**Module:** ClusterVMS — workers that outlive their server (Module 11)
**You will build:** the controller as a job — placement by the workers' own capacity under label constraints, with the server in the reason; *where is camera 7* answered in one scan; two controllers agreeing under constraints; the one object that leaves the cluster — and the argument for why it decides neither how many workers there are nor where they run.
**Time:** ~150 minutes.

## Why this lesson exists

Four lessons in, the controller has not been needed once. Failover did not consult it; the old instance was fenced without it; the autoscaler changed `count` without it. That is the design working, and this lesson is where the controller finally does the one job it has — write configuration and decide which *worker* runs which *camera* — under the two constraints a cluster adds: cameras are not reachable from every server, and workers are on servers.

It is also where the temptation to grow the controller is strongest, because a process that sees every heartbeat and every row *could* do so much: heal a silent worker, ask for another, balance at night. The lesson ends with the boundary written down, and the second subsystem as the proof that the boundary is a shape and not a special case.

> **What you can verify without hardware.** `tests/test_lesson5_controller.py`: placement under labels with the unplaceable named, adding a worker moving nothing under constraints, the directory in one scan, two controllers racing forty cameras under constraints, the snapshot, and the console over real HTTP. The bench adds `nomad job scale` and the ACL.

## Prerequisites

- **М10 Lesson 6** — CRUD by CAS, what the controller refuses, placement stored with a reason, the one unasked move, capacity as the worker's word.
- **Lesson 2** — labels from `meta.labels` in the heartbeat; who decides `N`.
- **М12 Lesson 3** (read ahead) — what the domain reads from a cluster: the snapshot and the heartbeats, never the rows.

## Learning objectives

1. Place a camera on a worker whose server can reach it, and store the server in the reason.
2. Name the cameras nothing can reach, with the labels that say why.
3. Answer *where is camera 7* from the cluster in one scan and say why the answer is consistent.
4. Run two controllers at once under constraints and show every camera placed exactly once.
5. Publish the one object that leaves the cluster and say what its age means.
6. State what the controller does not decide, and show it with a second subsystem.

---

## Step 1 — Constraints: cameras are not everywhere

A camera on the `cctv-b` VLAN is reachable from the workers on servers whose NICs are on it. That fact enters the system in two places and meets in the controller. The server declares it — `meta { labels = "vlan:cctv-a,vlan:cctv-b" }` — and the worker reports it in every heartbeat; the operator declares the camera's need — `labels: ["vlan:cctv-b"]`, an operator field in М10's schema — and the spec's `constraint: labels-subset` makes `place()` require the camera's labels to be a subset of the worker's:

```
srv-a: vlan:cctv-a    srv-b: vlan:cctv-a,vlan:cctv-b    srv-c: vlan:cctv-b     capacity 10
cam a  [vlan:cctv-a]           -> w-0 or w-1
cam b  [vlan:cctv-b]           -> w-1 or w-2
cam ab [cctv-a, cctv-b]        -> w-1   "most free capacity (10) among 1 worker(s) reaching vlan:cctv-a,vlan:cctv-b; on srv-b"
cam x  [vlan:cctv-x]           -> None  unplaceable: [{id: 4, labels: [vlan:cctv-x], workers_live: 3}]
```

`test_placement_under_label_constraints`. Two things to notice in the reason. It names the *server*, because at three in the morning *why is camera 812 on w-1* has the answer *because w-1 is on srv-b, and srv-b sees that VLAN* — the worker may move, and the reason records where it was when the decision was made. And the unplaceable list says *why*: not "the system is full" but "nothing live reaches `vlan:cctv-x`", which is a cabling fact and an operator's action, not a capacity fact and a purchase order.

The stability rule survives constraints unchanged — `test_adding_a_worker_moves_nothing_even_with_constraints`: a worker on srv-b arrives, the two cameras on srv-a's worker stay, the third that had nowhere to go lands on the new one.

**Who guarantees a worker runs where an archive is.** Two answers, and they are different guarantees. A worker writes its events into the resource on *its own* server (the recorder, `requires: resource` too, its footage) — nothing requires camera 7's old footage to be where its worker is now; after a failover the footage is on srv-a, the worker on srv-b, and `/timeline` merges the two — so the requirement is only that a worker's server *has* a resource. The first guarantee is the scheduler's: `vmsworker.nomad.hcl` carries `constraint { attribute = "${meta.archive}" operator = "is_set" }`, the same constraint the `resource` and `console` jobs carry, and Nomad will not put a worker allocation on a server that declares no disks. The controller never checks it; it places by capacity and labels and trusts that a worker that heartbeats was put somewhere legal — the 2c division: Nomad decides how many and where, the controller which camera on which worker. But a label is not a disk. A server that declares `meta.archive` and whose disks have died still runs a worker that heartbeats, and that worker has nowhere to write. So the second guarantee is the live fact behind the label, and it is one line in the spec — `placement: requires: resource` — read against the resource heartbeats the platform already keeps (`platform/resources/<server>`, Lesson 3): a worker whose server's resource is *silent* (no heartbeat for 45 s) is not placed on, its cameras are moved off on the next pass with the reason `resource on srv-a silent; most free capacity (…); on srv-b`, and every placement reason ends `, whose resource is live`. A resource that was *never* seen passes — silent is a fact, unknown is not one: a box before its resource process starts, a bench with no resource job. `test_a_worker_runs_where_a_resource_answers_and_leaves_when_it_stops`: srv-a's resource stops heartbeating while its worker keeps going; the worker's two cameras go to srv-b (the one other server that reaches `cctv-a`), a new camera never lands on srv-a, and when the resource returns nothing moves back — adding a place to record moves nothing, like adding a worker. Where the information lives, and how it is configured: the label is `meta { archive = "/data/archive" }` in each server's `client.hcl` (`nomad node status -verbose` shows it), read by the job's `constraint` — and, since the job now sets `ARCHIVE = "${meta.archive}"`, handed to the worker, which records there and reports it in its heartbeat as `archive`; the fact is the resource job's heartbeat under `platform/resources/<server>`. The console puts the two side by side: `GET /servers` lists every server the subsystem runs on with `archive` (the label), `resource` (`live`, `silent`, `unknown` — the fact), its workers with load and capacity, and `placeable` with the reason when not; the page's sidebar shows it, *not placeable: resource on srv-a silent* in red. What this does *not* cover is a worker started by hand on a diskless server outside Nomad; that is М12's, where a worker without a workload identity gets no token at all.

## Step 2 — Capacity is still the worker's

Nothing changed from М10 Lesson 6: `capacity_of(w)` reads what the worker said in its heartbeat, measured on *its* server, and the controller's constructor constant is the fallback for a worker that said nothing. On a cluster this is what makes a mixed fleet work — a worker on a 32 GB server says 80 and one on a 16 GB server says 40 — and the autoscaler scales on the same numbers through `vms_worker_load`. The controller has no model of the servers. It does not need one.

## Step 3 — Where is camera 7, in one scan

`vms/workers/*` is the assignment: written by the controller into one raft, read by every worker from the same raft. Scanning it *is* the cluster directory:

```python
d = Directory(vars, ttl=5.0)
d.where(7) == ctl.where(7)   and d.scans == 1
for i in 1..9: d.where(i)    -> nine answers, still one scan
d.holdings("w-1")            -> [2, 5, 8]
```

`test_where_is_camera_7_in_one_scan`. It is **consistent** because it is one store: every worker read its assignment from the log this scan read. М12 aggregates several of these across clusters through snapshots and heartbeats and *cannot* be consistent — which is why the question is answered here and only summarised there. During a reassignment window `where()` returns `w-1+w-2` rather than pretending: two rows list the camera for a few seconds, and the directory says so.

Why not consistent hashing, since it is what a distributed-systems course teaches for *which node holds key K*: cameras are not uniform (a 4K stream beside a 720p one is eight keys pretending to be one); constraints break the ring (a camera that may only land on `cctv-b` cannot go where the hash says); and *why is 812 on w-1* — because `hash(812) mod ring` — is not an answer an operator can act on. A stored placement with a reason and a revision is a table you can read. It costs one row per camera.

## Step 4 — Two controllers, forty cameras, under constraints

`count = 1` is not exactly-one during a reschedule. `test_two_controllers_agree_under_constraints` runs two controllers placing the same forty cameras concurrently — half of them needing `vlan:cctv-b` — and asserts every camera has exactly one worker, appears in exactly one assignment, and no `cctv-b` camera is on srv-a's worker. The two writes that make it true are М10's: the placement row's `mutate` returns `None` when a worker is already named (the loser adopts the winner's decision), and `assign_add` merges inside the CAS loop. Constraints change which worker wins, not how the race is decided.

## Step 5 — The one object that leaves the cluster

```python
ctl.publish_snapshot()   -> vms/snapshot/w-1  {cluster: "north", worker: "w-1", ts, cameras: [{…row, worker: "w-1", server: "srv-b"}]}
                         -> vms/snapshot/w-2  {…}                  ONE OBJECT PER WORKER (М10A Lesson 25)
                         -> vms/snapshot/unplaced  {…}             the rows no worker holds
```

`test_the_snapshot_is_the_only_thing_that_leaves_the_cluster`. It is sharded by the worker holding the unit, exactly as the heartbeats are, and for the reason М10A Lesson 25 measures: `OBJECTS=variables://objects` here, and a Nomad Variable caps the whole object at 64 KiB, which one object for 600 cameras does not fit under. It is a *copy*: the rows themselves stay in raft with one writer, and М12's read model is built from these objects and the workers' heartbeats, never from the Variables. Its `ts` is what the domain's console shows as the age on every row from this cluster — Lesson 3's point that the RPO moved up a level and became a display age. The controller publishes it every five seconds beside `ensure_placed()` and `redistribute()`; those three calls are the whole of its loop.

## Step 6 — The console

`cluster/console.py` — its own job now (`system`, one on every server and nothing in front — `console.nomad.hcl`; a token for the operator's rows and nothing else; М10 Lesson 7 has the argument) — is the platform's `SpecConsole` over the VMS spec, exactly as М10 runs it, plus the two routes a *cluster* answers differently: the timeline merged across resources and the segment proxied from the server that has it. Registered as extras; nothing subclassed, no new writes:

```
GET /cameras            rows from every heartbeat: phase, server, age, worker_state
GET /where/1            {worker: w-1, reason: "…; on srv-b", directory: w-1, scans: 1}
GET /timeline/7         merged across resources; unreachable named (Lesson 3)
GET /resources          {srv-a: {usage, cameras, state: live|silent}, …}
GET /unplaceable        [{id, labels, workers_live}]
GET /metrics            vms_workers_live 3 · vms_worker_headroom{worker,server} · vms_worker_load · vms_epoch_conflicts
                        vms_failover_seconds{kind="worst"} 48.0 · vms_resources_live · vms_cameras_running
POST /cameras  (Idempotency-Key)   PUT /cameras/1 {"worker": …} -> 400
GET /                   М10's page, unchanged: the list, the merged timeline (each span says which server), playback
GET /segment/<path>?server=srv-a     the bytes, fetched from THAT server's resource job — Range passed through, 503 "unavailable, not lost" when it is silent
```

`test_the_console_over_http` runs it on a real port: the same POST twice makes one camera; the PUT that tries to set `worker` is refused; `/where/1` agrees with the directory; `/metrics` carries the autoscaler's number and the RTO; and the page plays a segment that lives on srv-a's resource through the console, by server. The page is the platform's file, built from `/spec`: the only thing a cluster changed is that a span carries a `server`, and the console proxies the bytes from the resource job that has them rather than reading a disk of its own — the console has no archive, and must not. The detector subsystem's console is the same class over `det.subsystem.yaml` with no extras at all.

## Step 7 — What the controller does not decide

Written as the boundary, because every item on the right has been proposed for a controller somewhere:

| The controller decides | It does not decide — and who does |
|---|---|
| which camera rows exist, and their revision | **how many workers there are** — Nomad runs `count`; the Autoscaler moves it from `vms_worker_load` |
| which worker runs a camera, by capacity under constraints, stored with a reason | **where a worker runs** — Nomad, by constraint and affinity |
| when a *released* slot's cameras move | **when a *silent* worker is dead** — nobody; Nomad brings it back, and `retire` is an operator's word |
| a rebalance, when asked, budgeted | **a rebalance on its own** — never; the stability rule |
| the snapshot for the domain | **which cluster a camera belongs to** — М12; this controller has never heard of another cluster |

Everything on the right would require the controller to hold a model of something outside the stores — the servers, the scheduler's API, a cost policy, a timeout that means *dead* — and М10's two properties (stateless and correct by CAS; never on the recovery path) are exactly what such a model would break.

**The second subsystem** is the proof that the shape is not the VMS's. М10's live and det subsystems ran through the same platform on one box; here the detector subsystem's job files differ from the VMS's by a name, a prefix (`det/*`), and a constraint — `meta.gpu is_set` where the worker's was `meta.archive is_set` — and its controller places detector jobs on the workers that heartbeat from GPU servers with the same `SpecController` code and a different spec. There is no `ClusterController` class any more, and the fact is worth a sentence: the first design had one, adding constraints, the server in the reason and the snapshot to М10's controller — and every one of those turned out to be the one-box behaviour with N = 1 (empty labels, the hostname, a cluster of one). `clustervms/controller.py` is now a one-line import of М10's class. The platform's job is to place processes; the subsystem's is to say what those processes do and which of its units each one runs.

**Deliverable:** *where is camera 7* answered from Variables in one scan on the bench; the placement tests green including the constraint cases; `nomad job scale vmsworker 3` followed by a camera that only the new worker's server can reach, placed there with the server in its reason; and a written copy of Step 7's table for your product, with one row added for the thing your team most recently proposed the controller should do.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Every camera with labels is unplaceable | The workers' heartbeats carry no `labels` — `NOMAD_META_labels` is not set because `meta.labels` is missing from `client.hcl`. |
| A camera lands on a worker whose server cannot reach it | The camera row has no labels. Empty means *anywhere*; the operator must state the need. |
| `/where` disagrees with the directory for a few seconds | A move in flight: two assignments list the camera. The directory says `w-1+w-2` on purpose. |
| Two controllers place a camera on two workers | The placement `mutate` does not return `None` when a worker is already named — it overwrites. |
| The snapshot's `server` is `?` | The placement names a worker that has never heartbeated. Correct: the domain shows the worker and no server until it does. |
| Someone added `heal_silent_workers()` | Delete it, then re-read Lesson 2, Step 4, and Lesson 4, Step 8: a silent worker is Nomad's; a drained one released its slot. |

## Recap

- Placement is by the workers' own capacity, among the workers whose server can reach the camera, with the server in the reason; the unplaceable are named with their labels.
- A worker runs where an archive is because Nomad's `meta.archive` constraint puts it there, and stays placed on only while that server's resource answers: `requires: resource`, a silent resource moves its cameras off, an unknown one passes.
- *Where is camera 7* is one scan of one raft, and consistent because of that; consistent hashing is not inspectable.
- Two controllers agree under constraints for the same reason they agreed without them.
- One object leaves the cluster — a copy with an age.
- The controller decides rows and camera-to-worker placement, and nothing about processes; the second subsystem is the same code with a different prefix and constraint.

## Exercises

1. Add a `priority` affinity: high-priority cameras prefer workers on servers with two NICs. Keep the stability rule and show the property test still passes.
2. Give the console a `POST /move` and defend it: who may call it, what it records, and why it goes through `move()` and never through `assign()`.
3. Cache `workers_seen()` for a minute in the controller and run the two-controller test and the scale-in test. Say which property broke and which test found it.
4. Write the detector subsystem's job files and its `Subsystem("det")` — units, heartbeat status, the epoch key — and diff them against the VMS's.
5. Add the row to Step 7's table for *which cluster a camera belongs to*, then read М12 Lesson 1 and check whether the domain agreed with you.

## Where this is going

A cluster survives any one server, and the controller was never needed for it. [**М12 — DomainVMS**](../М12_DomainVMS/README.md) is where several clusters meet: a directory of directories that cannot be consistent and says so, a signer the clusters trust, users that live somewhere, and the honest sentence when a whole cluster is unreachable — *not anywhere I could reach*, never *not anywhere*.
