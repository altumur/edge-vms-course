# Lesson 4 — Failover, and the Two Instances of One Worker

**Module:** ClusterVMS — workers that outlive their server (Module 11)
**You will build:** the `disconnect` numbers a worker needs and the lease arithmetic that agrees with them; the power pull measured from the workers' own heartbeats; the old instance waking up and being fenced twice — at its slot and at every epoch — with its footage kept; and the reassignment window, which is the same window with a different verdict.
**Time:** ~180 minutes.

> **The two numbers this lesson exports.** `vms_failover_seconds{kind="worst"}` — the last heartbeat of the dead instance to the replacement's first pass, worst of three runs — is the product's RTO. `vms_epoch_conflicts{worker}` counts a stale instance fenced at the resource; zero forever on a healthy cluster, alarmed on anyway.

## Why this lesson exists

Everything in this module so far is about placing things. This lesson is about the moment placement is wrong and nobody can tell: Server A stops answering, and it is dead, or partitioned, or paused under a debugger, and *nothing can tell those apart.* Nomad concludes `w-1` is lost and starts another `w-1` on Server B. Both read the same assignment. Both believe they own cameras 1, 2 and 3.

The first ClusterVMS design solved this with an epoch in the archive path and a lease on a monotonic clock, and that mechanism is unchanged — it is М10's, per camera, in every key. What 2c adds is a second, earlier fence that was not available when identity was a Variable the scheduler delivered: the **slot**. Under identity by claim, the replacement *takes* `w-1` by CAS, and the old instance finds out at its next renewal before it has looked at a single epoch. Two layers, on purpose: the slot fences the instance cheaply; the epoch fences the camera and still holds when the two writers are not two instances of one name but a reassignment between two.

> **What you can verify without hardware.** `tests/test_lesson4_failover.py`: the power pull on a fake clock with the measured 48 s, the old instance waking and fenced at both layers with `conflicts == 3`, the reassignment that is not a zombie, and the lease stopping the holder at 26 s on its own clock. The real pull, `deploy/failover-drill.sh`, needs the bench.

## Prerequisites

- **М10 Lesson 4** — the gate: an epoch per camera by CAS, `may_write`, `lease_pass`, the zombie and the reassignment.
- **М10 Lesson 1, Step 5a** — `claim_slot(prefer)` taking a held slot outright; `renew_slot`.
- **Lesson 2** — the worker job, `kill_timeout`, the slot from the index.
- **М8 Lesson 2** — signals; `kill -STOP` is still the module's most important teaching device.

## Learning objectives

1. Distinguish restart from reschedule and say which keeps footage on the same disk.
2. Set the `disconnect` block for a worker and defend the four values.
3. State why the epoch must come from a single issuer, and why Nomad's variable lock is the wrong one.
4. Derive the lease margins on a monotonic clock and the clock-rate error they tolerate.
5. Pull the power, measure the RTO from the heartbeats, and show the replacement asked nobody.
6. Let the old instance wake, and show it fenced at the slot and at every epoch with its footage kept.
7. Show a reassignment lose the same lease and keep recording.

---

## Step 1 — Restart versus reschedule

| | Where | Governed by | Fixes | Footage |
|---|---|---|---|---|
| **Restart** | the same server | `restart {}` | a crashed process — a vendor SDK taking the worker with it | stays on the same resource; the closed-but-unpromoted segment is picked up (М10 Lesson 3) |
| **Reschedule** | a different server | `reschedule {}` | a dead server, a full disk, a constraint no longer met | the old resource keeps the past; the new one takes the future |

Restart first, reschedule second: three restarts in ten minutes on one server before Nomad decides the server is the problem. Service jobs default to unlimited reschedule attempts with exponential delay — right for a worker, which must not give up while the operator is asleep.

## Step 2 — The `disconnect` block

By default, when a client stops heartbeating Nomad marks its allocations lost and places replacements *while the client, if merely partitioned, keeps running its tasks.* For a web service that is fine. For a worker it is two writers on camera 7. The block (Nomad ≥ 1.8.0 — the version floor) is where you say what you want instead; the worker job carries:

```hcl
disconnect {
  lost_after           = "45s"        # TTL + margin: how long a silent client keeps its allocation
  replace              = true         # then place a replacement elsewhere
  stop_on_client_after = "25s"        # TTL − margin: the partitioned client stops the old one itself
  reconcile            = "best_score" # when the client returns and there are two, keep the better-placed one
}
```

The question that *is* the argument: is two servers recording camera 7 for a minute better or worse than neither? *Neither* is a gap of exactly `lost_after` plus placement. *Both* is a minute of duplicate footage on two resources — harmless **only if the archive enforces the epoch**, which it does. So: both, bounded by `stop_on_client_after`, and fenced. Step 4 sets the three numbers so that Nomad and the lease agree on the moment.

## Step 3 — Where the epoch comes from

**Two writers to one video stream cannot be merged.** There is no reconciliation function for footage, so the epoch must be a fencing token from a **single issuer**, and — Kleppmann's argument — the essential part is *where it is checked*: the resource rejects the stale token, not the lock service.

The VMS version is unusually clean because the token is in the key: `<cam>/e3/…` versus `<cam>/e4/…`. The old instance cannot corrupt the new one's segments because it cannot name them. It writes valid files under an epoch the manifest marks as fenced — kept, shown, never deleted.

**The wrong answer first.** Nomad has a variable *lock*, and its lock ID is an opaque UUID with no monotonic index — exactly the lock Kleppmann's argument is about. A holder that paused, lost the lock and resumed has a lock ID that proves nothing about ordering. **The right answer**: a Variable with check-and-set. `PUT vms/epoch/7 {epoch: 4} cas=8123` — 200 if nobody else wrote, 409 otherwise; atomic, single-issuer, monotonic, replicated. And no new coupling: failover already requires Nomad, because Nomad is what reschedules the worker. Why not a database sequence: a sequence reissues numbers after a restore from backup, and the recovery procedure corrupts the archive. Raft cannot lose the counter without losing the cluster, and if the cluster is gone there is nothing to fail over.

## Step 4 — Clocks, and what the margins buy

> Lease expiry must not depend on wall-clock time. Use a monotonic clock, and depend only on clock *rates*, never on two servers agreeing what time it is.

```
holder:       may write while  now − last_renewal   <  TTL − margin     (stops early, on its own clock)
replacement:  may start when   now − last_heartbeat ≥  TTL + margin     (starts late)
```

The holder stopping is a purely local decision, which is precisely why it is the part that can be trusted. `test_the_lease_stops_writing_before_the_replacement_may_start`: at 24 s `may_write` is true; at 26 s it is false — nobody told it anything — and a successful renewal restores it, because it was never fenced, only cautious.

| | Value | Because |
|---|---|---|
| **TTL** | **30 s** | Nomad's own client heartbeat and grace are in the ten-second range; a lease shorter than three heartbeats flaps on a switch reboot |
| **margin** | **5 s** each side | `(30 − 5)/(30 + 5)` = 71 %: a holder's clock may run 29 % slow before the two-writer window opens — a bound no real clock approaches |
| **renewal** | `(TTL − margin)/3` ≈ **8 s** | three chances before the local stop; one lost renewal is nothing, three is the design working |
| **`stop_on_client_after`** | **25 s** | the partitioned client stops the old instance at the moment its own lease would have — Nomad and the lease agree |
| **`lost_after`** | **45 s** | Nomad places the replacement at the moment the arithmetic says one may start |

The two-writer window is at most `2 × margin` = 10 s on a partition and zero on a pause (`CLOCK_MONOTONIC` keeps counting through `SIGSTOP`, so the instance already knows on `SIGCONT`), and every write inside it lands under its own epoch. The slot uses the same TTL on the *wall* clock for a different purpose — a lapsed slot is one a replacement may inherit — and is not a fence by time: the fence is the CAS.

## Step 5 — Pull the power

`test_the_power_pull` is the drill on a fake clock:

```
t=0      w-1 (alloc-0001) on srv-a records cameras 1, 2, 3 under epoch 1; heartbeats
         srv-a dies
t=48     the replacement alloc for index 1 comes up on srv-b   (lost_after 45 + placement 3)
         claim_slot("w-1") -> taken outright; previous_instance = alloc-0001
         reconcile_once -> [('start', 1), ('start', 2), ('start', 3)]; epochs {1: 2, 2: 2, 3: 2}; server srv-b
         heartbeat -> vms_failover_seconds{w-1} = 48.0
ctl.where(1) == "w-1"                                           nothing was rewritten
```

The number comes from the workers, not from Nomad: the replacement reads the heartbeat object its predecessor left under the same name and reports `started − previous_hb`. The controller's `failover_seconds()` collects them and the console exports the worst as `vms_failover_seconds{kind="worst"}`. On the bench, [`deploy/failover-drill.sh`](clustervms/deploy/failover-drill.sh) does it three times with a zero-deadline drain standing in for the power cut (М9's `outage.sh power-cut` for the real thing), keeps the worst case, and after each run returns the server and reads `vms_epoch_conflicts` — Step 6's number.

What the replacement did not do: ask the controller, read a published object, restore anything. It read raft and recorded.

**Two workers on one server, and who decides.** The power pull above assumes a spare server: Nomad puts the replacement on srv-b. Nothing in the job stops Nomad from putting two workers on one server — it says `spread`, a preference, not `distinct_hosts`, a refusal — and whether that is wanted is not the scheduler's question. Two workers on one box share its disks and its NIC, so the second is no second place to record; but on failover it is the only way a dead server's cameras come back without anyone moving them. So the choice is the administrator's, on the console, one row: `vms/policy {servers: shared | distinct}` — `PUT /policy`, read by the controller on its next pass, the console's token may write it, the controller's may not.

*Shared* (the default — a box **is** several workers on one server): every worker is a place to put cameras, two on one server included. srv-a dies, Nomad reschedules `w-1` onto srv-b beside `w-2`, the replacement claims the slot and inherits the assignment — the power pull as written, no controller involved. *Distinct*: one worker per server carries cameras; a second worker Nomad put on the same server *idles by policy* (`idle_by_policy`, and `/servers` says so); and a dead server's cameras have nowhere to come back to by themselves, so the controller acts — on two silences. `gone_servers()`: a slot lapsed *and* still unclaimed another `lost_after` later (the scheduler's first refusal, should a spare server exist) *and* the resource on its last known server silent. Then `redistribute()` moves its cameras to the workers that are here, with the reason `server srv-a gone: slot w-1 lapsed and its resource silent; most free capacity (…); on srv-b`; `w-2` takes the next epoch for each and records on srv-b. When srv-a returns, its worker claims `w-1` again, reads an empty assignment and records nothing; nothing moves back, because adding a place to record moves nothing, like adding a worker. One silence — the process crashed, srv-a's resource still answering — moves nothing under either policy: that process returns under the same name.

```
policy: distinct
crash:      w-1 silent, srv-a's resource live      gone_servers() == {}          redistribute() moves nothing
power pull: w-1 silent, srv-a's resource silent    gone_servers() == {w-1: srv-a}   3 cameras → w-2, epoch 2, on srv-b
srv-a back: w-1 claimed, assignment empty          nothing moves back
policy: shared
power pull: w-1 silent, srv-a's resource silent    gone_servers() == {}          the slot is Nomad's; its replacement inherits
```

`test_a_server_gone_with_nowhere_to_reschedule_the_controller_moves_the_cameras`. This is the one place the controller acts on a silence, and it acts on two of them, from different processes on the same box, after giving the scheduler first refusal, and only when the administrator asked for it; the record's *Scale-in* row — a lapsed slot is not touched — stands for one silence and for the default. The price of *distinct* is that the surviving worker must carry both servers' cameras: `capacity` is sized for that, and М9 Lesson 1's arithmetic says what it costs in retention. The gain is control: every camera's move is a placement row with a reason, no worker ever shares disks with another, and the operator reads on `/servers` which server is gone, where its cameras went, and which worker is idle by policy.

## Step 6 — The old instance wakes up

Server A was not dead — partitioned, or paused. It comes back with `w-1` still running epoch 1 on three cameras. `test_the_old_instance_wakes_up_and_the_archive_is_intact`:

```
a.lease_pass()          (kill -CONT)
  renew_slot -> False: vms/slots/w-1 is held by alloc-0002
  FENCED (slot w-1 is held by another instance now). Stopping every pipeline.
a.renew_leases() -> ['1', '2', '3'];  a.conflicts() == 3         every epoch says the same, one layer down
a.heartbeat -> {fenced: true, server: srv-a}    then b heartbeats  -> the live one wrote last
a.reconcile_once() -> [('failed', 1), ('failed', 2), ('failed', 3)]   it may start nothing
```

Two fences, and the order matters. The **slot** is one read and fences the whole instance before it touches a camera — it is what a Nomad reschedule relies on, and what makes the duplicate-index bug harmless (Lesson 2). The **epoch** is per camera and is the one the archive enforces: even an instance that somehow kept its slot could not write into e2's directory, because it cannot name it. `vms_epoch_conflicts{worker="w-1"}` is now 3, and the console's timeline for each camera shows e1's last segment *fenced* beside e2's first — real footage of the partition minute, marked, kept.

## Step 7 — The reassignment that is not a zombie

The same lease loss, a different verdict. The controller moves camera 2 from `w-1` to `w-2` — an operator's decision, or a rebalance (Lesson 5). `test_the_reassignment_window_is_the_same_window_with_a_different_verdict`:

```
ctl.move(2, "w-2", "operator: srv-b sees that VLAN")
w-2.reconcile_once() -> [('start', 2)]; epoch 2                  the destination takes the next epoch
w-1.lease_pass() -> ['2']; recording_allowed stays True; running {1, 3}
w-1.reconcile_once() -> []                                       it let camera 2 go and kept the rest
```

From the lease's side the two cases are identical: a renewal found a newer epoch. The worker tells them apart by one read of its assignment — the camera is no longer mine, so this is a move, not a takeover — and that read is one more reason the assignment lives in raft rather than in the worker's memory. For up to `TTL − margin` both may write camera 2, into e1 on srv-a's resource and e2 on srv-b's, and the merged timeline shows both, one fenced.

## Step 8 — Planned failover

The same mechanism with a human choosing the moment. М9 replaces a server's OS with a RAUC bundle and reboots; on a cluster that reboot is a drain:

```bash
nomad node drain -enable -deadline 5m <node id>     # workers move off in order: SIGTERM, slot released, segment finalised
rauc install update-2026.10-1.raucb && reboot         # М9 Lesson 2
nomad node drain -disable <node id>                   # the server takes work again; its resource heartbeats
```

A drained worker leaves through `release_slot()` — so its slot is *released*, and the controller redistributes its cameras rather than waiting for a return (Lesson 2, Step 4). That is the difference between a drain and a crash from the controller's chair, and it is one word in a row. What does not move, planned or not: the footage. It is unavailable for the length of the reboot and comes back with the disks.

**Deliverable:** pull the power on a server; report `vms_failover_seconds{kind="worst"}` over three runs and how many seconds of camera 7 were lost. Restore the server, let the old instance wake, and prove the archive intact and its output fenced — `vms_epoch_conflicts` moved from zero, e1's segments on the timeline marked, e2's clean. Then do the same with a reassignment instead of a failure and show `recording_allowed` stayed true.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Nomad places the replacement before the old instance has stopped | `lost_after` < TTL + margin. 45 s. The epoch keeps it safe; the setting makes the window longer than it needs to be. |
| The old instance keeps writing for a minute after reconnect | `stop_on_client_after` unset, and its lease never lost (the partition healed before a renewal was due). The epoch fenced it anyway; the footage is under e1, marked. |
| `vms_failover_seconds` is missing after a pull | The replacement found no previous heartbeat under its name — the object store was unreachable when the old instance last wrote, or the slot name changed. Check `previous_instance` in the heartbeat. |
| `vms_epoch_conflicts` stays at zero after the server returns | The old instance was actually dead — the server lost power rather than its network. Zero is the healthy number; use `kill -STOP` to exercise the fence. |
| The replacement fences *itself* on its first lease pass | Its clock was set backwards after start (wall clock used for the lease). The lease is on `CLOCK_MONOTONIC`; find the code that is not. |
| A moved camera fences the whole source worker | The controller moved it without removing it from the source's assignment. `move()` removes it from every assignment that lists it, first. |

## Recap

- Restart on the same server keeps footage on the same resource; reschedule leaves the past behind and takes the future elsewhere.
- `disconnect { lost_after 45s, stop_on_client_after 25s }` — Nomad and the lease agree on the moment.
- The epoch is a fencing token from one issuer, checked at the resource, in the key; a variable lock's UUID is not one.
- Monotonic clocks; margins buy tolerance for clock *rate* error; the window is ≤ 10 s on a partition, zero on a pause.
- The RTO is measured from the workers' own heartbeats, worst of three, and the replacement asked nobody.
- The old instance is fenced at the slot and at every epoch; its footage is kept, marked.
- A reassignment loses the same lease and is not a zombie; a drain releases the slot and is not a crash.
- Two workers on one server is the administrator's choice, `vms/policy {servers: shared | distinct}` on the console: shared, the scheduler's replacement inherits; distinct, the controller moves a dead server's cameras on two silences, the slot's and the resource's, after Nomad's chance. One silence is a crash and moves nothing either way.

## Exercises

1. Set `lost_after = "10s"` and run the drill three times on a network with a switch reboot in the middle. Count the false failovers and the seconds of duplicate footage.
2. Replace `next_epoch` with Nomad's variable lock and construct, step by step with `kill -STOP`, the sequence in which the old instance writes into the new one's directory.
3. Make the slot TTL 10 s and the lease TTL 30 s. Which layer fences first on a pause, and does the answer change anything the archive can see?
4. Remove `stop_on_client_after`. Compute the worst-case two-writer window on a partition with a 20 % slow clock.
5. A worker with sixty cameras fails over onto a server that already runs a full worker. Trace what happens — Nomad's placement, the constraint, the autoscaler's cooldown — and say how long the sixty wait.

## Where this is going

Failover is measured and the zombie is fenced. [**Lesson 5**](05-the-controller.md) is the process that was never needed for any of it: the controller as a job — placement under constraints with the server in the reason, *where is camera 7* in one scan, two controllers agreeing, and the one object that leaves the cluster.
