# Lesson 1 — The Subsystem Contract

**Module:** ServerVMS — the platform's shape on one server (Module 10)
**You will build:** the platform on one box — a config store with check-and-set, an object store, an epoch issuer and a lease — and the contract every subsystem gives it: a controller and its workers.
**Time:** ~120 minutes.

## Why this lesson exists

М9 ended with a recorder that owns its truth: a Postgres holding what the box should be, an AppHost making it so. It works, it is tested, and it has a shape that does not survive the question an engineer asks first when a scheduler arrives — *why is there a controller process at all, when the scheduler supervises processes and the thing that holds the pipeline supervises itself?* The answer, worked out in [`ARCHITECTURE.md` §1.11](../ARCHITECTURE.md), split the AppHost in two: half of it was the worker's own and moves into DriverPack; half of it was never about video and belongs to a platform.

This lesson builds that platform, on one box, and writes down the contract. The point is not the code — a file-backed key-value store with an index is an afternoon — but the property the code makes testable: **the platform knows nothing about a camera**, and a second subsystem can be dropped onto it without changing a line. Lesson 5 does exactly that.

> **What you can verify without hardware.** All of it. `tests/test_lesson1_platform.py` runs the config store's persistence and CAS, four threads racing the epoch issuer, the lease on a fake clock, the ACL, and a test that greps the platform's source for the word *camera*. Every output below came out of it.

## Prerequisites

- **М9 Lessons 5–9** — the recorder: its schema, its loop, its numbers. This module keeps the loop and retires the database.
- **М9 Lesson 4** — the spool and *delete on acknowledgement, never on send*. The archive inherits it in Lesson 3.
- **М11 Lesson 2** (read ahead if you like) — Nomad Variables and their `cas` parameter. This lesson builds the same semantics as files so that М11 can swap the implementation and keep the tests.

## Learning objectives

1. Say what the platform is and is not, and name the boundary.
2. Build a config store with `ModifyIndex` and check-and-set, and prove two writers cannot both win.
3. Build the epoch issuer and the lease as platform pieces, generic to any writer.
4. Write the subsystem contract as a table a second team could implement from.
5. Explain why the ACL is *one writer per prefix* and what breaks without it.
6. Give `count = N` interchangeable processes stable names without a controller handing them out — identity by claim — and say who decides N.
7. Say what a server holds now, and why the word *Node* is retired.

---

## Step 1 — What the platform is

Everything in the course so far that does not know what a camera is: a scheduler that places processes by constraint (М11), a small consistent config store, an object store, a signer and an agent for trust (М12), a web gateway, an observer (М13). That is a **platform**, and it would host any fleet of stateless shards writing bulk data. The **VMS** is what is specific to video: the worker that holds the pipeline, the detectors, the schema of cameras, sites and grants, and the UI.

A **subsystem** is what a product team gives the platform, and it is always the same two things:

| | What it is | How many | State | When it is down |
|---|---|---|---|---|
| **Controller** | the only writer of the subsystem's configuration and of the assignment of work to workers | one — and safe at two, because every write is check-and-set | none of its own | no edits, no new assignments; nothing already running stops |
| **Worker** | runs the subsystem's work against its assignment; reports by heartbeat | 1+, by workload | none but a spool | its share of the work stops until it is restarted; the epoch and the lease make the restart harmless |

The VMS is the first subsystem: `vmscontroller` and `vmsworker`. Detectors will be the second; the live gateway the third. The platform knows the shape — a config prefix, an assignment per worker, a heartbeat per worker, an epoch prefix — and nothing else.

**Resources** are the third kind of thing and they are server-bound: on one box, the archive on its disks. A resource has no controller; it has a lifecycle policy. Lesson 3 builds one.

## Step 2 — The config store, as files

М11 Lesson 2 will use Nomad Variables: a small, consistent key-value store where every entry carries a raft-assigned `ModifyIndex`, and a PUT with `cas=<index>` succeeds only if the index still matches. On one box there is no raft, so `vmsplatform/variables.py` promises the same thing with files: one JSON file per path, one counter for the index, one lock, every write atomic.

```
put ->  1001
get ->  ({'name': 'gate', 'revision': '1'}, 1001)
stale cas -> vms/cameras/7: cas=1000 but ModifyIndex=1001
['vms%2Fcameras%2F7.json']   index: 1001
```

Read the third line twice. A writer that read index 1000, went away, and came back to write with `cas=1000` is told the world moved — and told *what to*, so it can re-read and decide again. That is the whole of the platform's concurrency model, and it is why the controller can be "one" without anybody counting: two controllers write, one wins the CAS, the other re-reads and agrees. `test_two_processes_one_cas_winner` runs four threads incrementing one counter through their own store handles and asserts that the counter equals the number of successful writes — nothing lost, nothing doubled.

The store also survives a restart, which is the point of it being files: `FileVariables(root)` opened by a fresh process returns the same items at the same index. М9's Postgres survived restarts too; this survives them with nothing to migrate.

## Step 3 — The epoch and the lease, generic

М11 Lesson 4 built the fencing token for a recorder. Here it is a platform piece, because *any writer that can have two instances* needs it, and the platform does not know which writers those are:

```python
next_epoch(vars, "vms/epoch/7")     # by CAS: two callers racing get two DIFFERENT numbers, in order
Lease(vars, "vms/epoch/7", epoch, ttl=30, margin=5, clock=monotonic)
lease.may_write()                   # now − last_renewal < TTL − margin: a purely local decision
lease.renew()                       # read the key; still mine → renewed; someone else's → fenced
```

Four threads issuing twenty-five epochs each get `1..100` with no number twice. The lease stops writing at 24.9 + 0.2 seconds on a fake clock and resumes on a successful renewal; when somebody else takes the key, `renew()` returns false and `fenced` is set once. What the subsystem decides is *which key* — for the VMS, one per camera (Lesson 4 says why). What the platform promises is that the number comes from one issuer and increases.

## Step 4 — The contract

`vmsplatform/contract.py` is the document, as code:

| The platform provides | The subsystem provides |
|---|---|
| a config prefix `<name>/*`, writable by the controller only | a **controller**: `count = 1`, every write by CAS, holds nothing |
| assignment rows `<name>/workers/<worker>` — `{units, rev}` | a **worker**: `count = N`, reads its row and the units it names, runs the work, heartbeats |
| a heartbeat object `<name>/<worker>/heartbeat` — `{worker, ts, status: [...], ...}` | what goes in `status` — for the VMS, each camera's phase, epoch and revisions |
| an epoch prefix `<name>/epoch/<unit>`, taken by workers by CAS | which key the epoch goes in |
| a slot prefix `<name>/slots/<worker>` — a worker's *name*, claimed by CAS and renewed (Step 5a) | a headroom number in its heartbeat, for whoever decides `N` |
| an event log per unit on the resource — `<name>/<unit>/e<epoch>/<start>Z.events.jsonl`, written by the worker holding that unit's epoch (Lesson 3, Step 5a) | what its events say |
| **the resource** — a platform job per server (`vmsplatform/resource.py`): every subsystem's buckets served, mirrored to a peer with `platform/mirror` on, restored, and retained by the rows `<name>/retention` and `<name>/retention/<unit>`; a pass a subsystem registers runs on its timer | its retention rows (the controller writes them); its own pass over its own part of the tree (the VMS: manifests and media) |
| a metrics scrape per job (М13) | its two numbers |

```
vms/cameras/7   vms/workers/w-1   vms/w-1/heartbeat   vms/epoch/7   vms/slots/w-1
controller ACL: ['vms/*']        worker ACL: ['vms/epoch/*', 'vms/slots/*']
```

`Controller` has `write(path, mutate)` — read-modify-write by CAS with a retry — plus `workers_seen()`, which is not a list it keeps but a fact it reads: the workers whose heartbeat object is younger than `lost_after`; and `slots()`, `released_slots()`, `retire()`, which read slots and never hand them out. `Worker` has `claim_slot()`, `renew_slot()`, `release_slot()`, `assignment()`, `take_epoch(unit)`, `may_write(unit)`, `renew_leases()` and `heartbeat(status)`, and one abstract method, `reconcile_once`. Neither base class imports anything from `vms/`, and `test_the_platform_knows_nothing_about_video` fails if one ever does — or if the word *camera* appears in the package outside its docstring.

## Step 5 — One writer per prefix

`FileVariables.as_writer(name, allowed)` is what a Nomad ACL policy will be in М11: the same store seen through an identity that may write only listed prefixes.

```python
ctl = vars.as_writer("vmscontroller", ["vms/*"])
wrk = vars.as_writer("vmsworker", ["vms/epoch/*", "vms/slots/*"])
wrk.put("vms/cameras/7", {...})      # Forbidden: a worker never writes configuration
```

Why it matters is the sentence the whole module rests on: **the controller writes, the platform stores, the worker reads its share.** A worker that could write a camera row is a second owner of configuration; a controller that could write a worker's epoch could fence a worker by accident. The ACL is what turns "should not" into "cannot", and М11's `verify-bench.sh` is where it is proven against real Nomad.

## Step 5a — Identity by claim: who decides how many workers, and what they are called

Nobody in the VMS decides how many workers there are. On one box an operator starts `vmsworker@w-1`, `@w-2`; in М11 Nomad runs `count = N` of one job, and the **Nomad Autoscaler** moves `N` from a number the workers export — *headroom*, cameras they could still take, not CPU. The controller places cameras on the workers it sees. It never asks for one, never starts one, never calls a scheduler.

What that leaves the platform to solve is a small thing with a sharp edge: `count = N` gives you N *interchangeable* processes, and an assignment is written to a *name*. If a replacement process after a crash came up under a new name, its predecessor's cameras would sit in a row nobody reads — and the only thing that could fix it would be the controller noticing a silence, which is the healing it must never do. So a name is a **slot**, and a process *claims* one:

```python
a = Worker(sub, None, ...); a.claim_slot()          -> "w-1"      first free number
b = Worker(sub, None, ...); b.claim_slot()          -> "w-2"      count = 2: two names, in order
wall.advance(46)                                                   A went silent past the slot TTL
c = Worker(sub, None, ...); c.claim_slot()          -> "w-1"      a lapsed slot before a new number:
c.assignment().units                                -> ["1","2"]  the replacement inherits, asking nobody
a.renew_slot()                                      -> False      A, wherever it was, is fenced at the slot
d.claim_slot(prefer="w-7")                          -> "w-7"      the scheduler's own index wins outright
```

`vms/slots/w-1` is `{holder, until, released, gen}`; the claim is a CAS, holding is renewed with the lease pass, and losing it fences the whole instance before any epoch is looked at. Nomad hands each allocation a stable `NOMAD_ALLOC_INDEX`, and `__main__` passes it as `prefer`: the replacement of index 3 takes slot `w-3` *even from a holder that has not lapsed* — the scheduler is the authority on which process is current, and the old one finds out on its next renewal (Lesson 4's zombie, one layer earlier). With no index — `systemd`, or a job that does not set one — a lapsed slot is taken before a new number, which is the same inheritance by a different route.

The last column of the row is what tells **scale-in from a crash**. A process stopped by the scheduler — SIGTERM, an orderly `run()` exit — writes `released: true`; a crash writes nothing, and the slot merely lapses. `released_slots()` lists the first kind only, and that list is what a subsystem's controller redistributes (Lesson 5's one unasked move). A lapsed slot is left alone: the scheduler brings the process back, and it claims the same name. `retire(slot)` exists for the operator who knows a process will not return — and it is the operator's word, never the controller's inference from a silence.

## Step 6 — What a server is now

Four words now, not five: Server, Cluster, Domain, Site. *Node* is retired. М9 needed it because the recorder was a process with its own database and its own disk — a thing distinct from the box it ran on, that could in principle move. Under this module's shape there is no such thing: what is on a physical server is the platform's stores (a replica of the cluster's, or the files on one box), a **resource** on its disks, and whichever **workers** the scheduler placed there. None of those is a Node, and naming their sum would only reintroduce the process М10 dissolved. So the word for the box is the box's: **server**. On one server that is: two directories under `/data/platform`, one `vmscontroller`, one `vmsworker`, one resource with the archive on it. In М11 the directories become Nomad Variables, the `systemd` units become jobs, and the subsystems do not change.

**Deliverable:** the platform package with its tests green; a config store on disk that survives a restart and refuses a stale CAS; four threads through the epoch issuer with no number issued twice; and the contract table above, written as a document a second team could implement `detectorcontroller` and `detectorworker` from without reading `vms/`.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Conflict` on every write from one process | It reads once and writes many times with the first index. Read-modify-write is one unit: `Controller.write(path, mutate)` does it for you. |
| Two processes on one box both think they won | They are using different `PLATFORM_DIR`s. The lock and the index are per root. |
| `next_epoch` raises after two hundred conflicts | Something is hammering one key in a tight loop. That is the test's four threads, or a worker retrying a start without backoff — Lesson 4's loop has the backoff. |
| `Forbidden` from the controller | It was constructed as a worker identity. The identity is the process's, set once in `__main__`. |
| The platform test fails on a docstring | You wrote *camera* into `vmsplatform/`. The test is the boundary; move the sentence. |

## Recap

- The platform is everything that does not know what a camera is; a subsystem is a controller and its workers; a resource is server-bound and has a policy, not a controller.
- The config store has `ModifyIndex` and CAS — as files here, as Nomad Variables in М11 — and CAS is why "one controller" needs no counting.
- The epoch issuer and the lease are platform pieces; the subsystem chooses the key.
- The contract is a table and a pair of base classes with no import from `vms/`.
- One writer per prefix: the controller writes `<name>/*`, a worker writes only its epochs and its slot.
- Identity by claim: a name is a slot taken by CAS; a replacement inherits a lapsed slot and its assignment; `released` tells scale-in from a crash. The scheduler decides `N`, the autoscaler moves it from headroom, and the controller never asks for a worker.
- *Node* is retired: a server holds the platform's stores, a resource, and the workers placed on it — and none of that needs a fifth word.

## Exercises

1. Remove the lock from `FileVariables.put` and run the four-thread test until it fails. Say which write was lost and why the index did not catch it.
2. Give the worker's ACL `vms/*` "for convenience". Write the first bug that becomes possible.
3. Sketch `Subsystem("det")` for detectors: what are its units, what is in its heartbeat's `status`, and what key does its epoch go in?
4. The heartbeat is an object and the assignment is a Variable. Swap them — assignment as an object, heartbeat as a Variable — and say what goes wrong in each direction, using М11 Lesson 2's three-stores rule.
5. Write, in one paragraph, what the platform team may change without telling the VMS team, and what it may not.
6. Make the controller "help" by retiring any slot silent for two minutes. Construct the Nomad reschedule (a node drain, `kill_timeout` 30 s, a two-minute pull of the image on the new node) in which it moves every camera twice and the returning worker fences on a slot it should have kept.

## Where this is going

The platform can store, fence and describe work; nothing is producing any. [**Lesson 2**](02-driverpacksrc.md) builds the source: a GStreamer element that plays a file as if it were a camera, with the one part of any source that is not mechanical — timestamps — built and tested first.
