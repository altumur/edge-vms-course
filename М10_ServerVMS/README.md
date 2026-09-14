# Module 10 — NodeVMS: The Platform's Shape on One Node

[Module 9](../М9_EdgeVMS/README.md) ends with a box that owns its OS and its truth: an A/B root under RAUC, and a Node whose Postgres holds what it should be while a worker makes it so. This module takes that Node apart and rebuilds it on the shape the course arrived at last — **a controller, workers, and resources** — on one box, so that every piece can be seen running before [М11](../М11_ClusterVMS/README.md) spreads it across servers.

Five lessons, each building one artifact: a GStreamer source that plays files as if they were cameras; an archive that is a resource, on the spool's discipline, with a manifest instead of an index; the worker — DriverPack itself — running М9's loop over an assignment; the controller that is the only writer of configuration; and a second, trivial subsystem that proves the platform knows nothing about video. No scheduler, no KVS, no database.

The design brief is [`module-design.md`](module-design.md); the first NodeVMS design it supersedes is [`node-design.md`](../М9_EdgeVMS/node-design.md), whose tests this module keeps.

## The thesis

| | Controller | Worker | Resource |
|---|---|---|---|
| What it is | the only writer of `vms/*` — cameras, assignment, placement | DriverPack with N cameras assigned; the pipelines and their routing | the archive on this box's disks; later a GPU |
| How many | one — and safe at two | `N`, decided by the scheduler and its autoscaler from the workers' headroom — never by the controller | one per server |
| State | none: computation over the stores | none but a spool | the footage and its manifest |
| When it is down | edits stop; nothing running stops | its cameras pause until it is restarted; the edit made meanwhile is waiting | promotion pauses; recording continues into the spool |

> **The controller writes, the platform stores, the worker reads its share.** The platform — a config store with check-and-set, an object store, an epoch issuer, a lease, and slots that give `count = N` processes stable names by claim — knows the shape of a subsystem and nothing about a camera. `vmsplatform/` has no import from `vms/`, and a test greps it for the word.

**What changed since М9's Node**, and what did not. KVS is gone — the archive is ours. The per-Node Postgres is gone — configuration lives in the platform's store and has one writer. The worker is gone — the thing that holds the pipeline supervises itself. What is kept, and enforced by the same tests: desired persisted and actual derived, `>=` on the revision, backoff with jitter, positions apart from reasons, the epoch in the path, commit-then-publish, the heartbeat carrying its status.

## Lessons

| # | Lesson | You'll be able to... |
|---|---|---|
| 1 | [The Subsystem Contract](01-the-subsystem-contract.md) | Say what the platform is and is not; build a config store with `ModifyIndex` and CAS and prove two writers cannot both win; build the epoch issuer and the lease as platform pieces; write the contract as a table; enforce one writer per prefix; give `N` processes stable names by claim and say who decides `N`; say what a Node is now. |
| 2 | [`driverpacksrc`](02-driverpacksrc.md) | Write a GStreamer element in Python; resolve `driverpack://file/<name>` and refuse the vendor form by name; loop a file with PTS rebased so time never goes backwards; restate the per-frame rule for an element author. |
| 3 | [`archivesink`, and the Archive as a Resource](03-archivesink-and-the-archive-as-a-resource.md) | State the promotion order and what each step protects; account for a kill at minute seven; replace the index table with a manifest and rebuild it from files; mark a fenced epoch and merge two resources; apply retention as a policy in the order that survives a crash; put events in buckets on the resource — recording or not — and say who writes them and for which subsystems. |
| 4 | [`vmsworker`: DriverPack as the Worker](04-vmsworker-driverpack-as-the-worker.md) | Claim a slot and inherit a lapsed one; run М9's loop over an assignment with М9's tests unchanged; take an epoch per camera by CAS and gate starts on a lease; publish the heartbeat with headroom; prove the controller is never on the recovery path; tell a zombie from a reassignment. |
| 5 | [`vmscontroller`, and the Second Subsystem](05-vmscontroller-and-the-second-subsystem.md) | CRUD by CAS with the revision bump in the controller; refuse what a client may not set; place by capacity, stored with a reason, adding a worker moves nothing; two controllers agree; redistribute a released slot and leave a lapsed one alone; measure the failure arithmetic; run a second subsystem through the same platform. |

## The demo the module is built backwards from

One box. `POST /cameras {"source": "driverpack://file/lobby.mp4"}` and within one worker pass a pipeline is recording into `<spool>/1/e1/`; ten minutes later the first segment is in the archive with a line in its manifest. Then:

```bash
systemctl stop vmscontroller          # nothing running stops; the console still lists the camera from the heartbeat
systemctl kill -s KILL vmsworker@w-1   # the open segment is lost; the closed one in the spool is promoted on restart
                                      # and the edit you made while it was dead is applied — from the store, not from anyone
```

Then the zombie on one box: `kill -STOP` the worker, start a second `vmsworker@w-1`, `kill -CONT` the first — it renews its lease, finds the newer epoch, fences itself and stops; the manifest names both epochs and marks the old one *fenced*. And finally a second subsystem — a controller and a worker that count seconds — through the same platform code, with a different prefix, proving the VMS is a subsystem and not the platform.

## What you can verify without hardware

Nearly all of it. [`vmsserver/`](vmsserver/README.md) is the five lessons as one runnable package, and its 43 tests need no GStreamer: the config store's persistence and CAS with four threads racing; the epoch issuer and the lease on a fake clock; the ACL; slots claimed, lapsed, inherited and released; the resource as a platform job mirroring any subsystem's buckets to a peer and restoring them; the promotion order on real files, the kill-at-minute-seven accounting, the manifest rebuilt from the archive alone, the fenced epoch on a timeline, retention per kind, event buckets recording or not; М9 Lesson 6's seven tests against the worker's loop, the epoch per camera, the restart with the controller object deleted, the zombie and the reassignment that is not one; the controller's refusals, stored placement by the workers' own capacity, two controllers racing to place forty cameras, scale-in redistributed and a crash left alone, the failure arithmetic with the clock, the console over real HTTP; and the counter subsystem. Every number in the lessons came out of those tests.

**Needs a box with GStreamer** — `python3-gi`, `gst-plugins-good` and `-bad`, М9's bench: the two elements and the actuator (`gstvms/`), the hour-long PTS run, `kill -9` mid-segment on real files, and the zombie with two real worker processes. `deploy/` has the `systemd` units: `vmscontroller.service`, `vmsworker@.service`, and the archive resource's policy on a timer.
