# Module 11 — ClusterVMS: Workers That Outlive Their Server

[Module 10](../М10_ServerVMS/README.md) built the platform's shape on one box: a **controller** that is the only writer of configuration, a **worker** — DriverPack — running pipelines against its assignment, an **archive resource** on the box's disks, and the platform's two stores underneath. This module runs that shape across several servers and makes it survive any one of them dying: the worker moves and its cameras go with it, the resource stays and its footage with it, and the controller is not consulted — because failover rewrites nothing.

Five lessons in which a server is pulled from the wall and, within a number of seconds the workers themselves measured, its worker is recording again on another server into another resource — with an edit made *during* the failover already there, because configuration never left the cluster's raft — and when the dead server comes back believing its old worker still owns those cameras, it is fenced twice and the archive is provably intact.

The full design brief is [`module-design.md`](module-design.md); the orchestrator choice and its licence are in [`kubernetes-vs-nomad.md`](kubernetes-vs-nomad.md).

## The thesis

| | Worker | Resource | Controller |
|---|---|---|---|
| What it is | DriverPack with N cameras assigned | the archive on a server's disks; a GPU; a camera-VLAN NIC | the only writer of `vms/*` |
| How many | `N` — Nomad runs it, the Nomad Autoscaler moves it from the workers' own load; **never the controller** | one per eligible server | one — and safe at two |
| Identity | a slot `w-<NOMAD_ALLOC_INDEX>`, **claimed by CAS**: the index is the preference, the Variable is the proof | the server's | none: computation over the stores |
| Moves? | yes — Nomad reschedules it; a replacement claims the same slot and inherits its assignment | **never** | not needed to move anything |
| When it is down | its cameras pause until Nomad brings it back; a *released* slot's cameras are redistributed | that server's footage is unavailable — by name — not lost | edits stop; nothing running stops |

> **The controller writes, the platform stores, the worker reads its share.** Because camera 7 is assigned to worker `w-1` in the cluster's raft rather than to Server A, failover rewrites nothing: Nomad reschedules `w-1` and it reads the same assignment from the same raft. What was on Server A is a resource, and a resource stays.

**Cluster is not domain, and they are different sizes.** A cluster is servers close enough to share a network you would bet recording on — one LAN, one room; that boundary is physics. A domain is clusters under one directory and one signer; that boundary is administration. **A worker fails over within its cluster and never across one**, and [М12](../М12_DomainVMS/README.md) is where several clusters meet.

## Lessons

| # | Lesson | You'll be able to... |
|---|---|---|
| 1 | [When One Box Isn't Enough](01-when-one-box-isnt-enough.md) | Name what forces a second server; argue why a scheduler on a single appliance costs surface and buys nothing; bring up a three-server Nomad cluster with ACLs on; swap М10's file stores for Nomad's and show the platform tests do not notice; measure a worker's capacity. |
| 2 | [Workers, Resources and the Controller as Jobs](02-workers-resources-and-the-controller-as-jobs.md) | Run the five jobs; claim a worker's name from `NOMAD_ALLOC_INDEX` and say why the index is the preference and the CAS the proof; write the `scaling` policy and say who decides `N`; scale out and in and show what the controller does on each — and on a crash; prove one-writer-per-key from inside an allocation. |
| 3 | [What Stays on the Server, and What Does Not](03-what-stays-on-the-server-and-what-does-not.md) | Say what travels (nothing), what stays, what is lost; show an edit during a failover needs no publication; read #12118 rather than trust it; state the storage knob; give a resource a heartbeat and served manifests; merge a timeline across resources and name the unreachable one; put events on the resource for any subsystem, keep the database on each resource, and merge in a console that holds none. |
| 4 | [Failover, and the Two Instances of One Worker](04-failover-and-the-two-instances-of-one-worker.md) | Fix the `disconnect` block; derive the lease margins; pull the power and measure the RTO from the heartbeats; let the old instance wake and show it fenced at the slot and at every epoch with its footage kept; show a reassignment lose the same lease and keep recording; drain for an OS update. |
| 5 | [The Controller](05-the-controller.md) | Place under label constraints with the server in the reason; name the unplaceable; answer *where is camera 7* in one scan; race two controllers under constraints; publish the one object that leaves the cluster; write down what the controller does not decide, and prove it with a second subsystem. |

## The demo the module is built backwards from

Three servers, two workers, two hundred cameras. Then:

```bash
# pull the power on the server running w-1
```

`w-1` reappears on another server within a number of seconds you measured — `vms_failover_seconds{kind="worst"}`, from the workers' own heartbeats — reads its assignment, takes a new epoch for each of its cameras, and records into the archive resource on *its new* server. Footage recorded before the failure stays on the dead server's resource and the console says so — *unavailable on srv-a*, not lost. The edit made during the failover went through the controller into raft and is simply there. When the dead server comes back, its old instance of `w-1` tries to keep writing — **and the archive is intact, provably**: it is fenced at its slot before it touches a camera, every epoch agrees, its segments carry the old epoch, the manifest marks them, and `vms_epoch_conflicts` moved from zero.

## What you can verify without hardware

More than the subject suggests, because the correctness core has nothing to do with video. [`clustervms/`](clustervms/README.md) is the five lessons as one package built on М10's `vmsserver/`, and its 30 tests need no Nomad and no GStreamer: М10's base classes on the raft fake; the slot from the allocation index and the duplicate-index bug resolved at the CAS; scale out and in with the redistribution; the ACL from a worker's identity; the edit during the failover; the timeline across two resources with one silent; events from three subsystems held by each resource's database over its own tree and merged by a console that holds none, a cache that rebuilds to the same answer, and the mirror knob — a copy to the next resource, no store between — keeping it complete while a server is down, and the owner pulling its buckets home; the power pull on a fake clock with its 48 s; the old instance fenced twice; the reassignment that is not a zombie; placement under constraints, the directory in one scan, two controllers racing forty cameras, a worker whose server's resource went silent losing its cameras to one whose resource answers, the snapshot, and the console over real HTTP. Every number in the lessons came out of those tests.

**Needs the bench** — three VMs and Nomad ≥ 1.8.0: cluster formation, `nomad job validate` on the five jobs, the Podman driver, `deploy/verify-bench.sh` (the ACL from inside an allocation; a scale drill), draining, the `disconnect` block, and `deploy/failover-drill.sh` — the power pull itself, three runs, worst case kept.

Four things in this module were checked against the projects' own sources rather than taken on trust, and each lesson says which:

- **Shared storage cannot fail over unattended under Nomad** — [#12118](https://github.com/hashicorp/nomad/issues/12118), open: the volume stays attached to the dead client.
- **The allocation index has had a duplicate-index bug** — [#10727](https://github.com/hashicorp/nomad/issues/10727), fixed; it is why the index is a *preference* and the CAS claim is the identity.
- **A Nomad variable lock's ID is an opaque UUID** — the Locks API; it is the lock Kleppmann's fencing-token argument is about.
- **The Nomad Autoscaler is MPL-2.0** — its `LICENSE` file; it runs as the cluster's fourth job and is the only thing that changes `count`.

[`clustervms-go/`](clustervms-go/README.md) is the Go port of the *first* ClusterVMS design and stays as its measurement record (7.1 MB against 28.5 MB at idle; the CAS race under the race detector). Its port to this shape follows.
