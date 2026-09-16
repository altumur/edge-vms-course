# Lesson 11 — On М9's Box

**Module:** ServerVMS — the platform's shape on one server (Module 10)
**You will build:** the whole of М10 as Quadlet units on the box М9 built — one image, ten units, everything that must outlive an OS update under `/data`, the mounts saying what the ACL says, and М9's health check reading this recorder's archive so that an update that records nothing is rolled back.
**Time:** ~60 minutes.

## Why this lesson exists

Every lesson so far said "on М9's box" and shipped nothing that proves it. This one turns the sentence into units the tests read: the processes of Lessons 4–7 — the worker, the recorder and their controllers, the console — the gateway and its controller of Lesson 8, the detector and its controller of Lesson 9, and the resource process of Lesson 10 — Quadlet files over one image, on the data partition, with the rollback decision reaching the new worker.

> **What you can verify without hardware.** `tests/test_deploy_units.py`: each unit's `Exec=` is an entrypoint `python3 -m vms` has; every volume is under `/data`; the controller has no archive and no spool, the console cannot write the spool, the worker has no spool at all, only the recorder writes segments; the Containerfile carries the three packages and no database. `deploy/check-quadlet.sh` is the generator's dry-run, for a box with podman. RAUC, the health check and a real update are the bench's.

## Prerequisites

- **М9 Lessons 1–5** — RAUC, the data partition, Quadlet, the health check ladder.
- **Lessons 4–10** — the processes these units run.

## Learning objectives

1. Express every process of the module as a Quadlet unit over one image.
2. Put everything an OS update must not lose under `/data`, and read the ACL off the mounts.
3. Extend М9's health check so that *footage is actually being written* reads this worker's archive.

---

## Step 1 — Eight units, one image


Everything above runs on the box М9 built, and the module should say so as units rather than as a sentence. `deploy/` is ten Quadlet units over one image (`Containerfile` → `localhost/vmsserver:latest`, the same image М11's jobs start `FROM`): `vmsworker@.container` (the slot is the instance name — `systemctl start vmsworker@w-1`), `vmscontroller.container`, `recworker@.container` and `vmsreccontroller.container` (Lesson 5: the only unit with the spool, the only writer of segments), `vmsconsole.container`, `vmsresource.container` — the resource process of Lesson 10: heartbeat, policy pass, `/events` from the event database, the unit М11 runs as the `resource` job — and, from Lessons 7 and 8, `vmsgateway@.container`, `vmslivecontroller.container`, `vmsdetworker@.container` and `vmsdetcontroller.container`. М9's four units become these: `worker.container` is `vmsworker@`, `postgres.container` is gone (М10 Lesson 1 — the platform's stores on the data partition are the truth), `spool-uploader.container` is gone with it (the spool is the archive resource's staging, promoted locally), and `vms-agent.container` stays М9's business. What every unit has in common is М9 Lesson 5's rule: the image is in the rootfs slot, everything the box must not lose is under `/data` — `/data/platform` (the stores), `/data/spool`, `/data/archive`, `/data/media`, `/data/config/vms.env` — so an A/B update that boots the other slot finds the same cameras, the same assignment and the same footage.

## Step 2 — The mounts say what the ACL says

The mounts say the same thing the ACL says, in bytes: the controller mounts no archive and no spool (it has nothing to do with footage); the console mounts the spool read-only and the archive to serve from; the worker mounts the archive for its events and the media read-only, and no spool; the recorder is the only unit with the spool, the only writer of segments, and mounts no media — it never reads a camera; the resource process writes its heartbeat into the platform and its policy into the archive, and cannot touch the spool. `tests/test_deploy_units.py` reads the units and checks exactly that, and that each `Exec=` is an entrypoint `python3 -m vms` actually has. `deploy/check-quadlet.sh` is М9 Lesson 4's generator dry-run for these files, for the bench.

## Step 3 — The health check reaches the new worker

And the health check reaches the new worker. М9's `rauc-health-check` — the script that decides whether an OS update is kept — had a bottom rung, *footage is actually being written*, that read the old recorder's metrics. It now tries the VMS first: `vms_workers_live ≥ 1` from the console's `/metrics`, `vms_cameras_running ≥ 1` when `/cameras` has an enabled camera configured, `rec_recordings_running ≥ 1` from `/rec/metrics` when `/rec/recordings` has an enabled recording, and a segment promoted into `/data/archive/rec/` within two segment lengths — read from the disk, on the box, asking nothing outside it. An update that boots perfectly, starts every unit and records nothing is rolled back by the same unit that rolled back М9's.

**Deliverable:** one box, four subsystems, one console, ten units. `POST /cameras` holds a camera within one worker pass; open `/` and watch it appear in the list, press *Record*, then watch its first promoted segment on the timeline, and play it; press *Live* and watch `live/streams/1` appear, get placed and answered; add a line-crossing model to the camera and watch its events arrive under `det/1-linecross/e1/` on the resource; stop the controller and show recording, the read model and a worker restart all unaffected; kill the worker and show the edit made meanwhile applied on restart; `rauc install` a bundle that starts every unit and records nothing, and watch it rolled back; every test green, with a written statement of what the platform knows about the VMS — a prefix, an assignment shape, a heartbeat shape, and nothing else.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `check-quadlet.sh` exits 2 | No podman here; run it on the bench or in CI. The generator is the only thing that parses `[Container]`. |
| The console cannot serve a segment | Its archive volume is missing or read-only; the console reads the archive and writes only its own marks into it. |
| The health check passes on a box with no cameras | By design and stated: no cameras, no footage to prove; rows 1–2 only. Commission a camera before the first update. |
| A good update is rolled back at boot | `rauc-mark-good` judged before a segment could exist: its sleep must exceed a segment length plus startup. |

## Recap

- Eight Quadlet units over one image, `localhost/vmsserver:latest`, which М11's image builds `FROM`.
- Everything an OS update must not lose is under `/data`; the mounts encode the ACL.
- М9's health check tries the VMS first: a live worker, a camera held when one is configured, a recording running when one is configured, a segment promoted within two segment lengths.

## Exercises

1. Move the archive volume to a second disk. Which units change, and which rows in the store?
2. The resource process runs its policy pass every 600 s. Write the interval as a product decision: what is lost if it runs hourly, and what if it runs every minute — for media, for buckets, and for the event database's `forget`?
3. The health check reads `/metrics` on `127.0.0.1:8080`. The console is now a system job on every server in М11 — which server's console should a box's health check ask, and why only that one?

## Where this is going

One box runs the platform's shape: two stores, a resource, one controller and a console per subsystem, and three subsystems to prove none of them is special. [**М11 — ClusterVMS**](../М11_ClusterVMS/README.md) puts a scheduler under the same processes: the stores become raft and an object store, the units become jobs, and a server dying moves a worker.
