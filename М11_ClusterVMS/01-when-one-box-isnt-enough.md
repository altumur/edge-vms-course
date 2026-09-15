# Lesson 1 — When One Box Isn't Enough

**Module:** ClusterVMS — workers that outlive their server (Module 11)
**You will build:** a three-server Nomad cluster with ACLs on and a data partition under raft, М10's platform tests running against Nomad's stores instead of files, and a measured per-worker budget — plus a written justification for why this deployment needed a scheduler at all.
**Time:** ~150 minutes (60 without the bench).

## Why this lesson exists

М10 ended with the platform's shape on one box: a controller that is the only writer, a worker that is DriverPack, an archive resource, and two stores underneath — a directory of JSON files with a `ModifyIndex`, and a directory of objects. Every test in that module passed against files. This module's first claim is that **nothing in `vms/` changes** when the files become a raft — both of them: the config store as Variables, and the object store as Variables too, at this size — and the first thing to do is prove it, because everything after depends on it.

The second claim is about restraint. The instinct after a few years near Kubernetes is that a scheduler is simply how things are run now. On one box it is a second supervisor over the same processes, memory taken from page cache, and a new way to orphan a container. The scheduler arrives with the second server, because that is when there is first a decision to make — *which* server — and this lesson makes you write down what forced that decision before you touch `nomad agent`.

> **What you can verify without hardware.** The argument, the arithmetic, and the store swap: `tests/test_lesson1_stores.py` runs М10's base classes against the cluster's Variables fake — the same CAS, the same ACL, the same epoch issuer under four threads. Building the cluster needs three VMs from the М9 bench (or three of anything with a Linux kernel and Podman). Nothing in this lesson needs a camera.

## Prerequisites

- **М10 Lesson 1** — the contract: `Variables` with `get/put(cas)/list`, `ObjectStore` with `put/get/list`, the `Controller` and `Worker` bases.
- **М9 Lesson 4** — the appliance's data partition; `/data` survives an A/B update and raft is going to live there.
- **М9 Lesson 7** — `B + n·I`, which is the number a worker's `CAPACITY` comes from.
- [`kubernetes-vs-nomad.md`](kubernetes-vs-nomad.md) — why Nomad, and its licence.

## Learning objectives

1. Name the four pressures that force a second server and say which one actually needs a scheduler.
2. Argue why an orchestrator on a single appliance costs surface and buys nothing.
3. Describe Nomad's server/client model and why three or five servers, never two or four.
4. Bring up a three-server cluster with ACLs on and raft on the data partition.
5. Swap М10's file stores for Nomad's and show the platform tests do not notice.
6. Measure a worker's baseline and increment and derive its capacity.

---

## Step 1 — What actually forces a second server

Four things, and it is worth being precise because people reach for a cluster for the wrong one.

| Pressure | What runs out | Cluster or bigger box? |
|---|---|---|
| **Camera count** | CPU and memory for pipelines — `B + n·I` | A bigger box, for a long time. Fifty pipelines in one worker is cheap; a server runs several workers |
| **Storage throughput** | disk write bandwidth, then disk *capacity* | More disks first. Two hundred cameras at 4 Mbit/s is 100 MB/s — one good disk — but 2 TB/day, which is where capacity beats bandwidth |
| **Retention** | disk capacity, linearly with days | More disks, or a second box when the chassis is full of them |
| **Availability** | **a server that must not be a single point of failure** | **This one.** No bigger box fixes it |

The first three are arithmetic and a purchase order. The fourth is the reason this module exists: a customer for whom *the server died and the site was dark for four hours while somebody drove there* is not acceptable. Meeting it means something has to notice the box died and start the work elsewhere. That something is a scheduler, and it is the cost of that requirement and nothing else. Write the requirement down first:

> *A single server failure must not stop recording for longer than __ seconds, and must not require a person.*

The blank is the product's recovery time objective. Lesson 4 measures it — `vms_failover_seconds{kind="worst"}` — and the drill script keeps the worst of three runs, because the datasheet number is the worst case.

## Step 2 — Why not on one box

**There is nothing to schedule.** A scheduler decides *which server* runs a piece of work. With one server there is one answer. "Run N workers" on one box is `systemctl start vmsworker@w-1`, `@w-2` — М10's template unit, with restart policy and the data-partition boundary already in it. A scheduler would add a second supervision layer over the same processes and a second notion of "running".

**It has a cost the appliance cannot spare.** Nomad's production guidance sizes *servers* at 4–8+ cores and 16–32 GB+ of memory and says nothing about single-node deployments; the shape is not one the tool is designed for. Memory spent on a raft server is memory that was page cache for video.

**It has its own failure modes.** HashiCorp publishes a support note on orphaned Podman containers after a Nomad agent restart — the same shape of bug М8 Lesson 3 met with the Docker client, one layer up. Under `systemd` that class of bug does not exist, because `systemd` *is* the supervisor and does not restart out from under itself.

> **Rule: one server, no orchestrator. The scheduler arrives with the second server, because that is when there is first a decision to make.**

М10's shape is what makes this a rule rather than a preference: the controller, the worker and the resource are the same three programs under `systemd` and under Nomad. Nothing has to be rewritten to go from one to many, so nothing is gained by running the scheduler early.

## Step 3 — Nomad's model, in the terms this module uses

| Role | Does | Count |
|---|---|---|
| **Server** | accepts jobs, holds cluster state in **raft**, decides placement | **three or five** per region |
| **Client** | registers its resources, runs the work it is given, reports back | every server that runs workers or carries a resource |

A **region** is one raft — one replicated log, one leader, one notion of what is true. Everything this module relies on for correctness — a worker's slot, the epoch per camera, the assignment, the placement — lives in that raft, which is why *cluster* in this course and a Nomad region are the same thing.

**Three or five, never two or four.** Raft needs a majority. Three servers tolerate one failure; two tolerate none — a two-server cluster is strictly worse than one server, because either one dying stops the other. For a server room, three.

The servers decide; the clients execute. On the bench the same three boxes do both, and the roles stay distinct in the configuration.

## Step 4 — Build the cluster

Three VMs from the М9 bench — `10.0.0.11`, `.12`, `.13` — each with a data partition at `/data`. **Neither `podman` nor `exec2` is built into Nomad**; both are plugins in a directory the agent is told about:

```bash
mkdir -p /data/nomad/plugins
nomad version                                  # >= 1.8.0: the disconnect block (Lesson 4)
install -m 0755 nomad-driver-podman /data/nomad/plugins/
systemctl enable --now podman.socket
```

[`deploy/server.hcl`](clustervms/deploy/server.hcl) — `bootstrap_expect = 3`, `retry_join` the three addresses, `acl { enabled = true }`, `data_dir = "/data/nomad"`. [`deploy/client.hcl`](clustervms/deploy/client.hcl):

```hcl
client {
  enabled = true
  servers = ["10.0.0.11:4647", "10.0.0.12:4647", "10.0.0.13:4647"]
  meta {
    labels  = "vlan:cctv-a,vlan:cctv-b"   # what this server's NICs can reach; the worker reports it, the controller places by it
    archive = "/data/archive"             # this server carries an archive resource
  }
}
```

Three things in there are load-bearing later. **`data_dir` on `/data`**: raft lives here, and the cluster's memory of every epoch ever issued must not be replaced by an OS update — М9 Lesson 4's boundary applied to the scheduler. **`meta.labels`** and **`meta.archive`**: the first becomes the worker's heartbeat and the controller's placement constraint (Lesson 5); the second is what pins the resource job to this server (Lesson 2). **`acl { enabled = true }`**: Lesson 2 depends on a worker being unable to write a camera row, and enabling ACLs later on a running cluster is a migration. Day one.

```bash
nomad server members         # three servers, one leader
nomad node status            # three clients, ready; -verbose shows the podman driver healthy
nomad acl bootstrap          # once; keep the management token somewhere that is not a lesson
```

And that is the whole of the cluster's storage layer: raft, and each server's disks. The object store М10's contract names — heartbeats, and one snapshot for the domain — is *also* Variables here, under `objects/…` (Step 5), because a dozen ten-kilobyte heartbeats every ten seconds is not a load a raft notices, and a second store with its own quorum, credentials and client is a cost the appliance does not need to pay for it. Footage and events are on the servers' own disks, as resources (Lesson 3).

## Step 5 — The platform's stores become Nomad's

This is the step the module's first claim rests on. `cluster/variables.py` is М10's `Variables` contract over Nomad's HTTP API:

```
GET  /v1/var/vms/cameras/7                    -> Items, ModifyIndex
PUT  /v1/var/vms/cameras/7?cas=8123           -> 200 if ModifyIndex is still 8123; 409 otherwise
PUT  /v1/var/vms/cameras/7  (a worker's token) -> 403: the ACL, Lesson 2
```

And `FakeVariables` is the same contract in memory with exactly the semantics the docs promise — a raft-assigned `ModifyIndex`, `cas` succeeding only on a match, 409 otherwise, a 403 for a writer outside its prefixes. The tests run against the fake in milliseconds; `verify-bench.sh` checks the promises against real Nomad.

The object store is the same store seen through the other contract: `VariablesObjectStore` implements М10's `put/get/list` as Variables under `objects/<key>`, so a worker's heartbeat is `objects/vms/w-1/heartbeat {data: …}` and the ACL that comes with its token covers it like any other Variable. `test_the_object_store_on_this_cluster_is_variables` runs М10's `Worker.heartbeat` and `Controller.workers_seen` over it unchanged. The contract is the point: when a cluster is large enough that its heartbeats are a raft load, `open_store("s3+http://…")` is the same three calls against MinIO or S3 (`s3.py`, SigV4 verified against Amazon's worked examples) — which is also the adapter a rented cluster uses in М12 — and `vms/` does not change.

Then the test that says nothing else changed:

```python
sub = Subsystem("thing")
ctl = Controller(sub, FakeVariables(), FsObjectStore(...))     # М10's base classes, untouched
w = Worker(sub, None, ...); w.claim_slot()   -> "w-1"
w.heartbeat([...], server="srv-a"); ctl.workers_seen()  -> {"w-1": ...}
ctl.assign("w-1", ["1"]); w.assignment().units          -> ["1"]
w.take_epoch("1")                                        -> 1
```

`test_m10s_base_classes_run_on_the_cluster_stores_unchanged` — the name is the claim. Four threads through `next_epoch` on the fake issue `1..200` with no number twice, which is what CAS on a `ModifyIndex` means whether the index comes from a file lock or a raft log.

## Step 6 — The per-worker budget

Before placing anything, the unit of placement. One container per camera is the obvious unit and the wrong one; М9 Lesson 7's probe measures why:

```
B ≈ 60 MB per process        I ≈ 8 MB per pipeline          (check yours — shapes, not claims)
one container per camera:    200 × (B + I)  ≈ 13.6 GB
four workers of fifty:         4 × (B + 50I) ≈  1.8 GB
```

Report PSS, not RSS — fifty processes share `libgstreamer`, and RSS counts it fifty times. The unit Nomad places is a **worker**: one process, N pipelines. Its `CAPACITY` — the number the worker puts in its heartbeat, the number the controller places by and the autoscaler scales on — is `(memory budget − B) / I` on *this* server, rounded down. It is the worker's number, measured where the pipelines run; the controller reads it and has none of its own (М10 Lesson 6).

**Deliverable:** a working cluster; `tests/test_lesson1_stores.py` green; a measured `CAPACITY` for your hardware written into `deploy/vmsworker.nomad.hcl`; and your version of Step 2's argument, one page, for the first time somebody proposes running Nomad on the single-box product for consistency.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `nomad node status -verbose` shows `podman` undetected | The plugin is not in `plugin_dir`, or `podman.socket` is not enabled. |
| The cluster forms, then splits after a reboot | `data_dir` is on the rootfs slot of an A/B box and the update replaced it. Raft on `/data`. |
| `nomad var put` works from the operator's shell and 403s from a job | ACLs are on and no policy is bound to the job yet — correct; Lesson 2 binds them. |
| `FakeVariables` passes and Nomad returns 409 on every write | The code reads once and writes many times with the first index. `Controller.write` re-reads on conflict; use it. |
| A worker's heartbeat returns 403 | Its policy lacks `objects/vms/*` — the object store is Variables here, and the ACL applies to it like everything else. |

## Recap

- Availability is the one pressure a bigger box cannot answer; write the RTO down before touching a scheduler.
- One server, no orchestrator; the scheduler arrives with the second server.
- A region is one raft; three servers, never two.
- `data_dir` on `/data`; `meta.labels` and `meta.archive`; ACLs on day one.
- Nomad Variables are М10's config store with the same two promises, and — at this size — its object store too, under `objects/…`; `vms/` does not notice either.
- A worker's capacity is measured on its server and is the worker's number.

## Exercises

1. Run the four-thread epoch test with the fake's lock removed. Report the first duplicate and explain why a real raft could never produce it.
2. Size a two-server "cluster" and list every failure in which it is worse than one server.
3. Put `data_dir` on the rootfs, simulate an A/B update by wiping it, and write down what the workers do at their next slot renewal.
4. Write the ACL policy that lets a *read-only* console token list `vms/*` and nothing else, and say what it must not be able to see (hint: `vms/objects`).
5. Argue the opposite of Step 2 for a customer with two hundred single-box sites and a central operations team — and say what they would have to give up.

## Where this is going

The stores are Nomad's and nothing noticed. [**Lesson 2**](02-workers-resources-and-the-controller-as-jobs.md) runs the three programs as jobs — four, counting the autoscaler — gives each worker a name it claims rather than one it is given, and proves from inside an allocation that a worker cannot write a camera row.
