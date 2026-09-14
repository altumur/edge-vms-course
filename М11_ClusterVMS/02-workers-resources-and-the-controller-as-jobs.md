# Lesson 2 — Workers, Resources and the Controller as Jobs

**Module:** ClusterVMS — workers that outlive their server (Module 11)
**You will build:** five jobs — the worker with `count = N` and a `scaling` block, the archive resource as a `system` job, the controller at `count = 1`, the console as a `system` job on every server, and the Nomad Autoscaler — a worker identity that survives being rescheduled because it is claimed rather than given, and the ACL proven from inside an allocation.
**Time:** ~180 minutes.

## Why this lesson exists

М10 ran three programs as `systemd` units. This lesson runs the same three programs as Nomad jobs and adds a fourth that is not the VMS's at all. The programs do not change; what changes is who starts them, how many, where, and what a process is told about itself when it comes up. That last one is the lesson's real subject. Under `systemd` a worker was `vmsworker@w-1` and knew its name from `%i`. Under Nomad it is *one of N allocations of a job*, and if its name came from nowhere better than its environment, a rescheduled worker would come back as somebody else and its cameras would sit in an assignment row nobody reads.

The other thing this lesson settles is a question М10 left open and answered last: **who decides N.** Not the controller. The operator sets bounds, Nomad places, and the Nomad Autoscaler moves `count` from a number the workers export. The controller has no Nomad client, and this lesson shows the two things it does instead: place cameras on whatever workers exist, and move the cameras of a slot whose holder *said* it was stopping.

> **What you can verify without hardware.** `tests/test_lesson2_jobs.py`: the slot from `NOMAD_ALLOC_INDEX`, labels from the server, scale out and in with the redistribution, Nomad's duplicate-index bug resolved at the CAS, and the ACL from a worker's identity. `nomad job validate` on the four files and `verify-bench.sh` need the Lesson 1 cluster.

## Prerequisites

- **М10 Lesson 1, Step 5a** — identity by claim: `vms/slots/<name>`, `claim_slot(prefer)`, `released` versus lapsed.
- **М10 Lesson 5, Step 4a** — the controller's one unasked move.
- **Lesson 1** — the cluster, `meta.labels`, `meta.archive`, ACLs on.
- **М9 Lesson 4** — Quadlet; **М10 Lesson 9** — the Quadlet units these jobs translate, over the image these jobs build `FROM`.

## Learning objectives

1. Translate М10's three units into jobs, and say which shape each takes and why.
2. Give a worker a name from `NOMAD_ALLOC_INDEX` and explain why the index is the preference and the Variable is the proof.
3. State who decides `count`, write the `scaling` policy, and say why the metric is load and not CPU.
4. Scale out and in and show what the controller does in each case — and what it does on a crash.
5. Say what goes in Variables and what does not.
6. Bind a policy to a job and prove one-writer-per-key from inside an allocation.

---

## Step 1 — Four shapes

| Program | Quadlet unit on М9's box (М10) | Under Nomad | Why that shape |
|---|---|---|---|
| **`vmsworker`** | `vmsworker@.container`, `vmsworker@w-N` started by hand | `service`, `count = N`, `scaling {}`, `disconnect {}`, `kill_timeout = 20s` | movable; placed by constraint; N is the scheduler's |
| **`resource`** — the platform's, with the VMS registered on it | the directory; `vms-archive-retain.container` + timer for the policy | **`system`**, `constraint meta.archive is_set` | one per eligible server, pinned; it never moves because it cannot; every subsystem's buckets, the mirror, the retention passes — and the VMS's manifests and footage as *its* part |
| **`vmscontroller`** | `vmscontroller.container` | `service`, `count = 1`, no port | one is economy, not correctness — CAS is the correctness, and a second would repeat the same pass; its token writes placement only (`vmscontroller-policy.hcl`) |
| **`console`** | `vmsconsole.container` | `system`, port 8080 on every server, `constraint meta.archive is_set` (for marks) | a person is waiting on it, and any server's address is the console — no load balancer, nothing in front; stateless; a retry is answered the same by any instance (`vms/idem/*`); its token writes the operator's rows only (`console-policy.hcl`) — a console that could place would be a second controller with a browser in front |
| **the Nomad Autoscaler** | the operator's hand | `service`, `count = 1`, reads Prometheus, talks to Nomad | the only thing that changes `count`; MPL-2.0; not ours |

The files are in [`deploy/`](clustervms/deploy/). Two things in the worker's job are not translation but new:

```hcl
constraint { attribute = "${meta.archive}"  operator = "is_set" }   # a worker records into the resource on ITS server
task "vmsworker" { kill_timeout = "20s" ... }                        # room to release the slot on SIGTERM
```

The first says where a worker may run at all: only on a server that carries an archive resource, because `archivesink` promotes into *this* server's disks. The second is what makes scale-in distinguishable from a crash (Step 4).

The drivers: `podman` throughout, a plugin (Lesson 1). `exec2` — a native process under Landlock and cgroups v2, for a worker that needs a capture card or a GPU without a container in the way — is a plugin too, GA since 1.9, and demands a kernel with Landlock (`cat /sys/kernel/security/lsm`); put the check in the image build.

## Step 2 — A name is a slot; the index is the preference

Every allocation gets `NOMAD_ALLOC_INDEX`, `0..count−1`. It looks exactly like a worker number, and the first ClusterVMS design forbade using it, for a documented reason: [Nomad issue #10727](https://github.com/hashicorp/nomad/issues/10727) reported two running allocations with the same index. Fixed — but a fixed bug is still the wrong foundation, because the index was designed as a *label* and correctness of an archive was never something it was promised to carry.

М10's slot mechanism is what makes the index safe to use at all. `cluster/worker.py`:

```python
NOMAD_ALLOC_INDEX=1  NOMAD_NODE_NAME=srv-b  NOMAD_META_labels=vlan:cctv-a,vlan:cctv-b  NOMAD_ALLOC_ID=alloc-0002
w = ClusterWorker(vars, objects, env=...)     -> М10's VmsWorker, reading NOMAD_* from env; claims vms/slots/w-1 by CAS: {holder: alloc-0002, until, released: false, gen}
```

The index is the *preference* — `claim_slot(prefer="w-1")` — and the claim is the *proof*. `test_the_slot_comes_from_the_allocation_index_and_the_labels_from_the_server` shows the row: the holder is the allocation id, and the heartbeat carries `server: srv-b`, `labels: vlan:cctv-a,vlan:cctv-b`, `capacity`, `headroom`. Then the bug, on purpose:

```
a = worker(index 0, srv-a); ctl.assign("w-0", [1, 2]); a records
b = worker(index 0, srv-b)                                   # #10727: two allocations, one index
slots["w-0"].holder == b.instance, gen 2                     # the second claim took the slot outright
a.lease_pass() -> fenced: "slot w-0 is held by another instance now"
b.reconcile_once() -> [('start', 1), ('start', 2)]           # and only one of them is recording
```

`test_two_allocations_with_one_index_resolve_at_the_cas`. Two allocations with one index is exactly a reschedule from the slot's point of view — a new process claiming a name whose old holder may still be alive — and the CAS decides it the same way. The rule the first design wrote survives with one word changed:

> **The allocation index is a label. Worker identity is a slot claimed by CAS; the index only says which slot to claim first.**

## Step 3 — Who decides N

Nobody in the VMS. The worker job carries:

```hcl
scaling {
  min = 1
  max = 12                                       # the servers' budget, from Lesson 1's B + n·I
  policy {
    cooldown            = "5m"                   # longer than a failover, so a reschedule is not read as demand
    evaluation_interval = "1m"
    check "load" {
      source = "prometheus"
      query  = "avg(vms_worker_load)"            # assigned ÷ capacity, from the heartbeats — never CPU
      strategy "target-value" { target = 0.9 }
    }
  }
}
```

The Nomad Autoscaler — a separate agent under MPL-2.0, [`deploy/autoscaler.nomad.hcl`](clustervms/deploy/autoscaler.nomad.hcl) — evaluates that every minute against Prometheus, which scrapes the console's `/metrics`, where `vms_worker_load{worker="w-1"}` is `1 − headroom/capacity` straight from the heartbeat. At `avg = 1.0` and target `0.9` it sets `count = ⌈N × 1.0/0.9⌉`; at `0.4` it brings `N` down; `cooldown` keeps it from reading the 48 seconds of a failover as a demand change.

**Why load and not CPU.** A worker with two hundred idle cameras at 03:00 is at twelve percent CPU and *full* — every one of those cameras is assigned and must stay assigned. A CPU policy would scale the cluster down at night and strand them. Load says what the demand is: cameras that need a worker, over what the workers can carry. The controller adds nothing to this: it sums `headroom` from the heartbeats for the console and has no number of its own.

## Step 4 — Scale out, scale in, crash

`test_nomad_job_scale_out_then_in`, with `capacity = 4`:

```
count = 2, eight cameras: {w-0: 4, w-1: 4}, headroom 0, avg(vms_worker_load) = 1.0
camera 9 created  -> ensure_placed: "the system is full"; it waits
nomad job scale vmsworker 3      (the autoscaler's call, or yours)
  alloc index 2 on srv-c claims w-2; heartbeats
  ensure_placed -> camera 9 on w-2 — reason "most free capacity (4) among 3 worker(s); on srv-c"
nomad job scale vmsworker 2      (index 2 gets SIGTERM, inside kill_timeout)
  w-2.run() exits -> release_slot(): vms/slots/w-2 {released: true}
  redistribute -> [(9, w-2, w-0)]      "slot w-2 released; most free capacity (…)"
```

Three things to read off that. The controller **placed** the ninth camera when a worker appeared and **moved** it when a worker said it was going; it did not ask for the third worker and did not notice it was gone except by reading a row. The row says `released: true` because the worker wrote it on an orderly stop — which is why `kill_timeout` is 20 s and not Nomad's default 5: the release is a CAS write, and it has to land before the kill.

And the case the test also covers, because it is the one people get wrong: **a crash releases nothing.** The slot lapses; `released_slots()` does not list it; `redistribute()` moves nothing. Nomad's `disconnect` block (Lesson 4) brings index 2 back — on another server if this one is gone — and the replacement claims `w-2` with its assignment intact. Had the controller "helped" by moving the cameras at the first silence, the returning worker would have found an empty assignment and the cameras would have been moved twice for nothing, or worse, would have two writers for the length of the confusion.

`retire(slot)` exists for the operator who knows a process will not return. It is a statement, not an inference.

## Step 5 — What goes in Variables, and what does not

| In Variables (raft), as configuration | In Variables, as objects (`objects/…`) | On the resource |
|---|---|---|
| camera rows, placement, assignment, slots, the epoch per camera | heartbeats (every ten seconds, per worker and per resource); the snapshot for М12 | footage, its manifest, every subsystem's event buckets — and, with the knob on, a peer's copies |
| small, rare, must be consistent — one writer per prefix | frequent, never queried by key, and *small enough*: a dozen 10 KB heartbeats every ten seconds | large, written constantly, read as a range; does not move |

The 64 KiB cap on a Variable exists, in the maintainers' words, *to reduce the potential performance impact of Variables on our raft store* — the store is memory-resident and replicated to every server. A thousand camera rows are a few hundred kilobytes across a thousand keys and fit. So do the heartbeats: twelve workers and three resources every ten seconds is 1.5 writes a second of ten kilobytes each, a hundred thousand writes a day that a raft designed for scheduling decisions does not notice — which is why the first design's MinIO is gone from this module. The object store stays as a *contract* (`VariablesObjectStore` here; `s3.py` when a cluster outgrows this, or is rented), and the rule reads: **small and consistent goes in the scheduler's store; frequent-but-small goes there too, as objects; bulk read as a range stays on the server that wrote it.** What would bring a second store back is volume — a hundred workers, or heartbeats carrying thumbnails — and the code would not change to take it.

## Step 6 — The ACL, from inside an allocation

Two policies, bound to two jobs' workload identities:

```hcl
# vmscontroller-policy.hcl                      # vmsworker-policy.hcl
path "vms/*" { capabilities = ["write", ...] }  path "vms/epoch/*" { capabilities = ["write", "read", "list"] }
                                                path "vms/slots/*" { capabilities = ["write", "read", "list"] }
                                                path "vms/*"       { capabilities = ["read", "list"] }
```

`identity { env = true }` in each task puts the task's own `NOMAD_TOKEN` in its environment, and `nomad acl policy apply -job vmsworker` binds the policy to that identity. `test_the_acl_from_inside_an_allocation` is the fake's version; `verify-bench.sh` steps 4 and 5 are the real one — first with a token carrying only the worker policy, then from inside a running `vmsworker` allocation with `nomad alloc exec`:

```
own=200 other=403 — one writer per key holds
```

`own` is `PUT vms/epoch/verify`; `other` is `PUT vms/cameras/verify`. If `other` ever comes back 200, the sentence the whole module rests on — *the controller writes, the platform stores, the worker reads its share* — is a convention, and conventions do not survive the first "quick fix".

**Deliverable:** the five jobs running with the behaviour they had under `systemd`; `nomad job scale vmsworker 3` → a new slot claimed and the next camera placed on it within one pass; `… 2` → the released slot's cameras redistributed; `verify-bench.sh` all PASS, including `own=200 other=403` from inside the allocation.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| A rescheduled worker comes up as `w-3` while `w-1`'s cameras wait | It was not given the index (`NOMAD_ALLOC_INDEX` missing from a custom launcher). Without a preference the claim waits for the slot to lapse — 45 s — before inheriting. |
| Scale-in leaves the slot `released: false` and the cameras stranded | `kill_timeout` too short for the release to land, or the task ignores SIGTERM. 20 s, and `run()` must exit through `release_slot()`. |
| The autoscaler adds workers during a failover | `cooldown` shorter than the RTO. Five minutes. |
| The autoscaler adds workers at night | It is scaling on CPU. `avg(vms_worker_load)`. |
| `verify-bench.sh` says `own=403` | The policy is applied but not bound to the job (`-job vmsworker`), or `identity { env = true }` is missing from the task. |
| Two workers on one server, one with no cameras | Correct if `headroom` says the first is not full; the controller fills by most free capacity, not round-robin. |

## Recap

- Five jobs: worker (`service`, `count = N`), resource (`system`, on `meta.archive`), controller (`count = 1`, placement's token), console (`system`, one per server, the operator's rows' token), autoscaler.
- A name is a slot claimed by CAS; `NOMAD_ALLOC_INDEX` is the preference; the duplicate-index bug is harmless.
- Nomad places; the Autoscaler moves `count` from `vms_worker_load`; the controller never asks.
- Scale-in releases a slot and the controller redistributes; a crash releases nothing and the controller waits.
- Raft holds what is small and consistent, and the heartbeats as objects; the resource holds bulk; there is no MinIO on this cluster.
- One writer per key is a policy bound to a job's identity, proven from inside the allocation.

## Exercises

1. Set `kill_timeout = "1s"` and scale in. Read the slot row, then say what the controller does and for how long the cameras wait.
2. Write the `scaling` policy for a customer whose cameras are all on motion detection and idle at night — and then explain why it is the same policy.
3. Give the controller a Nomad token and let it call `nomad job scale` when `headroom()` is zero. List what it now has to know, and run the two-controller test.
4. Put the heartbeat in a Variable "for consistency". Compute the raft write rate at twelve workers and three resources, and find the 64 KiB cap.
5. Bind the *controller's* policy to the worker job by mistake. Which test in this module catches it, and which real-cluster check?

## Where this is going

Workers are placed and named; the controller is one and safe at two. [**Lesson 3**](03-what-stays-on-the-server-and-what-does-not.md) pulls a server and asks what was on it: configuration that never left raft, footage that stays on a resource, a manifest that returns with its disks, and one open segment that is the honest loss.
