# vmsserver — М10, whole: the platform's shape on one box

The five lessons as one runnable package. No scheduler, no KVS, no database: two stores on disk, a controller that is the only writer, a worker that is DriverPack, an archive that is a resource — and a second, trivial subsystem that proves the platform knows nothing about video.

```
vmsserver/
  vmsplatform/                 the platform (named so because Python owns `platform`)
    variables.py               Lesson 1  a config store with ModifyIndex and check-and-set, as files; one writer per prefix
    objects.py                 Lesson 1  an object store: a directory
    epoch.py                   Lesson 1  the fencing-token issuer and the lease — generic
    contract.py                Lesson 1  Subsystem, Assignment, Heartbeat, Slot; the Controller and Worker bases; identity by claim
    events.py                  Lesson 3  the event log: buckets per unit per epoch on the resource, for any subsystem — generic
    resource.py                Lesson 3  the resource as a platform job: heartbeat, buckets over HTTP, retention by each subsystem's row, the mirror to a peer, restore
    eventindex.py              Lesson 3  the index over every subsystem's buckets on every resource — a cache
    spec.py                    Lesson 5  the controller as data: SubsystemSpec (rows, fields, derived rows, placement by name, snapshot, the two ACLs) and SpecController, the one controller every subsystem runs
    console.py                 Lesson 5  the console as data: SpecConsole over the same spec — the page, /spec, /<rows>, /where, /metrics with the subsystem's prefix, /marks, the writes with the spec's refusals; a subsystem registers extra routes
    console.html               Lesson 5  the one page for every subsystem: reads /spec, builds the list and the forms from the fields; timeline and player only when the spec says media
  vms/                         the VMS — the first subsystem
    reconciler.py              Lesson 4  М9 Lesson 6's loop, copied unchanged: the contract
    archive.py                 Lesson 3  the VMS's part of the resource under vms/<cam>/: spool → promote → manifest; the camera's event buckets; ArchivePolicy (repair, close, media retention) registered on the platform's resource
    worker.py                  Lesson 4  vmsworker: N pipelines against an assignment; an epoch per camera; a lease; the heartbeat with server, labels and capacity — on a box or in an allocation
    vms.subsystem.yaml         Lesson 5  the VMS's controller, as a spec: cameras numbered, eight operator fields, vms/retention/<cam> derived, labels-subset placement, the snapshot
    controller.py              Lesson 5  vmscontroller: the platform's SpecController run from the spec, in the VMS's words (create_camera, cameras)
    console.py                 Lesson 5  the console, its own process with its own token (the operator's rows, never placement): SpecConsole plus the VMS's two media routes, /timeline/<id> and /segment/<path>
    config.py                  the schema's Python view over the spec: row() and items()
    __main__.py                python3 -m vms worker | controller | console
  gstvms/                      Track 2 — needs GStreamer
    uri.py                     Lesson 2  driverpack://file/<name> resolved and refused — pure, no GStreamer
    driverpacksrc.py           Lesson 2  the element: looping, PTS rebased across the loop
    archivesink.py             Lesson 3  splitmuxsink into the spool; on fragment-closed, promote
    actuator.py                Lesson 4  driverpacksrc ! h264parse ! watchdog ! tee ! archivesink, per camera; the bus drained into (dead, posted)
  deploy/                      Quadlet, on М9's box: Containerfile (localhost/vmsserver:latest, the image М11 builds FROM), vmsworker@.container,
                               vmscontroller.container, vmsconsole.container, vms-archive-retain.container + .timer, vms.env.example, check-quadlet.sh
  tests/                       47 tests, milliseconds, no GStreamer
```

```bash
python3 tests/run.py                                   # 47 tests
PLATFORM_DIR=/data/platform python3 -m vms controller  # the console on :8080
WORKER_NAME=w-1 python3 -m vms worker                  # with GStreamer: records; without: the fake actuator
python3 -m vms worker                                  # no name: claims the first free slot — a lapsed one first
```

## What each lesson's deliverable became

| Lesson | Deliverable | Test |
|---|---|---|
| 1 | a config store that survives a restart and refuses a stale CAS; one writer per prefix; the contract a second team could implement; names by claim | `test_lesson1_platform.py` — including *the platform knows nothing about video* (no import from `vms/`, and not the word) and *identity by claim* (two claims, a lapse inherited, a release, the scheduler's index) |
| 2 | `driverpacksrc` running for an hour with monotonic PTS; the refusal of a vendor URI | `test_lesson2_driverpacksrc.py` — the URI logic here; the element and the hour on a box with GStreamer |
| 3 | kill the worker at minute seven: six promoted, one closed-but-not-promoted picked up on restart, the open one lost; rebuild the manifest from the files | `test_lesson3_archive.py` — the acknowledgement order, `closed_in_spool`, `repair()`, the fenced epoch on the timeline, two resources merged, retention per kind, event buckets recording or not — silent included — closed, counted onto media, fenced, rebuilt |
| 4 | М9's four failures against the worker with its tests passing unchanged; the zombie on one box | `test_lesson4_worker.py` — М9 Lesson 6's seven, then the assignment, the epoch per camera, the restart with the controller stopped, a nameless replacement inheriting the lapsed slot, the zombie fenced at the slot, the reassignment that is not one |
| 5 | one box, two subsystems, one console; the controller stopped, the worker killed, recording resumes | `test_lesson5_controller.py` and `test_second_subsystem.py` — refusals, stored placement by the capacity each worker reports, adding a worker moves nothing, two controllers agree, scale-in redistributed and a crash left alone, the failure arithmetic, the console over HTTP, a retry across two consoles, the counter subsystem and its console for free; `test_deploy_units.py` — the Quadlet units against the package |

## The three lines the code holds

**The controller is never on the recovery path.** `test_restart_with_the_controller_stopped` deletes the controller object, starts a fresh worker under the same name, and asserts it records — from its assignment, with the next epoch for each camera.

**The controller never decides how many workers there are.** It has no scheduler client and no `count`. `test_scale_in_releases_a_slot_and_the_controller_redistributes` shows the only thing it does about worker numbers: moving the cameras of a slot whose holder *said* it was stopping — and leaving a merely silent one alone for Nomad. The workers export `headroom`; `/metrics` serves it; whoever runs `count` reads it.

**The platform knows nothing about video.** `test_the_platform_knows_nothing_about_video` greps `vmsplatform/` — `events.py` included — for an import from `vms/` and for the word *camera*, and `test_second_subsystem.py` runs a controller and a worker that count seconds through the same base classes with a different prefix, and writes their events into `counter/b/…` on the same resource.

## Verified where

The 47 tests ran in the authoring sandbox (Python 3.11) and on the author's machine (3.10). `gstvms/` — the two elements and the actuator — is written to GStreamer's Python binding and not exercised here; the logic it calls (`vms.archive.ArchiveResource.promote`, the URI resolution) is. The hour-long PTS run, `kill -9` mid-segment on real files, and the zombie with two real worker processes are the box's.
