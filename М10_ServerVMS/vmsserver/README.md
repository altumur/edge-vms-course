# vmsserver — М10, whole: the platform's shape on one box

The eleven lessons as one runnable package. No scheduler, no KVS, no database: two stores on disk, a controller that is the only writer, a worker that is DriverPack and holds the camera, a recorder that is the only writer of footage, an archive that is a resource — and three more subsystems through the same platform, which proves it knows nothing about video.

```
vmsserver/
  w2cplatform/                 the platform (named so because Python owns `platform`)
    variables.py               Lesson 1  a config store with ModifyIndex and check-and-set, as files; one writer per prefix
    objects.py                 Lesson 1  an object store: a directory
    epoch.py                   Lesson 1  the fencing-token issuer and the lease — generic
    contract.py                Lesson 1  Subsystem, Assignment, Heartbeat, Slot; the Controller and Worker bases; identity by claim
    events.py                  Lesson 3  the event log: buckets per unit per epoch on the resource, for any subsystem — generic
    resource.py                Lesson 3  the resource as a platform job: heartbeat, buckets over HTTP, retention by each subsystem's row, the mirror to a peer, restore
    eventdatabase.py           Lesson 10  EventDatabase — the database a resource keeps over its own buckets, a cache; MergedIndex — what a console has instead: every live resource's /events, merged
    spec.py                    Lesson 6  the controller as data: SubsystemSpec (rows, fields, derived rows, placement by name — requires, servers, near — snapshot, the two ACLs) and SpecController, the one controller every subsystem runs
    console.py                 Lesson 7  the console as data: SpecConsole over the same spec — the page, /spec, /<rows>, /where, /metrics with the subsystem's prefix, /marks, the writes with the spec's refusals; a subsystem registers extra routes;
                               Mount — one process fronting several subsystems, the root at / and the others under their names (/live/…, /det/…)
    console.html               Lesson 7  the one page for every subsystem: reads /spec, builds the list and the forms from the fields; timeline and player only when the spec says media
  vms/                         the VMS — the first subsystem
    reconciler.py              Lesson 4  М9 Lesson 6's loop, copied unchanged: the contract
    archive.py                 Lesson 3  the archive's two trees: rec/<cam>/ (the recorder's media: spool → promote → manifest, media only) and vms/<cam>/ (the worker's event buckets); ArchivePolicy (repair, media retention by the recording row) registered on the platform's resource as the rec hook
    worker.py                  Lesson 4  vmsworker: holds N cameras against an assignment — one connection, one fan-out (live_url) each, events into the resource, no footage; an epoch per camera; a lease; the heartbeat with server, labels and capacity — on a box or in an allocation
    rec.subsystem.yaml         Lesson 5  the FOURTH subsystem, as a spec: recordings named by camera, retention_days, requires: resource, servers: distinct — the only one placed on the archive; near: vms, the affinity
    recworker.py               Lesson 5  RecWorker: VmsWorker over rec/recordings/*, fed by the worker's tee from the VMS heartbeat — shared memory on the same server, RTSP from another; writes rec/<cam>/e<epoch>/ on its server's archive; re-subscribes when the camera's holder moves; promotes closed segments
    vms.subsystem.yaml         Lesson 6  the VMS's controller, as a spec: cameras numbered, seven operator fields, vms/retention/<cam> derived, labels-subset placement, requires: resource (for its events), the snapshot
    controller.py              Lesson 6  vmscontroller: the platform's SpecController run from the spec, in the VMS's words (create_camera, cameras)
    console.py                 Lesson 7  the console, its own process with its own token (the operator's rows, never placement): SpecConsole plus the VMS's media routes —
                               /timeline/<id>, /segment/<path>, and the WHEP door /whep/<cam> that creates a fan-out on the first viewer and proxies to its gateway
    live.subsystem.yaml        Lesson 8  the SECOND subsystem, as a spec: live fan-outs named by camera, placed on gateways by viewer headroom, labels for where viewers are
    det.subsystem.yaml         Lesson 9  the THIRD subsystem, as a spec: one model on one camera, named by the operator, placed on GPU-labelled workers by stream headroom
    detworker.py               Lesson 9  DetWorker: runs a Model against the camera's fan-out, writes what it saw into det/<unit>/e<epoch>/ on the resource under its own epoch
    liveworker.py              Lesson 8  LiveWorker, a worker whose unit is a camera's fan-out and whose capacity is viewers: one subscription to the worker's
                               RTSP fan-out (live_url) per camera, N webrtcbin peers behind it, WHEP (POST /whep/<cam>, DELETE /whep/session/<id>), demand-created and demand-deleted units
    resource.py                Lesson 10  the resource process: the platform's Resource with the recorder's ArchivePolicy registered and an EventDatabase attached; /manifest and /segment plugged in — the same function М11 runs as the resource job
    config.py                  the schema's Python view over the spec: row() and items()
    __main__.py                python3 -m vms worker | controller | recorder | reccontroller | console | resource | gateway | livecontroller | detworker | detcontroller
  gstvms/                      Track 2 — needs GStreamer
    uri.py                     Lesson 2  driverpack://file/<name> resolved and refused — pure, no GStreamer
    webrtc.py                  Lesson 8  the gateway's media path (Track 2): rtspsrc ! depay ! parse ! pay ! tee per camera, queue ! webrtcbin per viewer, WHEP without trickle
    livesrv.py                 Lesson 4  FanOut: the worker's RTSP server (GstRtspServer) re-serving each camera's loopback RTP port as rtsp://<server>:8554/<cam> — shared, TCP-interleaved
    driverpacksrc.py           Lesson 2  the element: looping, PTS rebased across the loop
    archivesink.py             Lesson 3  splitmuxsink into the spool under rec/; on fragment-closed, promote
    actuator.py                Lesson 4  GstActuator: driverpacksrc ! h264parse ! watchdog ! tee ! rtph264pay ! udpsink, per camera, published by FanOut; the bus drained into (dead, posted)
                               Lesson 5  GstRecActuator: shmsrc (the worker on this server) or rtspsrc ! rtph264depay (another server), then h264parse ! watchdog ! archivesink — the recorder's pipeline
  deploy/                      Quadlet, on М9's box: Containerfile (localhost/vmsserver:latest, the image М11 builds FROM), vmsworker@.container (no spool),
                               recworker@.container (the only writer of segments), vmscontroller.container, reccontroller.container, console.container, resource.container,
                               liveworker@.container, livecontroller.container, detworker@.container, detcontroller.container, vms.env.example, check-quadlet.sh
  tests/                       59 tests, milliseconds, no GStreamer
```

```bash
python3 tests/run.py                                   # 59 tests
PLATFORM_DIR=/data/platform python3 -m vms controller  # the console on :8080
WORKER_NAME=w-1 python3 -m vms worker                  # with GStreamer: holds cameras, rtsp://<box>:8554/<cam>; without: the fake actuator
RECORDER_NAME=r-1 python3 -m vms recorder              # subscribes to the fan-out, writes rec/<cam>/ into the archive
python3 -m vms worker                                  # no name: claims the first free slot — a lapsed one first
```

## What each lesson's deliverable became

| Lesson | Deliverable | Test |
|---|---|---|
| 1 | a config store that survives a restart and refuses a stale CAS; one writer per prefix; the contract a second team could implement; names by claim | `test_lesson1_platform.py` — including *the platform knows nothing about video* (no import from `vms/`, and not the word) and *identity by claim* (two claims, a lapse inherited, a release, the scheduler's index) |
| 2 | `driverpacksrc` running for an hour with monotonic PTS; the refusal of a vendor URI | `test_lesson2_driverpacksrc.py` — the URI logic here; the element and the hour on a box with GStreamer |
| 3 | kill the recorder at minute seven: six promoted, one closed-but-not-promoted picked up on restart, the open one lost; rebuild the manifest from the files | `test_lesson3_archive.py` — the acknowledgement order, `closed_in_spool`, `repair()`, the fenced epoch on the timeline, two resources merged, retention per owner, two trees for one camera — the worker's buckets recorded or not, silent included, the recorder's media beside them |
| 4 | М9's four failures against the worker with its tests passing unchanged; the zombie on one box | `test_lesson4_worker.py` — М9 Lesson 6's seven, then the assignment, the epoch per camera, `live_url` in the heartbeat, the restart with the controller stopped, a nameless replacement inheriting the lapsed slot, the zombie fenced at the slot, the reassignment that is not one |
| 5 | *Record* pressed; a segment in `rec/1/e1/`; the worker moved and the recorder re-subscribed under `e2`; *Stop recording* | `test_lesson5_recorder.py` — the recording placed on the archive (`requires: resource`, `servers: distinct`) beside the camera's worker (`near: vms`, the reason saying so) and fed from its tee through shared memory, or over RTSP when placed away from it, under the recorder's epoch, the segment promoted under `rec/` while the worker's tree stays events-only, the re-subscription on failover, the recording that waits while nobody holds the camera, the toggle turned off |
| 6 | one box, one controller; the controller stopped, the worker killed, the cameras held again | `test_lesson6_controller.py` — refusals, stored placement by the capacity each worker reports, adding a worker moves nothing, two controllers agree, scale-in redistributed and a crash left alone, the failure arithmetic |
| 7 | the console as its own process; a camera added, edited, recorded, played, deleted from the page; two consoles answering one retry | `test_lesson6_controller.py::test_the_console_over_http` and `…is_one_camera` — the whole surface on a real port, a retry across two consoles; `test_lesson9_det.py::test_one_console_mounts_every_subsystem_it_fronts` |
| 8 | press *Live*; fifty tabs, one subscription; a gateway killed, the next offer answered by the survivor | `test_lesson8_live.py` — the first viewer creates the fan-out and the controller places it, fifty viewers one subscription and the worker unchanged, the grace period and the gateway deleting its own unit, a dead gateway's fan-outs moved to the survivor, placement by label, two subsystems sharing the platform |
| 9 | a model added to the camera from the page, its events beside the VMS's on the resource | `test_lesson9_det.py` — a model placed on the GPU worker and writing its own buckets under its epoch, a camera that stops leaving the model waiting, the unplaceable model placed when a GPU arrives, three subsystems' events on one camera's timeline through the console's index and fenced by their own epochs |
| 10 | three subsystems' events on one timeline through the resource process and the console; the database rebuilt to the same rows; retention taking the rows with the file | `test_lesson10_events.py` |
| 11 | the box: ten units, one image, an update that records nothing rolled back | `test_deploy_units.py` — the Quadlet units against the package: entrypoints, `/data` volumes, the mounts as the ACL, the image's contents |

## The three lines the code holds

**The controller is never on the recovery path.** `test_restart_with_the_controller_stopped` deletes the controller object, starts a fresh worker under the same name, and asserts it holds its cameras again — from its assignment, with the next epoch for each; the recorder re-subscribes to the new fan-out the same way, from the heartbeat.

**The controller never decides how many workers there are.** It has no scheduler client and no `count`. `test_scale_in_releases_a_slot_and_the_controller_redistributes` shows the only thing it does about worker numbers: moving the cameras of a slot whose holder *said* it was stopping — and leaving a merely silent one alone for Nomad. The workers export `headroom`; `/metrics` serves it; whoever runs `count` reads it.

**The platform knows nothing about video.** `test_the_platform_knows_nothing_about_video` greps `w2cplatform/` — `events.py` included — for an import from `vms/` and for the word *camera*.

## Verified where

The 59 tests ran in the authoring sandbox (Python 3.11) and on the author's machine (3.10). `gstvms/` — the two elements, the two actuators and the RTSP fan-out — is written to GStreamer's Python binding and not exercised here; the logic it calls (`vms.archive.ArchiveResource.promote`, the URI resolution) is. The hour-long PTS run, `kill -9` mid-segment on real files, and the zombie with two real worker processes are the box's.
