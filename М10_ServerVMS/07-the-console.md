# Lesson 7 — The Console

**Module:** ServerVMS — the platform's shape on one server (Module 10)
**You will build:** the console — its own process with its own token, the operator's rows and never placement; the page, built from `/spec`; the console as data, `SpecConsole` from the same YAML the controller runs from; a retry answered the same by any instance; and one console process that mounts every subsystem it fronts.
**Time:** ~90 minutes.

## Why this lesson exists

Lesson 6 left the box with a controller nobody can talk to. Somebody has to serve the operator, and the module's answer is the same shape as everywhere else: a process that holds nothing, reads heartbeats, writes the operator's rows by CAS through the platform's class, and could not place a camera if it tried — its token does not allow it. The console is also where the course first has a screen since М8, and the page is deliberately small: one file, no framework, playing what the resource holds one segment at a time, because the web tier's problems must never reach the recorder.

The lesson's second half is the claim that made the controller a YAML in Lesson 6 applied to the console: everything a console needs — the rows' name, how a unit is identified, the operator's fields, what counts as running — is already in the spec, so the console is one class for every subsystem, and one process can front several of them.

> **What you can verify without hardware.** `tests/test_lesson6_controller.py::test_the_console_over_http` (the whole surface on a real port), `test_a_retry_that_lands_on_another_console_is_one_camera`, and `tests/test_lesson9_det.py::test_one_console_mounts_every_subsystem_it_fronts`. The page itself needs a browser; everything it calls is tested.

## Prerequisites

- **Lesson 6** — the controller and `SpecController`; the two ACLs.
- **Lesson 3** — the manifest and the promoted segment: what the page plays.
- **М12 Lesson 3** (read ahead) — the read model from heartbeats and *who serves browsers*; this lesson is the one-box version.

## Learning objectives

1. Run the console as its own process with a token for the operator's rows, and show the store refusing it a placement.
2. Read the camera list from heartbeats and play a promoted segment through `/segment/<path>` with `Range`.
3. Build the page from `/spec` so that it names no camera.
4. Keep the idempotency key in the store so a retry landing on another instance is the same request.
5. Mount several subsystems' consoles in one process, under their names.
6. Say who answers `/events` — and that it is not the console.

---

## Step 1 — The console over HTTP

`w2cplatform/console.py` — `SpecConsole`, standard library — is М12 Lesson 3's console on one box: reads never touch a worker, writes go through the controller. It is the platform's, not the VMS's, and it runs from the same YAML the controller does; `vms/console.py` is the two routes only a VMS has, `/timeline` and `/segment`, registered as extras.

```
GET  /spec                           -> {name: vms, rows: cameras, id: numeric, fields: [...], media: true, metrics: {...}}   what the page reads first
POST /cameras     (Idempotency-Key)  -> 201 {id: 1, worker: null}    the same POST again -> the same 201, one camera
PUT  /cameras/1   {"worker": "w-9"}  -> 400                           refused
GET  /cameras                        -> rows from heartbeats: phase running, server srv-1, age
GET  /where/1                        -> {"worker": "w-1", "reason": "…", "directory": "w-1", "scans": 1}
GET  /timeline/7?from&to             -> the manifest, fenced segments marked
POST /marks {"cam": 1, "note": …}   -> 201 {subsystem: console, unit: <host:pid>, bucket: console/<host:pid>/e1/…}
                                        the operator's observation is the CONSOLE's event, never a worker's (Lesson 4, Step 3a)
GET  /metrics                        -> vms_epoch_conflicts, vms_workers_live, vms_worker_headroom{worker="w-1"} 49,
                                        vms_headroom, vms_worker_load{worker="w-1"} 0.020, vms_cameras_running 1     the autoscaler scrapes this
GET  /                               -> the page (w2cplatform/console.html), built from /spec
DELETE /cameras/1                    -> 200 {deleted: 1}; the row is marked deleted, its assignment goes, its footage stays until retention
GET  /segment/vms/1/e1/<start>Z.mp4  -> the bytes of one promoted segment, Range honoured — what the page's <video> asks for
```

**The page.** `GET /` is the screen М8 had and М9 lost when the archive moved on-box: the camera list on the left (name, phase, worker and server, the heartbeat's age, greyed when stale — the read model, drawn), and for the chosen camera its timeline from `/timeline/<id>` — recorded spans in blue, *watched but not recorded* spans (Lesson 3's events-only buckets) in amber, fenced epochs faded — and a player. Click a span and the console serves that segment's bytes from the archive resource through `/segment/<path>`; when it ends the next one starts. The page also writes, and only the way the API does: *Add a camera* is a `POST /cameras` with a fresh `Idempotency-Key` per click (so a double click is one camera), the edit panel is a `PUT` of the operator's fields — every save a new revision, the worker restarting the pipeline — with enable/disable as one of them, and *Delete* asks first. What the page cannot offer is what the controller refuses: there is no field for the worker, the epoch or the phase, and if a client sends one anyway the 400 is printed under the form. It is one file, no framework, and it plays what the resource holds — *one segment at a time*. It is also not the VMS's file: the page reads `/spec` first and builds the list, the add form and the edit form from the fields it finds there; the timeline and the player appear because the spec says `media: true`. The word *camera* does not occur in it, and the test checks. Gapless playback of fragmented MP4 through MSE, live view, transcoding and TURN are the gateway's job (М12, *Who serves browsers*), and the reason the page is deliberately this small is the same reason the console never calls a worker: the web tier's problems must never reach the recorder.

## Step 2 — Two processes, two tokens, one class

`python3 -m vms console` serves it — **its own process**, not the controller's. `python3 -m vms controller` runs the loop and nothing else, every five seconds: `unplace_deleted()`, `ensure_placed()`, which places cameras that have no placement onto the workers it sees, `redistribute()`, which moves the cameras of a *released* slot, and `publish_snapshot()`. Nothing else, ever — not a rebalance (an operator asks for that), not a heal (Nomad restarts workers), not a scale (the autoscaler does that, from `/metrics`).

**Two processes, two tokens, one class.** Both are `VmsController` — the same spec, the same CAS writes — but their tokens differ, and that is where "the controller is the only writer" gets precise. The console's token (`SPEC.acl_console()`) may write the *operator's* rows: `vms/cameras/*`, `vms/next_id`, `vms/retention/*`. The controller's (`SPEC.acl_controller()`) may write *placement*: `vms/workers/*`, `vms/placement/*`, `vms/slots/*`, and the snapshot. So a `POST /cameras` writes a row and answers `worker: null`; the placement is the controller's next pass, and if the console tried it the store would say 403 — `test_the_console_over_http` makes it try. A `DELETE` is the same in reverse: the console marks the row, the controller's pass takes the assignment back. Which is why the console may run on *every* server while the controller stays at `count = 1`: a person is waiting on the console, so it is wherever they can reach; nobody waits on the controller, whose whole job is one pass every five seconds that a second instance would merely repeat. Neither number is about correctness — that is CAS — one is about a screen and the other about economy.

## Step 3 — The console as data

**The console as data, like the controller.** Lesson 6, Step 6 made the controller a spec; the console is the same move. Everything a console needs — what the rows are called, how a unit is identified, which fields an operator may set and their types, what "running" means for the gauge — is already in `vms.subsystem.yaml`, so `SpecConsole(ctl)` serves the page, `/spec`, `/cameras`, `/where`, `/metrics` with the `vms_` prefix, `/marks` and the three writes with the spec's refusals, and knows nothing about video. What the VMS adds is registered, not subclassed: `extra(handler, method, path, query)` gets every request the generic routes did not claim, and `vms/console.py` answers two of them from the archive. A subsystem with no media registers nothing and gets a console with no timeline. The ACL is the spec's too: `acl_console()` and `acl_controller()` are derived from the same file, so adding a field or a derived row never touches a policy by hand.

## Step 4 — A retry that lands on another console

One thing has to move for that to be true. The `Idempotency-Key` cache was a dict in the console's process, and with a console per server a client's retry may land on a different server — and make a second camera. So the key is a Variable: `vms/idem/<key>` is *claimed* by a create-only CAS before the write and filled with the reply after it; a second instance that finds the claim waits for the reply and serves it, and never repeats the write. `test_a_retry_that_lands_on_another_console_is_one_camera` runs two consoles over one store and retries across them; keys older than a day are pruned on the way past. It is the same move as everything else in this lesson: state that two instances must agree on goes in the store, by CAS, and the instances stay interchangeable.

## Step 5 — One console for every subsystem

Lessons 7 and 8 add two more subsystems to the box, live video and detectors. **Does each get its own console?** Each gets `SpecConsole` over its YAML for free, and it needs it — `det_worker_headroom` is what the autoscaler moves `N` on, `/det/unplaceable` and `/det/where` are what an operator of the platform asks — but it does not get a *screen*, because the person's world is cameras: nobody configures "detector d-3", they open camera 7 and add a model to it. So the VMS console **mounts** the other subsystems it fronts: `w2cplatform.console.Mount` runs the VMS's `SpecConsole` at `/` and every other one under its name — `/live/spec`, `/det/units`, `/det/where/7-linecross`, `/det/metrics` — each the same class over its own YAML, all with the console's token, which now carries the operator's rows of every subsystem it fronts (`SPEC.acl_console() + LIVE_SPEC.acl_console() + DET_SPEC.acl_console()`). One process, one port, one page: the camera's edit panel grows a *Detectors* section that lists this camera's models with the worker's word on each (running · 2 events · d-2), adds one with `POST /det/units {name: "7-linecross", cam, kind, params}`, and enables, disables or deletes it through `/det/units/<name>`. `GET /mounts` says what the process fronts. A new subsystem is a YAML, a worker, and a path.

```
GET /mounts          -> {root: vms, mounts: {live: {rows: streams, id: cam, …}, det: {rows: units, id: name, …}}}
GET /live/spec       -> the live subsystem's spec; /live/streams, /live/where/7, /live/metrics — the same routes under a name
GET /det/units       -> the det subsystem's rows with the read model; POST /det/units, PUT and DELETE /det/units/<name>
```

## Step 6 — The ticks on the timeline come from somewhere else

The page has drawn ticks from `/events?cam=7` since Step 1, and nothing in this lesson answers that route: the console holds no event database. It asks — `MergedIndex`, the platform's — every resource it finds by heartbeat, merges by time, and fences each event by its unit's own subsystem's epoch, which only the console's rows know. Who it asks, what a resource holds, why none of that is a subsystem — and the page's events list, live feed and Mark button — are [Lesson 10](10-events-the-database-that-is-a-cache.md); until then the timeline is empty and the state line says which resource did not answer.

**Deliverable:** the console as its own unit (`console.container`, Lesson 11) serving the page; a camera added from the page, edited, disabled and deleted; *Record* pressed — a row under `rec/recordings/` through the mount at `/rec/…`, placed by the rec controller — and a segment played; two console instances over one store answering the same retry with one camera; `/mounts` naming what the process fronts.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| The console's PUT applies twice | The client made a new `Idempotency-Key` on retry. The key belongs to the intent. |
| A retry on the other instance made a second camera | The key is in the process, not the store. `vms/idem/<key>` by create-only CAS; the second instance waits for the first's reply. |
| `POST /cameras` answers `worker: null` forever | The controller is not running. The console wrote the row; placement is the controller's pass — that is the split, not a bug. |
| `/timeline/7` marks nothing fenced on one box | М10's route does not pass the current epoch; М11's does. Fencing on the timeline is the cluster's concern. |
| The page names a camera | It is not reading `/spec`. The test greps the page after its comments for the word. |
| `/det/spec` is 404 | The console was started without the mount. `python3 -m vms console` mounts `live` and `det`; a console built from `SpecConsole` alone fronts one subsystem. |

## Recap

- The console is its own process with its own token: the operator's rows, never placement; the store refuses the crossing.
- The page reads `/spec` and builds itself; it plays what the resource holds, one segment at a time, and never calls a worker.
- `SpecConsole` runs from the same YAML as `SpecController`; the live and det subsystems get a console with no console code.
- The idempotency key is a Variable, so any instance answers a retry the same way; the console runs on every server with nothing in front.
- One process mounts every subsystem it fronts: the VMS at `/`, the others under their names.
- The console holds no event database: `/events` asks the resources and merges, fenced by every subsystem's epochs (Lesson 10).

## Exercises

1. Give the console a direct `vars.put` for the "quick fix" of a camera name. Write the sequence in which the controller and the console overwrite each other.
2. Add a `move` endpoint to the console and defend it against the refusal list: who may call it, and what must it record?
3. Make the page gapless: fragmented MP4 through MSE instead of one segment at a time. List what the console would have to hold, and say where the design record puts that job.
4. Two consoles, one key, both claims arriving in the same millisecond. Trace the create-only CAS and say which one writes and what the other serves.

## Where this is going

The operator has a screen, and the screen plays yesterday. [**Lesson 8**](08-live-video-the-second-subsystem.md) gives it *now* — and finds that live video is not a feature of the console but a subsystem of its own, with workers scaled by the audience.
