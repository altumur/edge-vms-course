# Lesson 6 — `vmscontroller`

**Module:** ServerVMS — the platform's shape on one server (Module 10)
**You will build:** the controller — the only writer of `vms/*`, camera CRUD and placement by CAS, stored with a reason, safe at two, never needed to recover, never deciding how many workers there are — the failure arithmetic measured process by process, and the controller as data — a YAML the platform runs, so that the next subsystems need no controller of their own. The console is Lesson 7; live video and detectors are Lessons 7 and 8; the box is Lesson 10.
**Time:** ~120 minutes.

## Why this lesson exists

Somebody has to write configuration, and the module's answer is: exactly one thing, and it is not the worker and not the console. М9 gave the Node its own database so that an operator could edit a camera with everything above the Node unreachable — an argument about the *domain*, which may be down. Inside a cluster the store is one raft, the workers are stateless, and a single writer keeps every property М9 wanted while dropping the one it paid for. The controller is that writer.

It is also the process most likely to be built wrong, because "one controller" invites state. So the lesson spends its second half on the two properties that keep it honest — it holds nothing and is correct by CAS; it is never on the recovery path — and its last step on making the controller a description rather than a program — the YAML that Lessons 7 and 8 will reuse for two more subsystems without writing a controller for either.

> **What you can verify without hardware.** All of it: `tests/test_lesson6_controller.py` — refusals, stored placement, *adding a worker moves nothing*, two controllers racing to place forty cameras, capacity read from the workers' heartbeats, budgeted rebalance, scale-in redistributing a released slot and a crash moving nothing, the failure arithmetic with the clock. Every output below came out of them.

## Prerequisites

- **Lesson 1** — the `Controller` base: `write(path, mutate)`, `workers_seen`, `assign_add`/`assign_remove`.
- **Lesson 4** — what the worker reads, so that what the controller writes is exactly that.
- **М9 Lesson 5** — operator-owned versus controller-owned columns; the revision trigger this controller replaces.
- **М9 Lesson 7** — `B + n·I`, which is where `capacity` comes from.
- **М12 Lesson 3** (read ahead) — the read model from heartbeats and the write API that refuses placement; this lesson is the one-box version.

## Learning objectives

1. Implement camera CRUD as read-modify-write by CAS, with the revision bump in the controller.
2. Refuse what a client may not set, and say why each field is refused.
3. Place a camera on a worker by capacity, store the decision with a reason, and prove adding a worker moves nothing.
4. Run two controllers at once and show every camera placed exactly once.
5. Say who decides how many workers run and where — and prove the controller does not: scale-in moves cameras, a crash moves nothing.
6. Measure the failure arithmetic: stop each process and say what stopped.
7. Write the controller as a spec, and say what a subsystem must still write by hand.

---

## Step 1 — The only writer

`vms/config.py` is the schema, as items in the config store: `vms/cameras/<id>` with `id, name, source, enabled, retention_days, priority, revision`. Operator-owned columns and one controller-owned column, exactly М9 Lesson 5's split — but the trigger that bumped `revision` on every operator edit is now three lines in `update_camera`:

```python
def mutate(it):
    r = row(it); r.update(fields); r["revision"] += 1; return items(r)
return row(self.write("vms/cameras/7", mutate))       # read-modify-write, by CAS, with retry
```

`create` takes an id from `vms/next_id` (by CAS), `delete` marks the row and removes the camera from its worker's assignment. Every write goes through `Controller.write`, which re-reads on a conflict — so a second controller editing the same row does not lose the first one's edit, it applies its own on top of it.

## Step 2 — What it refuses

```
refused: a client may not set ['worker']: placement is decided and stored by the controller with a reason;
         revision, epoch and phase are not the operator's
```

Five fields are refused on any write: `worker` and `placement` (the controller decides where, with a reason — a client that could set them would be a second placement service without one), `revision` (the controller's, bumped on edit, never set), `epoch` (the worker's, taken by CAS, never assigned) and `phase`/`observed_revision` (derived by observation, in the heartbeat, and only there). A camera without a `source` is refused with the sentence that names both URI schemes. This is М12 Lesson 3's list at the cluster, and it is the same list because it is the same principle: what a thing is told and what it observes are different columns.

## Step 3 — Placement, stored with a reason

`place(cid)` puts one camera on the worker with the most free capacity among those it sees heartbeating, and stores the decision. **Whose number is capacity?** The worker's. `B + n·I` is measured on the server the pipelines run on (М9 Lesson 7), so each worker carries its own `capacity` in every heartbeat, and `capacity_of(w)` is the controller *reading* it — its constructor's `capacity=50` is only the fallback for a heartbeat that says nothing. The controller does not know the servers; it knows what the workers said:

```
w-1 says capacity 2, w-2 says 6, nine cameras:  load {w-1: 2, w-2: 6}, the ninth waits — "the system is full"
capacity_of("w-9") -> 50                        a worker that said nothing gets the fallback
```

Nothing has to *tell* DriverPack to start a camera, either. The controller writes the id into `vms/workers/w-2` and that is the whole act; the worker reads its own row at the top of every pass and starts what it is not yet running. A row and a poll — no RPC, no push — which is exactly what lets a worker restart with the controller dead (Lesson 4, Step 5).

The placement, stored:

```
cam 1 -> w-1 | most free capacity (3) among 2 worker(s) | rev 1
cam 2 -> w-2 | most free capacity (3) among 2 worker(s) | rev 1
cam 3 -> w-1 | most free capacity (2) among 2 worker(s) | rev 1
...
full: camera 7 -> None                                   "the system is full" — never "w-1 is full"
```

Two rules carried up from М11 Lesson 5, with their tests. **Store the placement; do not derive it.** `vms/placement/<id>` holds the worker, the reason, the time and a revision, so *why is camera 5 on w-1* at three in the morning is a row. **Adding a worker moves nothing.** A third worker arrives; the six placed cameras stay where they were and the seventh, which had nowhere to go, lands on it:

```
with w-3: {1: 'w-1', 2: 'w-2', 3: 'w-1', 4: 'w-2', 5: 'w-1', 6: 'w-2', 7: 'w-3'}
```

The assignment rows are what the workers read, and they follow the placement: `{'w-1': ['1', '3', '5'], 'w-2': ['2', '4', '6'], 'w-3': ['7']}`. Rebalance exists and is what М11 said it must be — explicit, budgeted, interruptible, with a dead band, a reason on every move, and each move going through `move()`, which removes the camera from *every* assignment that lists it before adding it to the destination (the reassignment window Lesson 4 handles on the worker's side).

## Step 4 — Two controllers, forty cameras

Nomad's `count = 1` is not exactly-one during a reschedule. So the test runs two controllers with *opposite* preferences — one lists `w-1` first, the other `w-2` — placing the same forty cameras concurrently, and asserts:

```python
where = {cam: c.where(cam) for cam in cameras}            # every camera has exactly one worker
units = assignment("w-1").units + assignment("w-2").units
assert sorted(units) == [1..40]                           # and appears in exactly one assignment
```

Two writes make that true. The placement row is written by CAS with a `mutate` that returns `None` if the row already names a worker — the loser reads the winner's decision and adopts it. The assignment is `assign_add`, a read-modify-write that merges into whatever is there rather than overwriting a list read a moment ago. The first version of this controller did the second one wrong and lost cameras under the race; the test is what found it, which is the point of writing it.

## Step 4a — Who decides how many workers, and the one unasked move

Not the controller. It places cameras on the workers it *sees* — the heartbeats — and it has no way to ask for one: no scheduler client, no `count`, no opinion. On one box the operator starts `vmsworker@w-2`; in М11 Nomad runs `count = N` and the Nomad Autoscaler moves `N` from `vms_worker_headroom`, which the console exports per worker straight from the heartbeats and the controller sums in `headroom()`. That keeps three things out of the controller that would otherwise have to be in it: a model of the servers, a client for the scheduler, and a policy about cost.

What the controller *does* own is what happens to cameras when `N` goes down. `test_scale_in_releases_a_slot_and_the_controller_redistributes`:

```
count = 3, six cameras placed: {w-1: 2, w-2: 2, w-3: 2}     headroom 12
redistribute()                        -> []                 nothing released, nothing moves
wall += 46 (w-3 silent: a crash)      -> []   where(3) = w-3  a crash is Nomad's to fix; the cameras wait for w-3
w-3.release_slot()  (SIGTERM: scale-in)
redistribute()                        -> [(3, w-3, w-1), (6, w-3, w-2)]     "slot w-3 released; most free capacity (2)"
headroom()                            -> 2                  2 × 4 − 6: what the autoscaler reads next
retire("w-1")                         -> released_slots() == ['w-1']        the operator's word, never an inference
```

`redistribute()` runs beside `ensure_placed()` every five seconds, and it reads exactly one thing: `released_slots()` — slots whose holder *said* it was going (Lesson 1, Step 5a), and that still list cameras. A slot that merely lapsed is not on that list, and so a dead worker's cameras are not moved: its process returns under the same name and records them. That line is the difference between a controller and a healer. Every move goes through `move()`, with a reason that names the slot — so at three in the morning *why is camera 3 on w-1* is still a row.

## Step 5 — The failure arithmetic, measured

`test_the_failure_arithmetic` stops each process in turn:

| Down | What stops | What continues — and the test that says so |
|---|---|---|
| **the controller** | placing a new camera; taking a deleted one's assignment back; the snapshot | recording (the worker never asked it); **edits** — the console writes rows with its own token; the read model — a fresh `VmsController` built from the same store answers `phase: running` for both cameras from the heartbeat |
| **the console** | the screen; edits | recording; placement — the controller's pass never asked the console anything; a second console instance, since it is stateless |
| **the worker** | recording, until `systemd` restarts it | edits: `update_camera(1, …)` lands in the store while the worker is down; the console shows the last snapshot as `stale`, `age 100.0`; a restarted worker reads the edit from the store — *not* from the controller — and starts with the new name |
| **the archive resource** | promotion | recording into the spool, until the high-water policy (Lesson 3) |
| **the config store** | edits and new assignments | recording: the worker holds its assignment in memory and needs the store only to change |

The row to read twice is the second. An edit made *while the worker was dead* is present when it comes back, because the edit went into the store and the worker reads the store. There is no *saved · not yet replicated* on one box, and М11 will show there is none inside a cluster either.

## Step 6 — The controller as data

Read `vmscontroller` line by line and nothing in it is about video. It is parameterised by names and numbers: where the rows live, how a unit is identified, which fields an operator may set and their types, which heartbeat field is capacity, a rule for which workers are eligible, what leaves the cluster. That is a description, not a program — so `vms/vms.subsystem.yaml` is the description and `psimplatform/spec.py`'s `SpecController` is the one program, run from it. `vms/controller.py` is twenty lines that call the platform's class by the VMS's names (`create_camera`, not `create`). The spec's vocabulary is deliberately small: fields with types and defaults, a derived row (`vms/retention/<id>`, what the resource retains buckets by), a constraint and a tie-break **by name** from a catalogue of two (`labels-subset`, `most-free-capacity`), a snapshot list, the console's gauge. A subsystem that needs another rule registers a function under a name — code, named, not YAML pretending to be code. And the two tokens come out of the same file: `acl_console()` is the operator's rows, `acl_controller()` is placement.

```
name: vms
unit:      {rows: cameras, id: numeric, fields: {name, source, enabled, retention_days, events_retention_days, priority, labels, ref}, derived: [retention/{id}]}
placement: {capacity: {from: capacity, fallback: 50}, headroom: {from: headroom}, constraint: labels-subset, tie_break: most-free-capacity}
snapshot:  [name, source, enabled, retention_days, events_retention_days, priority, labels, ref]
```

What is *not* in a spec is what a unit *does* — that is the worker, and the worker is the subsystem. So what a new subsystem writes is a YAML and a worker, and the proof is not a toy: Lesson 8 adds live video, whose unit is a camera's fan-out and whose capacity is viewers, and Lesson 9 adds detectors, whose unit is a model on a camera and whose events are their own buckets on the resource — two subsystems that look nothing like recording, through this class, with no controller code for either. `test_the_platform_knows_nothing_about_video` keeps the boundary literal: nothing under `psimplatform/` imports the VMS or says the word *camera*.

**Deliverable:** one box, one controller. `POST /cameras` (through the console of Lesson 7, or `create_camera` from a shell) starts a recording within one worker pass; stop the controller and show recording and a worker restart unaffected; kill the worker and show the edit made meanwhile applied on restart; and a written statement of what the platform knows about the VMS — a prefix, an assignment shape, a heartbeat shape, the names in one YAML, and nothing else.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Two controllers place a camera on two workers | The placement `mutate` does not return `None` when a worker is already named — it overwrites. The row is the decision; the assignment follows it. |
| A camera vanishes from every assignment under the race | `assign()` with a list read before the write. Use `assign_add`/`assign_remove`; they merge inside the CAS loop. |
| `ensure_placed` places nothing | No worker has heartbeated within `lost_after`. A worker that has never run has never existed, and the controller invents nothing. |
| A dead worker's cameras are not moved | Correct. Its slot lapsed but was not released; Nomad brings the process back under the same name. If it will not return, `retire(slot)` — an operator's statement. |
| The autoscaler adds workers at night | It is scaling on CPU. Scale on `vms_headroom`; CPU is a symptom, headroom is the demand. |
| Rebalance moves the same camera back and forth | No dead band, or budget larger than the imbalance. Ten percent and a small budget. |
| A second subsystem sees `vms/` rows | Its prefix is wrong or it is listing `/`. A subsystem lists its own prefix and nothing else; the ACL will make that a rule in М11. |

## Recap

- The controller is the only writer of `vms/*`; every write is read-modify-write by CAS with a retry.
- It refuses `worker`, `placement`, `revision`, `epoch`, `phase`: what a thing is told and what it observes are different columns.
- Placement is by capacity — the worker's number, read from its heartbeat, never the controller's — stored with a reason; adding a worker moves nothing; rebalance is explicit and budgeted.
- Two controllers agree because the row is CAS and the assignment merges — and the test found the version that did not.
- Stop the controller: nothing running stops. Kill the worker: the edit is waiting in the store when it returns.
- The controller never decides how many workers there are or where they run: the scheduler runs `N`, the autoscaler moves `N` from headroom, and the controller's one unasked move is to redistribute a *released* slot — never a lapsed one.
- The controller is data: a YAML the platform's `SpecController` runs from; a new subsystem writes a YAML and a worker, and Lessons 7 and 8 do exactly that.

## Exercises

1. Cache `workers_seen()` in the controller for sixty seconds "to save reads". Run the two-controller test and the failure test, then say which property broke.
2. `capacity_of` reads the *last* heartbeat, however old. Make it read only live ones and construct the reschedule in which the fallback constant places forty cameras on a worker that can carry ten.
3. Write `Subsystem("det")` with a `GPU` resource: what does its controller place *on*, and what does the worker's affinity look like in М11's job file?
4. Give the controller a Nomad client and let it set `count` itself when `headroom()` hits zero. List what it now has to know (servers, costs, the job file, the API's failure modes) and what happens when two controllers do it at once.

## Where this is going

One box runs the platform's shape: two stores, a controller run from a description, a worker, a resource. Nobody has looked at a screen yet. [**Lesson 7**](07-the-console.md) gives the operator one — its own process, its own token, and the same YAML the controller runs from.
