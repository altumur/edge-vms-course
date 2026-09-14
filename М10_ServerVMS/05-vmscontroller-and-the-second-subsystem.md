# Lesson 5 — `vmscontroller`, and the Second Subsystem

**Module:** NodeVMS — the platform's shape on one Node (Module 10)
**You will build:** the controller — the only writer of `vms/*`, camera CRUD and placement by CAS, stored with a reason, safe at two, never needed to recover, never deciding how many workers there are — the console over it, the failure arithmetic measured process by process, a second subsystem through the same platform code — and a third, live video, whose workers are gateways scaled by viewers.
**Time:** ~150 minutes.

## Why this lesson exists

Somebody has to write configuration, and the module's answer is: exactly one thing, and it is not the worker and not the console. М9 gave the Node its own database so that an operator could edit a camera with everything above the Node unreachable — an argument about the *domain*, which may be down. Inside a cluster the store is one raft, the workers are stateless, and a single writer keeps every property М9 wanted while dropping the one it paid for. The controller is that writer.

It is also the process most likely to be built wrong, because "one controller" invites state. So the lesson spends its second half on the two properties that keep it honest — it holds nothing and is correct by CAS; it is never on the recovery path — and its last step on the proof that the shape is not special: a second subsystem, a controller and a worker that count seconds, dropped onto the same platform with a different prefix.

> **What you can verify without hardware.** All of it: `tests/test_lesson5_controller.py` and `tests/test_second_subsystem.py` — refusals, stored placement, *adding a worker moves nothing*, two controllers racing to place forty cameras, capacity read from the workers' heartbeats, budgeted rebalance, scale-in redistributing a released slot and a crash moving nothing, the failure arithmetic with the clock, the console over real HTTP, and the counter subsystem. Every output below came out of them.

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
7. Build a second subsystem through the same platform and diff the two.

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

## Step 6 — The console

`psimplatform/console.py` — `SpecConsole`, standard library — is М12 Lesson 3's console on one box: reads never touch a worker, writes go through the controller. It is the platform's, not the VMS's, and it runs from the same YAML the controller does; `vms/console.py` is the two routes only a VMS has, `/timeline` and `/segment`, registered as extras.

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
                                        vms_headroom, vms_worker_load{worker="w-1"} 0.020, vms_cameras_recording 1   the autoscaler scrapes this
GET  /                               -> the page (psimplatform/console.html), built from /spec
DELETE /cameras/1                    -> 200 {deleted: 1}; the row is marked deleted, its assignment goes, its footage stays until retention
GET  /segment/vms/1/e1/<start>Z.mp4  -> the bytes of one promoted segment, Range honoured — what the page's <video> asks for
```

**The page.** `GET /` is the screen М8 had and М9 lost when the archive moved on-box: the camera list on the left (name, phase, worker and server, the heartbeat's age, greyed when stale — the read model, drawn), and for the chosen camera its timeline from `/timeline/<id>` — recorded spans in blue, *watched but not recorded* spans (Lesson 3's events-only buckets) in amber, fenced epochs faded — and a player. Click a span and the console serves that segment's bytes from the archive resource through `/segment/<path>`; when it ends the next one starts. The page also writes, and only the way the API does: *Add a camera* is a `POST /cameras` with a fresh `Idempotency-Key` per click (so a double click is one camera), the edit panel is a `PUT` of the operator's fields — every save a new revision, the worker restarting the pipeline — with enable/disable as one of them, and *Delete* asks first. What the page cannot offer is what the controller refuses: there is no field for the worker, the epoch or the phase, and if a client sends one anyway the 400 is printed under the form. It is one file, no framework, and it plays what the resource holds — *one segment at a time*. It is also not the VMS's file: the page reads `/spec` first and builds the list, the add form and the edit form from the fields it finds there; the timeline and the player appear because the spec says `media: true`. The word *camera* does not occur in it, and the test checks. Gapless playback of fragmented MP4 through MSE, live view, transcoding and TURN are the gateway's job (М12, *Who serves browsers*), and the reason the page is deliberately this small is the same reason the console never calls a worker: the web tier's problems must never reach the recorder.

`python3 -m vms console` serves it — **its own process**, not the controller's. `python3 -m vms controller` runs the loop and nothing else, every five seconds: `unplace_deleted()`, `ensure_placed()`, which places cameras that have no placement onto the workers it sees, `redistribute()`, which moves the cameras of a *released* slot, and `publish_snapshot()`. Nothing else, ever — not a rebalance (an operator asks for that), not a heal (Nomad restarts workers), not a scale (the autoscaler does that, from `/metrics`).

**Two processes, two tokens, one class.** Both are `VmsController` — the same spec, the same CAS writes — but their tokens differ, and that is where "the controller is the only writer" gets precise. The console's token (`SPEC.acl_console()`) may write the *operator's* rows: `vms/cameras/*`, `vms/next_id`, `vms/retention/*`. The controller's (`SPEC.acl_controller()`) may write *placement*: `vms/workers/*`, `vms/placement/*`, `vms/slots/*`, and the snapshot. So a `POST /cameras` writes a row and answers `worker: null`; the placement is the controller's next pass, and if the console tried it the store would say 403 — `test_the_console_over_http` makes it try. A `DELETE` is the same in reverse: the console marks the row, the controller's pass takes the assignment back. Which is why the console may run on *every* server while the controller stays at `count = 1`: a person is waiting on the console, so it is wherever they can reach; nobody waits on the controller, whose whole job is one pass every five seconds that a second instance would merely repeat. Neither number is about correctness — that is CAS — one is about a screen and the other about economy.

One thing has to move for that to be true. The `Idempotency-Key` cache was a dict in the console's process, and with a console per server a client's retry may land on a different server — and make a second camera. So the key is a Variable: `vms/idem/<key>` is *claimed* by a create-only CAS before the write and filled with the reply after it; a second instance that finds the claim waits for the reply and serves it, and never repeats the write. `test_a_retry_that_lands_on_another_console_is_one_camera` runs two consoles over one store and retries across them; keys older than a day are pruned on the way past. It is the same move as everything else in this lesson: state that two instances must agree on goes in the store, by CAS, and the instances stay interchangeable.

**The console as data, like the controller.** Step 7 will make the controller a spec; the console is the same move. Everything a console needs — what the rows are called, how a unit is identified, which fields an operator may set and their types, what "running" means for the gauge — is already in `vms.subsystem.yaml`, so `SpecConsole(ctl)` serves the page, `/spec`, `/cameras`, `/where`, `/metrics` with the `vms_` prefix, `/marks` and the three writes with the spec's refusals, and knows nothing about video. What the VMS adds is registered, not subclassed: `extra(handler, method, path, query)` gets every request the generic routes did not claim, and `vms/console.py` answers two of them from the archive. A subsystem with no media registers nothing and gets a console with no timeline. The ACL is the spec's too: `acl_console()` and `acl_controller()` are derived from the same file, so adding a field or a derived row never touches a policy by hand.

## Step 7 — The second subsystem

`tests/test_second_subsystem.py` defines a `CounterWorker` over `Subsystem("counter")` — a worker that adds a `step` each pass, heartbeats its values, and writes an event into `counter/b/e1/…` on the same resource every tenth tick through the platform's `EventLog` — and **no controller at all**. The counter's controller is a *spec*, ten lines of YAML the platform's `SpecController` runs from: a prefix, where the rows live, how a unit is named (`id: name` — the operator names counters; the VMS numbers cameras), the operator's fields with types and defaults, which heartbeat field is capacity. Refusals, CAS, revision bumps, placement by capacity with a reason, redistribution, the read model and the snapshot all come with it — and so does the console: `test_the_second_subsystem_gets_a_console_for_free` puts `SpecConsole` over the counter's controller and gets `/spec` naming `units` and two fields, `POST /units` with the spec's refusal, `/where/a`, `counter_workers_live` and `counter_units_running` on `/metrics`, and a page that draws no timeline because the counter registered no media. Thirty lines of worker, no reference to the VMS, and the platform runs it:

```
counter/units/a  counter/units/b  counter/workers/c-1  counter/epoch/a  counter/epoch/b
heartbeat status: [{'id': 'a', 'value': 4, 'phase': 'counting'}, {'id': 'b', 'value': 10, 'phase': 'counting'}]
vms/*: []            the two subsystems share the platform and see nothing of each other
```

Diff the two subsystems and you get a YAML file and a worker — the controller and the console are the platform's, run from that file. That is what "each new subsystem provides its controller and its worker to the platform" means as an artifact — and the VMS is no exception: `vms/vms.subsystem.yaml` is *its* controller, and `vms/controller.py` is twenty lines that call the platform's class by the VMS's names (`create_camera`, not `create`). The spec's vocabulary is deliberately small: fields, a derived row, a constraint and a tie-break **by name** from a catalogue of two (`labels-subset`, `most-free-capacity`), a snapshot list. A subsystem that needs another rule registers a function under a name — code, not YAML pretending to be code. Detectors in М11 will be `det.subsystem.yaml` with `constraint: labels-subset` against GPU labels and a `detectorworker` that runs them — the same class, the same stores, the same ACL shape.

## Step 8 — On М9's box

Everything above runs on the box М9 built, and the module should say so as units rather than as a sentence. `deploy/` is Quadlet units over one image (four here; Step 9 adds two) (`Containerfile` → `localhost/vmsserver:latest`, the same image М11's jobs start `FROM`): `vmsworker@.container` (the slot is the instance name — `systemctl start vmsworker@w-1`), `vmscontroller.container`, `vmsconsole.container`, and `vms-archive-retain.container` run by a timer — the archive resource's policy pass, `python3 -m vms retain`. М9's four units become these: `worker.container` is `vmsworker@`, `postgres.container` is gone (М10 Lesson 1 — the platform's stores on the data partition are the truth), `spool-uploader.container` is gone with it (the spool is the archive resource's staging, promoted locally), and `vms-agent.container` stays М9's business. What every unit has in common is М9 Lesson 5's rule: the image is in the rootfs slot, everything the box must not lose is under `/data` — `/data/platform` (the stores), `/data/spool`, `/data/archive`, `/data/media`, `/data/config/vms.env` — so an A/B update that boots the other slot finds the same cameras, the same assignment and the same footage.

The mounts say the same thing the ACL says, in bytes: the controller mounts no archive and no spool (it has nothing to do with footage); the console mounts the spool read-only and the archive to serve from; the worker is the only unit with the archive writable and the media read-only; the policy pass reads the platform's rows and writes the archive. `tests/test_deploy_units.py` reads the units and checks exactly that, and that each `Exec=` is an entrypoint `python3 -m vms` actually has. `deploy/check-quadlet.sh` is М9 Lesson 4's generator dry-run for these files, for the bench.

And the health check reaches the new worker. М9's `rauc-health-check` — the script that decides whether an OS update is kept — had a bottom rung, *footage is actually being written*, that read the old recorder's metrics. It now tries the VMS first: `vms_workers_live ≥ 1` from the console's `/metrics`, `vms_cameras_recording ≥ 1` when `/cameras` has an enabled camera configured, and a segment promoted into `/data/archive/vms/` within two segment lengths — read from the disk, on the box, asking nothing outside it. An update that boots perfectly, starts every unit and records nothing is rolled back by the same unit that rolled back М9's.

## Step 9 — Live video: the third subsystem

The page plays what the resource holds, one segment at a time. A person in front of a wall wants *now*, and "now" is per-frame, per-viewer work — DTLS handshakes, ICE, RTP pacing, a browser that stalls — none of which may run in the process that records. So live view is a fourth process, and the question the platform asks of any process is the same: *what is its unit, and what is its capacity counted in?* The answer is what makes it a subsystem rather than a feature: **the unit is a camera's fan-out** — *camera 7 is being watched, from gateway g-2* — and **capacity is viewers**. `vms/live.subsystem.yaml` is that description, ten lines like the counter's: rows `streams`, named by the camera (`id: cam`), a `labels` field for where the viewers are (`public-address`), a `grace` in seconds, capacity and headroom from the heartbeat, `labels-subset`, the snapshot. The controller is `SpecController` run from it (`python3 -m vms livecontroller` — no code of its own); the console for it is `SpecConsole` over the same file.

**The gateway is a worker.** `vms/gateway.py`'s `LiveGateway` extends the platform's `Worker`: a slot claimed by CAS (`g-1`), an assignment read from `live/workers/g-1`, an epoch taken per fan-out, a heartbeat with `capacity`, `headroom` (viewers it could still take — what the autoscaler moves `N` on), its `url`, and a status line per stream. Its reconcile pass makes its subscriptions equal its assignment: for each fan-out it is assigned, it finds where the camera's RTP is — the VMS worker's heartbeat now carries `live_port` per running camera (`20000 + id`, deterministic, so nobody keeps a port table) — and subscribes **once**. Fifty browsers are fifty `webrtcbin`s hung off one `tee` inside the gateway, not fifty subscriptions; `test_fifty_viewers_one_subscription_and_the_worker_unchanged` counts one, and compares the VMS worker's heartbeat object byte for byte before and after: the worker never learned a viewer exists. Its side is a second branch on the pipeline's `tee` — `queue leaky=downstream ! rtph264pay ! udpsink host=127.0.0.1` — the same bytes the archive gets, no second encode, UDP into nobody when no gateway listens, and a stalled gateway loses packets rather than pushing back on recording. That is the leaky-queue rule by construction.

**Demand creates the unit; demand deletes it.** Nobody configures a fan-out. The page's *Live* button makes a WebRTC offer and `POST`s it to the console's `/whep/<cam>` (WHEP: one HTTP round trip, offer in, answer out, `Location` for the hang-up). On the first viewer the console — whose token may write the operator's rows of the subsystems it fronts, `live/streams/*` included — creates `live/streams/7` and answers **503, retry in 2 s**, because placement is the live controller's pass and the console's token cannot place (`test_the_first_viewer_creates_the_stream_and_the_controller_places_it` makes it try). The controller places the fan-out on the gateway with the most viewer headroom that reaches the stream's labels; the gateway subscribes; the browser's retry is proxied to that gateway, whose SDP answer comes back with `Location: /whep/session/<id>?gateway=g-1`. From then on RTP flows gateway → browser and the console carries nothing. When the last viewer hangs up the gateway keeps the subscription for `grace` seconds (a returning viewer costs nothing) and then **deletes the unit itself** — a worker deleting a row, the one such crossing in the course, allowed because the row exists only for the audience — and the controller's next pass takes the placement back. A viewer who comes back recreates the unit under its name, one revision on.

**Failover is the worker's.** A gateway dies: its slot lapses, its viewers are gone with it, and the controller moves nothing on its own — a crash is left alone, as for a VMS worker — until the slot is released (the operator's `retire`, or Nomad's orderly stop); then `redistribute()` moves the fan-outs to the survivor and the page's next offer lands there. `test_a_dead_gateway_loses_its_fan_outs_to_the_survivor_and_viewers_reconnect` ends by checking the VMS worker is still recording camera 1 and never noticed.

```
POST /whep/1  (an SDP offer)     -> 503 {retry_after: 2}          live/streams/1 created; the console cannot place
                                    livecontroller: place(1) -> g-1 (most free capacity (100) among 1 worker(s); on srv-1)
                                    g-1: subscribe udp://srv-1:20001, epoch 1
POST /whep/1                     -> 201 application/sdp, Location: /whep/session/<id>?gateway=g-1
GET  /live/1                     -> {gateway: g-1, status: {phase: live, sessions: 1, port: 20001}}
× 50                             -> live_sessions 50 · live_headroom 50 · one subscription; vms/w-1/heartbeat unchanged
DELETE /whep/session/<id>?gateway=g-1  -> 200; 30 s later the gateway deletes live/streams/1; the controller unplaces it
POST /whep/1?labels=public-address     -> only a gateway on a server labelled public-address is eligible
```

What is real and what is a stand-in: signalling, placement, the fan-out arithmetic and the failover are tested here with `FakePeer`, which answers any offer with a minimal SDP. `gstvms/webrtc.py` is the media path for a box — one `udpsrc ! rtpjitterbuffer ! tee` per camera, one `queue ! webrtcbin` per viewer, WHEP answered with every ICE candidate so nothing trickles — and needs `webrtcbin`, libnice and a browser, so it is bench work like every GStreamer element in the course. Two things it does not do, stated: it does not transcode (a camera with B-frames or H.265 is *live unavailable: codec* in the status, and transcoding is a placement decision — a GPU label — not a silent default), and it has no TURN, so a viewer outside the LAN waits for М12, where the domain places a relay where a public address is and puts a token check on `/whep`.

**Deliverable:** one box, three subsystems, one console, six units. `POST /cameras` starts a recording within one worker pass; open `/` and watch the camera appear in the list, then its first promoted segment on the timeline, and play it; press *Live* and watch `live/streams/1` appear, get placed and answered; stop the controller and show recording, the read model and a worker restart all unaffected; kill the worker and show the edit made meanwhile applied on restart; and `test_second_subsystem.py` and `test_lesson5_live.py` green, with a written statement of what the platform knows about the VMS — a prefix, an assignment shape, a heartbeat shape, and nothing else.

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
| The console's PUT applies twice | The client made a new `Idempotency-Key` on retry. The key belongs to the intent. |
| The counter subsystem sees `vms/` rows | Its prefix is wrong or it is listing `/`. A subsystem lists its own prefix and nothing else; the ACL will make that a rule in М11. |

## Recap

- The controller is the only writer of `vms/*`; every write is read-modify-write by CAS with a retry.
- It refuses `worker`, `placement`, `revision`, `epoch`, `phase`: what a thing is told and what it observes are different columns.
- Placement is by capacity — the worker's number, read from its heartbeat, never the controller's — stored with a reason; adding a worker moves nothing; rebalance is explicit and budgeted.
- Two controllers agree because the row is CAS and the assignment merges — and the test found the version that did not.
- Stop the controller: nothing running stops. Kill the worker: the edit is waiting in the store when it returns.
- The controller never decides how many workers there are or where they run: the scheduler runs `N`, the autoscaler moves `N` from headroom, and the controller's one unasked move is to redistribute a *released* slot — never a lapsed one.
- The console reads heartbeats and writes through the controller; a second subsystem runs through the same platform with a different prefix.

## Exercises

1. Cache `workers_seen()` in the controller for sixty seconds "to save reads". Run the two-controller test and the failure test, then say which property broke.
2. Give the console a direct `vars.put` for the "quick fix" of a camera name. Write the sequence in which the controller and the console overwrite each other.
3. `capacity_of` reads the *last* heartbeat, however old. Make it read only live ones and construct the reschedule in which the fallback constant places forty cameras on a worker that can carry ten.
4. Add a `move` endpoint to the console and defend it against the refusal list: who may call it, and what must it record?
5. Write `Subsystem("det")` with a `GPU` resource: what does its controller place *on*, and what does the worker's affinity look like in М11's job file?
6. Give the controller a Nomad client and let it set `count` itself when `headroom()` hits zero. List what it now has to know (servers, costs, the job file, the API's failure modes) and what happens when two controllers do it at once.

## Where this is going

One box runs the platform's shape: two stores, a controller, a worker, a resource, and a second subsystem to prove the first is not special. [**М11 — ClusterVMS**](../М11_ClusterVMS/README.md) puts a scheduler under it and several servers around it: the stores become Nomad Variables and MinIO with the tests unchanged, the `systemd` units become jobs, the worker moves between servers with its cameras, the resource stays with its footage — and the controller is still not asked.
