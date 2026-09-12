# Lesson 4 — `vmsworker`: DriverPack as the Worker

**Module:** ServerVMS — the platform's shape on one server (Module 10)
**You will build:** the worker — one process, N pipelines, М9's loop running over an *assignment* instead of a table, an epoch per camera taken by CAS, a lease, a heartbeat carrying its status — and prove it restarts with the controller stopped and fences itself when it is the zombie.
**Time:** ~150 minutes.

## Why this lesson exists

The AppHost is gone and this is what replaced it. Not a new supervisor beside DriverPack — **DriverPack is the worker**. There is no process called "the worker" that hosts it; `vmsworker` is what DriverPack is called when it runs as a shard of the VMS subsystem, with its own loop, over its own assignment, publishing its own status. Nomad (or `systemd`, on one box) supervises the process; the process supervises its pipelines; nothing supervises the loop, because the loop is the process.

What did not change is the loop. `vms/reconciler.py` is М9 Lesson 6's file, copied, and М9's seven tests run against it in this lesson without a change of meaning — because those tests were written against a design, and the design is what a rewrite keeps. What changed is where desired state comes from (an assignment in the platform's store, not a table in the worker's database) and what a start costs (an epoch, by CAS, and a lease).

> **What you can verify without hardware.** All of it: `tests/test_lesson4_worker.py` runs М9's seven, then the worker over an assignment with the fake actuator — the epoch per camera, the edit that restarts, the reassignment that stops, the heartbeat, the restart with the controller object deleted, a nameless replacement inheriting the lapsed slot, the zombie fenced at the slot, the reassignment that is *not* a zombie, and a lease that ran out. `gstvms/actuator.py` is the real actuator, `driverpacksrc ! h264parse ! watchdog ! tee ! archivesink`, for the bench.

## Prerequisites

- **М9 Lesson 6** — the loop: desired persisted, actual derived, `>=`, the stop loop over what is running, backoff with jitter.
- **М9 Lesson 8** — the four failures, and `lost()`.
- **Lesson 1** — the `Worker` base: assignment, `take_epoch`, `may_write`, `renew_leases`, `heartbeat`.
- **Lesson 3** — the epoch in the path, which the worker now supplies.

## Learning objectives

1. Run М9's reconcile loop over an assignment and its camera rows, with М9's tests unchanged.
2. Take an epoch per camera on every start, by CAS, and gate every start on a live lease.
3. Publish a heartbeat carrying the status snapshot, and say who reads it.
4. Prove the controller is never on the recovery path.
5. Tell a zombie from a reassignment when a lease is lost, and act differently on each.
6. State what one supervisor means, and what crash isolation costs.

---

## Step 1 — М9's seven, unchanged

Before anything new, the contract:

```
test_1_converge_then_idle · test_2_revision_bump_restarts · test_3_disable_and_delete
test_4_restart_re_derives_actual · test_5_persisted_actual_is_a_cache_that_lies
test_6_backoff_with_jitter_spreads_200_cameras · test_7_lagging_vs_stalled
```

They import `vms.reconciler.Reconciler` and pass. Read `test_5` again: a loop that loaded its *actual* from disk does nothing, reports converged, and records nothing — the cache that lies. Every property in this lesson rests on the worker keeping that rule: **a fresh worker knows nothing and rediscovers everything.**

## Step 1a — A name is a slot

`VmsWorker(name, …)` claims a slot before it does anything else (Lesson 1, Step 5a). With a name — `systemd`'s `%i`, or `w-<NOMAD_ALLOC_INDEX>` in М11 — it takes that slot outright. With `None` it takes the first free one, a lapsed one first:

```
a = VmsWorker(None, …); b = VmsWorker(None, …)   -> ("w-1", "w-2")     count = 2, nobody named them
ctl.assign("w-1", ["1","2"]); ctl.assign("w-2", ["3","4"]); wall += 46   A is dead
c = VmsWorker(None, …)                            -> "w-1"              not w-3
c.reconcile_once()                                -> [('start', 1), ('start', 2)]   epochs {1: 2, 2: 2}
```

The replacement recorded the dead worker's cameras from the assignment and asked nobody — Step 5's property, now without a fixed name. The worker also exports `headroom` in its heartbeat (`capacity − assigned`, capacity being М9 Lesson 7's `B + n·I` measured on *its* server, `CAPACITY=` in the unit): the number the autoscaler reads in М11 to move `count`. Not CPU — a worker at forty percent CPU with no cameras left to take is full, and one at ninety percent with headroom is not a reason for another process.

## Step 2 — Desired state is an assignment

The reconciler reads `store.desired()`. In М9 that was a table; here `VmsWorker` *is* the store, and `desired()` returns the rows its assignment names:

```python
def refresh(self):
    a = self.assignment()                                 # vms/workers/<me>: {units, rev}
    self.rows = [row(items) for unit in a.units
                 for items, _ in [self.vars.get(f"vms/cameras/{unit}")] if items]
```

The controller wrote both — the assignment and the rows — and the worker reads both, every pass, from the platform's store. It never writes either. Run it:

```
unassigned:  []                                    a worker with no assignment invents nothing
assigned:    [('start', 1), ('start', 2)]   epochs {1: 1, 2: 1}
vms/epoch/1 = {'epoch': '1'}
after edit:  [('restart', 1)]                      revision 2 on camera 1 → restart, same epoch
```

## Step 3 — The gate: an epoch per camera, and a lease

The actuator the reconciler calls is wrapped:

```python
def _actuate(self, verb, cam):
    unit = str(cam["id"])
    if verb in ("start", "restart"):
        if not self.recording_allowed:  return False
        if verb == "start" or unit not in self.epochs:
            cam = dict(cam, epoch=self.take_epoch(unit))    # a NEW epoch for a NEW writer, by CAS
        if not self.may_write(unit):    return False        # no live lease, no start
        return self.actuator(verb, cam)
    ok = self.actuator("stop", cam); self.release(unit); return ok
```

Why per camera and not per worker, as М11 did for the recorder: because a **reassignment** is the one legitimate case of two writers on one camera — the controller moves camera 7 from `w-1` to `w-2`, and for a few seconds both may be writing — and the epoch has to separate those too. A restart of a running pipeline (an edit) keeps its epoch: same writer, same segment directory. A *start* takes the next one: `archivesink` opens a new directory, and whatever the previous writer was doing lands in the old one.

`lease_pass` renews the slot first and the camera leases second; a slot held by another instance fences everything before any epoch is read. The lease is the other half. `may_write(unit)` is a purely local decision on a monotonic clock — TTL 30, margin 5, the numbers М11 Lesson 4 derived — and a start without a live lease is refused before the actuator is asked. `test_lease_expiry_without_renewal_stops_starts` runs the clock 26 seconds forward, shows `may_write` false, and shows the next start taking a *fresh* epoch and a fresh lease rather than reusing the stale one.

## Step 3a — How an event is fired

Three sources, and they are different kinds of thing. **The pipeline**, which is where nearly all events come from: elements do not call Python, they post messages on the bus — a motion or analytics element posts an *element message*, a `GstStructure` named `motion` or `person` with its fields; `watchdog` posts an error when frames stop. The actuator's per-camera bus watch collects both, and `pump()` returns them as `(dead, posted)`:

```python
bus.connect("message::element", lambda b, m, c=cid: self._posted(c, m))   # gstvms/actuator.py
bus.connect("message::error",   lambda b, m, c=cid: self.dead.append(c))

def pump_once(self):                                                        # vms/worker.py
    dead, posted = self.actuator.pump()
    for cid, kind, fields in posted: self.observe(cid, kind, **fields)     # a line, if I hold the epoch
    for cid in dead:                 self.reconciler.lost(cid, self.now()); self.observe(cid, "silent")
```

The element never knows about buckets, epochs or files; it posts what it saw, and the worker — the process holding the camera's epoch — turns it into a line. That keeps М9's per-frame rule: detection runs inside the pipeline in C, and Python touches an event, not a frame. **The worker itself** is the second source, for what no element posts: `silent` on a lost pipeline, above. **An operator** is the third — and a mark from the console is *not* this worker's event and must not reach into it (there is no RPC to workers): it is the console's own observation, in the console's own bucket (Lesson 5, Step 6). `test_the_worker_observes_what_it_holds_recording_or_not` runs all of it with the fake actuator's `post()`, and ends with the fence at the source: a fenced instance's bus still posts, and `observe` drops it.

## Step 4 — The heartbeat

```json
{"worker": "w-1", "instance": "srv-1:4121:9c0f2a", "ts": 1757500000.0, "server": "srv-1", "assignment_rev": 2,
 "fenced": false, "conflicts": 0, "capacity": 50, "headroom": 47,
 "status": [{"id": 1, "name": "gate", "enabled": true, "phase": "running", "position": "converged",
             "revision": 2, "observed_revision": 2, "epoch": 1}, ...]}
```

One object, `vms/w-1/heartbeat`, every ten seconds, carrying the recorder's `/status` as М9 Lesson 9 defined it — positions apart from reasons — plus the epoch per camera and the server it runs on. Nobody calls the worker for its status: the controller reads this to know which workers exist (`workers_seen`), the console reads it for the camera list, М12's read model reads the same object across clusters. This is the shape М11 Lesson 4 chose for the heartbeat and М12 Lesson 3 grew; here it is the worker's from the start.

## Step 5 — The controller is never on the recovery path

```python
w1 = VmsWorker("w-1", ...); w1.reconcile_once()          # recording 1, 2, 3
del ctl                                                   # the controller is gone
w2 = VmsWorker("w-1", ...)                                # kill -9, restart: a fresh process under the same name
assert w2.reconciler.actual == {}                         # it knows nothing
assert w2.reconcile_once() == [("start", 1), ("start", 2), ("start", 3)]
assert act2.epochs == {1: 2, 2: 2, 3: 2}                  # the next epoch for each
```

The restart read its assignment and its rows from the store and asked nobody. The old instance — if it is still alive somewhere, paused, partitioned — holds epochs 1 and will find out on its next renewal. That is the property М11's whole failover story rests on, and it is a five-line test here. `deploy/vmsworker@.service` has `Restart=always`; the platform's job is to restart the process, and the process's job is to need nothing else.

## Step 6 — The zombie, and the reassignment that is not one

Two instances of `w-1` with the same assignment — a pause, then a replacement:

```
A: [('start', 1)]   epochs {1: 1}
B: [('start', 1)]   epochs {1: 2}   slot w-1 gen 2         the replacement takes the slot and the next epoch
A lease pass -> ['1'] lost   recording_allowed False       A wakes, renews its slot, finds another holder: it is the zombie
   w-1: FENCED (slot w-1 is held by another instance now). Stopping every pipeline.
A renew_leases -> ['1'], conflicts 1                       and the camera's epoch says the same, one layer down
A reconcile -> [('failed', 1)]                             it may start nothing
B lease pass -> []                                         B is fine
```

Two layers say the same thing on purpose. The slot fences the *instance* — cheap, one read, before anything else — and it is what Nomad's reschedule of index 3 relies on. The epoch fences the *camera* — and it is the one that still holds when the two writers are not two instances of one name but a reassignment between two names, which is the next case.

Now the same lease loss for a different reason: the controller moved camera 1 to `w-2`. `w-1` renews, finds epoch 2 — and must *not* fence itself, because it is not a zombie; it is a worker whose camera was taken away on purpose. The difference is one read:

```python
def lease_pass(self):
    lost = self.renew_leases()
    assigned = set(self.assignment().units)
    for unit in lost:
        if unit not in assigned:      # reassigned: stop it, release it, carry on
        else:                         # still mine and somebody else has it: I am the zombie; fence everything
```

`test_a_reassignment_is_not_a_zombie` asserts `recording_allowed` stays true and the next pass is empty. The two cases look identical from the lease's side; the assignment is what tells them apart, and it is one more reason the assignment lives in the store rather than in the worker's memory.

## Step 7 — One supervisor, and what crash isolation costs

The worker's `run()` is М9's AppHost's task list as one loop: reconcile, pump the buses, renew the slot and the leases every `(TTL − margin)/3`, heartbeat every ten seconds — and on an orderly stop, `release_slot()`, which is the one word that tells the controller *scale-in* rather than *crash*. There is no controller thread, no second process, no supervisor of the loop. `systemd` restarts the process if it dies, and a crash releases nothing: the slot lapses, and the restart claims it back.

Which means a vendor SDK that segfaults inside a pipeline takes the loop with it — the thing М9 Lesson 9 separated controller from worker to avoid. It is acceptable here *only because* the state is outside: the store holds the assignment, the resource holds the footage, the epoch and the lease make the restart harmless, and Step 5 is the proof. Keep that dependency explicit: the day someone caches the assignment in the worker "to survive the store being slow", crash isolation is gone and nobody will notice until a restart records nothing. The per-frame rule is the other line: the prototype's `_rebase` in Lesson 2 runs in Python, and the product's does not.

**Deliverable:** М9's seven and М9 Lesson 8's four failures — pipeline death (`pump_once` → `lost()`), stall (the watchdog on the bus), store unreachable (the lease keeps writing until TTL − margin), `kill -9` — against the worker, with the tests' meaning unchanged; then the zombie on one box with two real `vmsworker` processes: `kill -STOP` the first, start the second, `kill -CONT` the first, and read the manifest.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| The worker starts nothing and logs nothing | Its assignment is empty — correct. The controller assigns; a worker invents nothing. |
| Every pass restarts every camera | The rows' `revision` moves on every read. Only the controller writes it, and only on an edit; a worker that writes rows has the wrong ACL. |
| A replacement comes up as `w-3` while `w-1`'s cameras wait | The slot had not lapsed yet (45 s) and no name was given. Give the name — `%i`, `NOMAD_ALLOC_INDEX` — and the claim is immediate; or accept the wait, which is bounded by the slot TTL. |
| A worker fences itself on every restart of its neighbour | Two units share a name. `%i` is the slot; two `vmsworker@w-1` on one box is the zombie test, on purpose. |
| A restart takes epoch 1 again | The epoch key is per camera in the store, not per worker in memory. `take_epoch` must go through `next_epoch` — CAS — never a local counter. |
| A moved camera fences the whole worker | `lease_pass` did not re-read the assignment before deciding, or the controller moved the camera without removing it from the old assignment. `move()` removes it from every assignment that lists it. |
| After a reassignment both workers record for a while | Expected, and bounded by the lease numbers: ≤ TTL − margin for the old writer, into its own epoch. |
| The fake actuator passes and the real one does not | `archivesink` did not get the `epoch` property before its first fragment — the actuator formats it into the launch string from `cam["epoch"]`. |

## Recap

- DriverPack is the worker. One process, N pipelines, its own loop, no host.
- A name is a slot, claimed by CAS: a replacement inherits a lapsed slot and its assignment; an orderly stop releases it; the heartbeat carries headroom for whoever decides `N`.
- М9's loop and its seven tests are the contract, unchanged.
- Desired state is an assignment and its rows, read from the store every pass; the worker writes neither.
- A start takes a new epoch by CAS and needs a live lease; a restart keeps its epoch.
- The heartbeat carries the status snapshot; nobody calls the worker.
- The controller is never on the recovery path — a restart reads and records.
- A lost lease is a zombie if the camera is still mine, a reassignment if it is not.
- Events are fired by the bus: an element posts, `pump()` drains, `observe()` writes — if the worker still holds the epoch.
- One supervisor; crash isolation by external state, and only that.

## Exercises

1. Make `take_epoch` a local counter and run the zombie test. Then explain what the manifest looks like after a week.
2. Renew leases every second instead of every eight. Count the reads per camera per day at two hundred cameras, and say which store М11 will put them on.
3. Make a reassignment race: move camera 7 while `w-1` is mid-pass. List the orderings and show that every one ends with exactly one live epoch.
4. Add `pump_once` to the real actuator's bus handling for the watchdog and reproduce М9 Lesson 8's stall with `driverpacksrc` paused via `kill -STOP` on a helper.
5. Write the worker's `run()` for Nomad: what changes (the stop signal, the name from `NOMAD_ALLOC_INDEX`, `kill_timeout` against the slot release), and what must not.
6. Export CPU instead of headroom and let an autoscaler act on it. Describe the box with two hundred cameras assigned, idle at night, and what the autoscaler does at 03:00.

## Where this is going

Workers run what they are told. [**Lesson 5**](05-vmscontroller-and-the-second-subsystem.md) builds the one that tells them: the controller — the only writer of `vms/*`, placement stored with a reason, safe at two, never needed to recover — the console over it, the failure arithmetic measured, and a second subsystem through the same platform to prove the VMS is not special.
