# М11_ClusterVMS — Module Design

**Workers that outlive the server running them, resources that do not move, and one controller that is never needed to recover.**

[М10](../М10_ServerVMS/module-design.md) built the platform's shape on one box: a **controller** that is the only writer of configuration, a **worker** — DriverPack — that runs pipelines against its assignment, an **archive resource** on the box's disks, and the platform's two stores underneath, file-backed. This module runs that shape across several servers and makes it survive any one of them dying: the worker moves and its cameras go with it, the resource stays and its footage with it, and the controller is not consulted — because failover rewrites nothing.

The organising decision, carried up from М10: **the controller writes, the platform stores, the worker reads its share.** Put a scheduler under that and a dead server is a relocation of a worker, not a decision anyone has to make.

> **What changed since the first ClusterVMS design.** The first version of this module ([its lessons](README.md), written 8 September) moved М9's *recorder* — a process with its own Postgres and its own disk — between servers, and had to carry its configuration in a published object, rebuild its archive index by scanning, and fence its zombie with an epoch in a filesystem path. Under *2c* three of those four are simpler and one is unchanged. Configuration is already in the cluster's raft, written by the controller, so nothing has to be published to survive a server (the RPO inside a cluster is **zero**). The archive is a resource that never moves, so nothing is rebuilt — the manifest is on the resource beside the footage. The fencing epoch is unchanged, in the key, now per camera. The five lessons and [`clustervms/`](clustervms/README.md) were rewritten to this record on 12 September 2026; `clustervms/` is now built on М10's `vmsserver/` and its 24 tests are the contract.

> **Cluster is not domain, and they are different *sizes*.** A **cluster** is servers close enough to share a network you would bet recording on — one LAN, usually one server room; that boundary is **physics**. A **domain** is the clusters under one directory, one CA and one set of operators; that boundary is **administration**. The rule that follows: **a worker fails over *within* its cluster and never across one.** Its resources are on that cluster's servers, its epoch comes from that cluster's raft, and [М12](../М12_DomainVMS/module-design.md)'s job when a whole cluster dies is to say what it took, not to heal it.

---

## The thesis

Three kinds of thing, and Nomad has a shape for each:

| | What | Nomad shape | Identity | When it is down |
|---|---|---|---|---|
| **Workers** (1+ per cluster, by workload) | `vmsworker`: DriverPack with N cameras assigned; holds the pipelines and routes them — closed segments to an archive resource, a tee to the live gateway, a tee to detectors | `service` jobs, movable, placed by constraint and affinity | **stable per worker** — a name in a Variable, never an allocation index | Nomad reschedules it; the epoch and the lease make the restart harmless; its cameras come with it because the assignment is on its name |
| **Resources** (N per cluster) | the archive on a server's disks; GPU compute for detectors; a NIC on the camera VLAN | `system` jobs — one per eligible server, pinned by what the server has | **the server's** | *that server's* footage is unavailable, *that server's* detectors stop; nothing moves, because nothing can |
| **One controller** | `vmscontroller`: the only writer of `vms/*` — cameras, assignment of cameras to workers, rebalance on request | one `service` job, `count = 1`, **safe at two** | none: computation over the cluster's stores | no edits, no new cameras, no rebalance; recording, failover and live view unaffected |

Because camera 7 is assigned to worker `w-3` rather than to Server A, **failover rewrites nothing**: Nomad reschedules `w-3` onto Server B and `w-3` reads its assignment from the same raft it read it from before. The question the module actually answers is narrower: **what is on Server A that `w-3` needed, and what happens to it?**

### The demo it is built backwards from

Three servers, two workers, two hundred cameras. Then:

```
# pull the power on the server running w-3
```

`w-3` reappears on another server within a number of seconds you measured, reads its assignment, takes a new epoch for each of its cameras, and records into the archive resource on *its new* server. Footage recorded before the failure stays on the dead server's resource and the console says so — *unavailable*, not lost. Edits made during the failover went through the controller into raft and are simply there when `w-3` returns. When the dead server comes back, its old instance of `w-3` tries to keep writing — **and the archive is intact, provably**: its segments carry the old epoch, the manifest names both, and `vms_epoch_conflicts` moved from zero.

---

## Decisions taken

| Decision | Choice | Why |
|---|---|---|
| The cluster's shape | **Workers, resources, one controller — 2c** *(12 September 2026)* | The unit was М9's recorder, bound to its disk with its own database. Split by what moves and what cannot: workers move, resources do not, the controller is not needed to move anything. [ARCHITECTURE §1.11](../ARCHITECTURE.md) has the row-by-row assignment. |
| Camera ownership | **An assignment on a worker's stable name**, written by the controller, read by the worker | Failover moves the worker; the assignment is in raft under its name; nothing is rewritten. |
| Worker identity | **A slot in a Nomad Variable, claimed by CAS** (`vms/slots/<name>`, М10's identity by claim); the allocation index is the *preference* the claim starts from, never the proof *(12 September 2026)* | Variables survive rescheduling; the claim is what makes the index safe to use at all. If two allocations ever carry one index — the documented bug — the CAS decides and the loser fences on its next renewal. A replacement of index 3 takes slot `w-3` outright and inherits its assignment; nothing is rewritten and nobody is asked. |
| Who decides how many workers, and where | **Nomad places them; the Nomad Autoscaler sets `count`** from a `scaling` policy on the worker job whose metric is the workers' own load (`vms_worker_load` = assigned ÷ capacity, from the heartbeats via the console's `/metrics`), **never the controller** *(12 September 2026)* | Placement of processes onto servers is the platform's (ARCHITECTURE §1.11): constraints, affinities, resources, draining, and now the count. The controller has no Nomad client; it places cameras onto the workers it sees. CPU is the wrong metric — a worker with two hundred idle cameras at 03:00 is full. The Autoscaler is a separate agent, MPL-2.0, run as a fourth cluster-level job. |
| Scale-in | **A worker stopped by the scheduler releases its slot (`released: true` on SIGTERM, inside `kill_timeout`); the controller's placement pass redistributes that slot's cameras to the workers that remain — its one unasked move.** A slot that merely lapses (a crash, a drain still in flight) is left alone: Nomad brings the process back under the same index | The word in the row is what tells scale-in from a crash; without it the controller would have to infer from a silence, which is the healing it must never do. `retire(slot)` is the operator's statement for a process that will not return. |
| Configuration authority | **The controller, and only the controller, by CAS into the cluster's Variables** | One writer per key, one level up. Edits are consistent because the store is one raft; the controller's outage stops edits and nothing else. М12's domain reads; it never writes into a cluster. |
| What travels on failover | **Nothing.** The assignment and the camera rows are already replicated to every server by raft | The first design published a configuration object on every change and measured its RPO. Under 2c the RPO inside a cluster is zero by construction. What М12 still publishes upward is a *snapshot* for the domain's read model, and that has an RPO — the domain's, not the cluster's. |
| The archive | **A resource: server-bound, unreplicated by default, with a manifest beside the footage** | Footage locality kept; no quorum that can stop every camera at once; the manifest is the index and needs no rebuild. An erasure-coded pool is the option for a customer who buys durability over locality and has the network (*the storage knob* below). |
| Fencing | **At the resource, not at the controller: an epoch per camera in every key, issued by CAS; a lease per worker on a monotonic clock** | You cannot stop a zombie from writing; you can make its writes land where nobody reads and be identifiable afterwards. Per camera, because a reassignment is the one legitimate two-writer window and the epoch must separate those too. |
| Epoch issuer | **A Nomad Variable with check-and-set** | Atomic, monotonic, raft-replicated. Nomad's *variable lock* is the trap: its lock ID is an opaque UUID, not a fencing token. |
| Lease numbers | **TTL 30 s, margin 5 s, `stop_on_client_after` 25 s, `lost_after` 45 s** | Nomad and the lease arithmetic agree on when a replacement may start; two-writer window ≤ 10 s on a partition, zero on a pause; tolerates a 29 % clock-rate error. Lesson 4 carries the reasoning, unchanged. |
| A fenced instance's footage | **Kept, with its epoch in the key; the manifest marks it *recorded by a fenced instance*** | Real footage of the partition minute; the epoch that made it harmless makes it identifiable. |
| The controller's two properties | **Stateless and correct by CAS; never on the recovery path** | Safe at two instances during a reschedule; a worker restarts without asking it. The moment it holds state or a worker needs it to restart, the cluster has grown the thing М12 spent a module dissolving. |
| Object store | **No MinIO. Heartbeats and the domain snapshot are Variables under `objects/…` through М10's `ObjectStore` contract** *(12 September 2026)* | 1.5 writes/s of 10 KB across a cluster is not the load the 64 KiB-and-raft rule was about; MinIO in the first design's job was one single-drive instance per server — three stores, not one — and fixing it meant a fourth quorum with its own credentials. What brings a store back is volume (a hundred workers, heartbeats carrying thumbnails), and the code would not change to take it. |
| The resource job | **The platform's** (`vmsplatform/resource.py`, job `resource`): heartbeat under `platform/resources/<server>`, every subsystem's buckets served, the mirror and restore, retention by each subsystem's `<sub>/retention[/<unit>]` rows; a subsystem registers its own pass — the VMS's `ArchivePolicy` and its `/manifest`, `/segment` routes *(12 September 2026)* | The mechanism was platform-generic and the names were not: `vmsarchive`, `vms/resources`, `vms/mirror`. A `system` job that serves, mirrors and retains buckets for every subsystem is the resource, and only its media half is the VMS's. The controller writes `vms/retention/<cam>` and nothing else crosses. |
| Orchestrator | **Nomad** | Non-container workloads (`exec2`, `virt`), Podman kept as the runtime, `system` jobs for resources, a far smaller surface. See [`kubernetes-vs-nomad.md`](kubernetes-vs-nomad.md). |
| Minimum version | **Nomad ≥ 1.8.0; target 1.10.x LTS or 2.0.x** | The `disconnect` block arrived in 1.8.0 and its predecessors were removed in 1.10.0. |
| Failover scope | **Within a cluster. A worker never crosses one** | Its resources are on the cluster's servers and its epoch comes from the cluster's raft — two independent reasons landing on one boundary. |
| Language | **Designed in Python, shipped in Go and C++** | The platform, the controller and the worker's loop port to Go — [`vmsserver-go/`](../М10_ServerVMS/vmsserver-go/README.md) and [`clustervms-go/`](clustervms-go/README.md), 70 tests between them, 8.9 MB vs 21.2 MB at idle; the worker's pipeline is DriverPack's C++. The contract is what both keep. |
| Events | **On the resource, in per-unit buckets under each subsystem's prefix, written by the worker holding the unit's epoch (М10); indexed across the cluster by `eventindex`, a `count = 1` job holding a SQLite table it rebuilds from every resource's buckets for every subsystem it finds — a cache, never a writer of record; subsystems joined on a `cam` field, fencing compared per `(subsystem, unit)`** *(12 September 2026)* | The obvious design — one event controller writing a database replicated on two servers — fails the module's own rules twice: a single writer of observations is on the hot path and the recovery path, and a two-server replicated database with automatic promotion is the zombie writer one layer down (two cannot hold a quorum; a promotion is a decision made on a silence). A cache that fails over rebuilds in seconds and says *catching up*; a silent resource makes its answer incomplete by name, not wrong. |
| Events, replicated | **Off by default; the storage knob's events row, one Variable `platform/mirror {enabled, copies}`: each resource's policy pass copies its *closed* buckets — any subsystem — to the next live resource(s) after it in sorted order, under `.mirror/<its name>/` on the peer's disks, exactly once; a silent server's rows come from its peer, marked *from mirror*; a server back with an empty disk pulls its own buckets home (`restore`); the open bucket is the RPO** *(12 September 2026, `test_the_events_knob_is_a_peer_copy_and_the_owner_restores`)* | No store in between: a copy on one other server is what *survive one server* costs for kilobytes, and the threshold — two servers lost, one server's events gone — is the cluster's own. The first draft mirrored to MinIO; *why not just copy the events to another server* had no good answer. | Events are kilobytes per camera per minute — footage's 800 Mbit/s argument does not apply — and an RPO of the mirror lag on *observations* is acceptable where an RPO on configuration was not. When there are consumers (SIEM, rules, cloud), NATS JetStream (Apache-2.0, R=3) is the on-prem stream and the index is one more consumer. |
| Single-server deployments | **М10's shape with `systemd` instead of Nomad** | One box has nothing to schedule; the same controller, worker and resource run as units. Lesson 1 makes students argue it. |

The layer above — which cluster a camera goes to, the directory of directories, who may call the API — is [М12's](../М12_DomainVMS/module-design.md), and nothing here is allowed to contradict it.

---

## Prerequisites

- **М10 entire.** The controller, the worker, the resource and the platform's two stores on one box; the subsystem contract. This module changes the implementation of the stores and adds a scheduler; it changes nothing in the contract.
- **М9 Lessons 1–4** — the appliance: Quadlet units become jobs; the spool's discipline becomes the archive's.
- **М9 Lessons 6–8** — the loop, the failures, the numbers; they are the worker's tests.
- **М8 Lesson 2** — signals. `kill -STOP` is still the module's most important teaching device.

---

## What must outlive a server

Worker `w-3` runs on Server A. Server A dies. Nomad places `w-3` on Server B. What was on Server A, and what happens to each thing?

| On Server A | Under the recorder model | Under 2c |
|---|---|---|
| **Configuration** — cameras, assignment | in the recorder's Postgres; published upward; restored from an object with an RPO | **in raft already** — the controller wrote it there; `w-3` reads it on Server B. Nothing travels; the RPO is zero |
| **Footage** | on the dead disk; stays; the replacement records the future | **on the dead resource; stays; unavailable until the server returns** — the console says *unavailable*, never *lost*. `w-3` records into Server B's resource from its first segment |
| **The index** | in the dead Postgres; rebuilt by scanning segments when the server returns | **the manifest, on the resource beside its footage** — it returns with the server; nothing to rebuild. A camera's timeline spans two resources and the console merges two manifests |
| **The open segment** | in the spool; lost up to one segment length | in the spool; lost up to one segment length — unchanged, and the number is the segment length (М9 Lesson 4) |
| **Events** | in the dead Postgres | **on the dead resource beside their segments** — unavailable with the footage, not lost; the cluster's `eventindex` says which server is silent, and re-indexes when it returns |
| **Identity and the epoch** | in Variables | in Variables — unchanged |

So the answer to *what must travel* is **nothing**, and the answer to *what is lost* is **the open segment and access to old footage until the server returns.** That second thing is the honest cost of unreplicated resources, and it is a knob:

### The storage knob

| Archive resource | Survives | Costs | Fits |
|---|---|---|---|
| **per server, unreplicated** (one MinIO in single-drive mode, or a directory, per box) | a dead server's footage is unavailable until it returns; every other camera records on | nothing new: footage locality kept; a manifest that may span resources | **the default**, every cluster size |
| **erasure-coded pool across servers** | a dead server's footage is still readable | every recorded byte crosses the LAN — 200 cameras × 4 Mbit/s = 800 Mbit/s east-west plus parity, so 10 GbE between tiers is a requirement; ≥ 4 drives across servers; **loss of write quorum stops every camera at once** — a correlated failure domain the unreplicated form does not have | a customer who buys durability over locality and has the network |

### Why the resource is not a volume that follows the worker

The tempting shortcut is a CSI volume: attach the archive to whichever server runs `w-3`, and failover carries the disk. [Nomad issue #12118](https://github.com/hashicorp/nomad/issues/12118) — still open — is why not: when a client holding a CSI volume dies, the volume stays attached to it, the rescheduled allocation fails to place, and the documented workaround is a human at the storage provider's console. Shared storage buys fencing and loses the unattended recovery it was adopted for. A resource that *stays* has no such problem, because nothing tries to move it.

### The acknowledgement problem, dissolved

The first design had to explain what the operator is told when they save a camera, because the recorder acknowledged on local commit and published later. Under 2c the controller acknowledges **after the CAS commit into raft**, which is replicated to every server before it returns. Inside the cluster there is no *saved · not yet replicated* — the write is either in the cluster's raft or it was refused. The condition survives one level up: М12's read model is built from snapshots the workers publish, and *that* copy is stale by the heartbeat interval, which the console prints on every row. The RPO moved from the cluster to the domain, and shrank to a display age.

### What stays open by design

- **A worker with no assignment** comes up idle and reports *unassigned*. It invents nothing; the controller assigns to it when there is work.
- **Old footage on a dead resource is unavailable, not lost.** Playback of the ranges on Server A's manifest waits for Server A; the console says which ranges and which server.

---

## The zombie writer

The correctness core, unchanged in mechanism and sharper in scope: **both writers are the same worker, and the resource is what refuses the stale one.**

`w-3` runs on Server A, writing camera 7. Server A is partitioned — not dead, still reaching its cameras and its disks. Nomad concludes `w-3` is lost and starts `w-3′` on Server B. Both are `w-3`. Both read the same assignment. Both believe they own camera 7.

**Nothing can tell dead from partitioned from paused.** That is not a gap to close with a better heartbeat; the design must be correct without resolving it.

### Why this cannot be solved by agreement

> **Two writers to one video stream cannot be merged. There is no reconciliation function for footage.**

So the epoch must be a fencing token from a **single issuer**, not a value each instance computes from what it believes about the cluster.

### Why a lock is not enough

Kleppmann's argument applies directly: a lock service cannot prevent a client whose lease expired during a pause from making unsafe changes, because it has no visibility into what the client does. The fix is a **fencing token**, and the essential part is *where it is checked*: **the resource must reject the stale token**, not the lock service.

### The VMS version, which is unusually clean

Make the epoch part of the key:

```
archive/cam-7/e000005/20260912T101000Z.mp4    <- the old instance of w-3
archive/cam-7/e000006/20260912T101000Z.mp4    <- the live one, on another resource
```

The old instance cannot corrupt the new one's segments because **it cannot name them.** It writes valid files under an epoch the manifest marks as fenced — identifiable afterwards, kept, shown as *recorded by a fenced instance*. Per camera rather than per worker, because the same window opens on a *reassignment*: the controller moves camera 7 from `w-3` to `w-4`, and for a few seconds both may write — into different epochs, harmlessly.

> **You cannot stop a zombie from writing. You can only make its writes harmless.**

### Clocks

Lease expiry must not depend on wall-clock time. The holder measures its lease with a **monotonic** clock; it **stops writing at TTL − margin**; a replacement **starts at TTL + margin**; safety depends on the two margins and on relative clock *rates*, never on two servers agreeing what time it is. The holder stopping is a purely local decision — which is precisely why it is the part that can be trusted.

### Where the token comes from

**Not a Nomad variable lock.** Its lock ID is an opaque UUID with no monotonic index — exactly the lock Kleppmann's argument is about, and the most instructive wrong answer in the module.

**A Nomad Variable with check-and-set.** `PUT var vms/epoch/cam-7 {6} cas=8123` → 200 if nobody else wrote, 409 otherwise. Atomic, single-issuer, monotonic, replicated to every server. And it adds no coupling: failover already requires Nomad, because Nomad is what reschedules the worker.

**Why this beats a sequence in a database** — and why there is no database. A sequence reissues numbers after a restore from backup, and the recovery procedure corrupts the archive. Raft cannot lose the counter without losing the cluster, and if the cluster is gone there is nothing to fail over.

Which generalises into the rule the module stores things by, now with the third store named honestly:

| | Holds | Shape | Why not one of the others |
|---|---|---|---|
| **Nomad Variables** | worker identity and assignment, camera rows, the epoch per camera, placement | small, rare, **must be consistent** — one raft | memory-resident and replicated to every server, so it must stay small; a thousand camera rows are a few hundred kilobytes and fit |
| **The object store** — on this cluster, Variables under `objects/…` | worker and resource heartbeats; the snapshot for the domain's read model | frequent, never queried by key, and *small*: a dozen 10 KB objects every ten seconds | a contract, not a second system: `VariablesObjectStore` here, S3 (`s3.py`) when a cluster outgrows this or is rented; nothing in `vms/` knows which. The first design's MinIO is gone from the module *(12 September 2026)* |
| **The archive resource**, per server | footage and its manifest under `vms/<cam>/`, and every subsystem's event buckets under its own prefix | large, constantly written, read as a range; events small and beside what they describe | it does not move, and that is the point; an index over the events is a cache the cluster can lose |

> **Small and consistent goes in the scheduler's store. Large and opaque goes in an object store. Bulk that is read as a range stays on the server that wrote it.** The database that held all three in М9 held them because it was the only store.

---

## The controller

The placement service of the first design, with a name, two properties and one more job.

**It is the only writer of `vms/*`.** Cameras (`vms/cameras/<id>`), assignment (`vms/workers/<worker>`), the placement revision. The console's write API is its client; М12's domain reads its rows and never writes them. Its ACL policy is `deploy/vmscontroller-policy.hcl`: write on `vms/*`, read on everything else — and a worker's policy is read on `vms/*`, write on its own heartbeat prefix in the object store and on `vms/epoch/*` (by CAS, when it starts a camera).

**Placement onto workers is by measured capacity**, from М9 Lesson 7's `B + n·I` — the probe is why this is observed rather than guessed — under label constraints (a camera on an isolated VLAN is reachable from the workers on servers that see it). **The stability rule, with a property test:** *adding a worker moves nothing.* Every camera lands on exactly one worker; no constraint violated; the tidy rebalance that every invariant lets through is the test that fails first. **Store the placement, do not derive it**: at 3 am *why is camera 812 on w-3* is a row with a reason and a timestamp. **Rebalance is explicit** — budgeted, observable, interruptible, with a dead band — and it is the one two-writer operation in the module, which is why each move takes a new epoch.

**It never decides how many workers there are, or where.** Nomad places the worker job under its constraints; the Nomad Autoscaler moves `count` from `vms_worker_load`; the controller has no Nomad client and places cameras on the workers it sees heartbeating. Its one unasked move is the mirror of that: when a worker stopped by the scheduler *releases* its slot, the next placement pass moves that slot's cameras to the workers that remain, each move with a reason that names the slot. A slot that merely lapsed is not touched — the process is coming back under the same index with its assignment intact, and moving its cameras would be the healing this controller does not do.

The `scaling` block on the worker job is the whole of the VMS's say in the matter — a number it exports and two bounds an operator sets:

```hcl
group "vmsworker" {
  count = 2
  scaling {
    min = 1
    max = 12                                   # the servers' budget, from М9 Lesson 7's B + n·I
    policy {
      cooldown            = "5m"               # longer than a failover, so a reschedule is not read as demand
      evaluation_interval = "1m"
      check "load" {
        source = "prometheus"
        query  = "avg(vms_worker_load)"        # assigned ÷ capacity, from the heartbeats — never CPU
        strategy "target-value" { target = 0.9 }
      }
    }
  }
}
```

**Its two properties**, restated because everything depends on them: it is **stateless and correct by CAS**, so `count = 1` is a preference and not a correctness requirement; it is **never on the recovery path**, so a worker that restarts on a new server reads its assignment and asks nobody. When the controller is down, the console can still show every camera (from heartbeats), a worker can still fail over, and the only thing an operator cannot do is change something.

**The second subsystem shows it is a shape, not a special case.** Detectors are `detectorcontroller` and `detectorworker` over the same platform: a config prefix `det/*`, an assignment per worker, a heartbeat, and a resource — GPU compute — that pins the worker by affinity. The platform's job files differ in a name and a prefix. Lesson 5 builds enough of it to prove that, and М12's gateway is the third.

---

## Lessons

*Five lessons. Several servers, one scheduler — and the hard part is still what stays, not what moves.*

### Lesson 1 — When one box isn't enough

- What actually forces a second server: camera count, storage throughput, retention, availability
- **Why an orchestrator is the wrong answer for a single appliance** — М10's controller, worker and resource run as `systemd` units on one box; "place N workers" is a template unit; Nomad's own production guidance sizes *servers* and says nothing about single-node deployments
- Nomad's model: servers accept jobs and place work, clients execute it; raft per region; three or five servers
- **The platform's stores become Nomad's.** М10's file-backed Variables become Nomad Variables with the same `ModifyIndex` and CAS; the object directory becomes MinIO; nothing in `vms/` changes — that is the test
- Build a cluster: three servers, two clients; run [`shard-memory-probe.py`](../М9_EdgeVMS/reference/shard-memory-probe.py) and derive the worker's camera budget

**Deliverable:** a working cluster, М10's tests green against Nomad's stores instead of files, a measured per-worker budget, and a written justification for why this deployment needed a scheduler.

---

### Lesson 2 — Workers, resources and the controller as jobs

- Jobspec structure; the Podman task driver; `exec2` and `virt`, and what `exec2` demands of the OS (Landlock, cgroups v2; a plugin the image must carry)
- **Four shapes**: the worker as a `service` job with `count = N` and a `scaling` block; the archive as a `system` job pinned to servers with disks (`meta.archive`); the controller as a `service` job with `count = 1`; the Nomad Autoscaler as a `service` job with `count = 1` reading the console's `/metrics`
- **Identity by claim.** A worker claims `vms/slots/w-<NOMAD_ALLOC_INDEX>` by CAS and renews it; the index is the preference, the Variable is the proof — if two allocations ever carry one index, the CAS decides and the loser fences. A worker reads *what am I assigned* from `vms/workers/<name>`
- **Who decides `N`.** The `scaling` policy: `min`, `max`, `target-value` on `avg(vms_worker_load)` at 0.9, `cooldown` longer than a failover; why the metric is assigned ÷ capacity and not CPU; what the controller does on scale-in (redistribute the released slot) and on a crash (nothing)
- **What goes in Variables and what does not.** Camera rows do (small, consistent, one writer). Footage, heartbeats and snapshots do not (the 64 KiB cap exists *"to reduce the potential performance impact of Variables on our raft store"* — the maintainers stating the rule)
- Placement constraints and affinities: cameras are not uniformly reachable; workers prefer the servers that carry their resources
- **The ACL that makes one-writer-per-key true**: the controller writes `vms/*`; a worker writes its epochs and nothing else; `deploy/verify-bench.sh` proves both

**Deliverable:** М10's controller, worker and resource running as jobs with the behaviour they had under `systemd`, a worker identity that survives being rescheduled, `nomad job scale vmsworker 3` → the new slot claimed and the next camera placed on it, `… 2` → the released slot's cameras redistributed within one placement pass, and the ACL proven from inside an allocation.

---

### Lesson 3 — What stays on the server, and what does not

- The table above: configuration is already in raft; footage stays on the resource; the manifest returns with it; the open segment is the loss
- **Why the resource is not a volume that follows the worker** — read issue #12118 rather than take it on trust
- **The storage knob**: per-server by default, an EC pool by choice; the 800 Mbit/s arithmetic and the quorum that stops every camera
- **The acknowledgement problem, dissolved**: the controller acknowledges after the CAS commit; the RPO inside the cluster is zero; what remains is the domain's read model and its display age
- A camera's timeline across two resources: merging manifests, and what the console shows while one resource is unreachable
- **Events: the database that is a cache.** On the resource beside the segment; `eventindex` rebuilt from the resources; why not an event controller over a two-server database; the storage knob's events row
- A worker with no assignment invents nothing

**Deliverable:** kill the server under `w-3`; show it recording on another server into another resource within the measured time, with the edit made *during* the failover present when it returns; show old footage as *unavailable* with the server named; bring the server back and play across the boundary.

---

### Lesson 4 — Failover, and the two instances of one worker

> **The two numbers this lesson exports.** `vms_failover_seconds` — power pulled to recording resumed — is the product's RTO, reported as the worst case. `vms_epoch_conflicts` counts a stale instance fenced at the resource; zero forever on a healthy cluster, alarmed on anyway.

- Restart versus reschedule; the `disconnect` block and why its default is wrong for a recorder — `lost_after`, `replace`, `stop_on_client_after`, the four `reconcile` strategies
- **Fencing at the resource**: the epoch per camera in the key, monotonic clocks, the two margins; the numbers this course ships (30 / 5 / 25 / 45) and why
- **Where the epoch comes from, and the wrong answer first**: the variable lock's opaque UUID, then a Variable with `cas`
- **The reassignment window** is the same window: the controller moves a camera and two workers may write for seconds — into different epochs
- Planned failover: draining a client before an OS update — М9's two update planes meet the scheduler
- What does not fail over: the resource

**Deliverable:** pull the power on a server; report the worst-case failover time over three runs and the seconds lost. Restore it, let the old instance wake, and prove the archive intact and its output fenced — then do the same with a reassignment instead of a failure.

---

### Lesson 5 — The controller

- The only writer: cameras and assignment by CAS; the console's write API as its client; what it refuses
- **Placement by measured capacity** under label constraints; the stability rule as a property test; the tidy rebalance that breaks it; why not consistent hashing
- **Store the placement, do not derive it**; rebalance budgeted, observable, interruptible, with a dead band — and an epoch per move
- **Two controllers at once**, and why nothing breaks; **the controller stopped**, and what still works (everything already running)
- **The cluster directory is the assignment.** `vms/workers/*` scanned answers *where is camera 7* in one raft, consistently — М12 aggregates several of these and cannot be consistent
- **The second subsystem**: `detectorworker` over the same platform, with a GPU resource and an affinity; the diff between the two subsystems' job files

**Deliverable:** *where is camera 7* answered from the cluster in one scan; the placement property tests; two controllers racing to place forty cameras and agreeing on all of them; and a detector worker placed by the same code with a different prefix.

---

## Verification plan

**Track 1 — in the authoring sandbox.** The controller's placement and assignment with two racing writers; the worker's reconcile loop over an assignment with a fake actuator (М9's tests); the lease and the epoch issuer's CAS; the manifest merge across two resources; the acknowledgement change (an edit lands in the store or is refused, never *pending*); the second subsystem through the same platform code. `clustervms/` and `clustervms-go/` already carry the fencing, lease, publish and placement tests; the rewrite moves them under `vms/` and `platform/` from М10 and keeps their meaning.

**Track 2 — needs the bench** (three VMs, Nomad ≥ 1.8.0, Podman, MinIO, the Nomad Autoscaler): cluster formation, `nomad job validate` for the four job shapes, `nomad job scale` out and in against the slot claim and the redistribution, the `system` job pinned to servers with `meta.archive`, the ACL from inside an allocation (`deploy/verify-bench.sh`), draining, the `disconnect` block, and the power pull with the failover drill (`deploy/failover-drill.sh`).

---

## Open questions

1. ~~Is 2a ever right?~~ — **No.** The resource stays; a CSI volume that follows the worker is #12118.
2. **Can a task write Variables under workload identity, scoped by ACL to its own prefix?** `verify-bench.sh` tests it; needs the bench. Load-bearing: the controller's monopoly on `vms/*` and a worker's on its own epochs rest on it.
3. ~~How long before a worker's old instance is assumed gone?~~ — **Decided:** 30 / 5 / 25 / 45, reasoning in Lesson 4.
4. ~~The rewrite order.~~ — **Done (12 September 2026).** `clustervms/` is the Nomad implementations of `vmsplatform` plus the cluster's additions (constraints, the resource's heartbeat and served manifests, the merged timeline, the snapshot); the five lessons follow this record. `clustervms-go/` was ported to 2c the same day, on a new `vmsserver-go/`, with both suites and the measurement redone.
5. ~~Who changes the worker count.~~ — **Decided (12 September 2026): the Nomad Autoscaler, from the workers' own load; the controller never.** Rows *Who decides how many workers* and *Scale-in* above; the identity-by-claim mechanism is М10's.

---

## Sources

- [Nomad architecture](https://developer.hashicorp.com/nomad/docs/architecture) · [task drivers](https://developer.hashicorp.com/nomad/plugins/drivers) · [`system` jobs](https://developer.hashicorp.com/nomad/docs/job-specification/job#type) · [affinity](https://developer.hashicorp.com/nomad/docs/job-specification/affinity)
- [Nomad `disconnect` block](https://developer.hashicorp.com/nomad/docs/job-specification/disconnect) · [rescheduling](https://developer.hashicorp.com/nomad/docs/job-declare/failure/reschedule)
- [`NOMAD_ALLOC_INDEX` uniqueness bug](https://github.com/hashicorp/nomad/issues/10727) · [#4264](https://github.com/hashicorp/nomad/issues/4264) · [#11628](https://github.com/hashicorp/nomad/issues/11628)
- [CSI volumes do not recover from client failure without human intervention](https://github.com/hashicorp/nomad/issues/12118)
- [Nomad Variables HTTP API](https://developer.hashicorp.com/nomad/api-docs/variables/variables) — `cas` against `ModifyIndex`, 409 on conflict, the 64 KiB item limit · [Variable Locks](https://developer.hashicorp.com/nomad/api-docs/variables/locks) — an opaque lock ID rather than a fencing token
- [How to do distributed locking](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html)
- [`kubernetes-vs-nomad.md`](kubernetes-vs-nomad.md) · [`apphost-and-process-model.md`](../М9_EdgeVMS/apphost-and-process-model.md) · [М10's design](../М10_ServerVMS/module-design.md) · [ARCHITECTURE §1.11](../ARCHITECTURE.md)

*Written 5 September 2026 for the recorder model; lessons written 8 September. Rewritten 12 September 2026 to workers, resources and one controller (*2c*), after the fold of the М9's recorder design into М9 and the new М10 design. The lessons and the code still describe the recorder until they are rewritten to this record.*
