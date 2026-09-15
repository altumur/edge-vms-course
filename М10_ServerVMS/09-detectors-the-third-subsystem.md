# Lesson 9 — Detectors: the Third Subsystem

**Module:** ServerVMS — the platform's shape on one server (Module 10)
**You will build:** detectors as a subsystem — `det.subsystem.yaml`, whose unit is one model on one camera and whose capacity is streams a GPU can carry; the detector worker, running a model against the camera's fan-out and writing its events into its own buckets on the resource under its own epoch; the *Detectors* panel on the camera's page, through the console's mount.
**Time:** ~90 minutes.

## Why this lesson exists

The subsystems so far consume: the worker holds, the recorder keeps, the gateway shows. A detector *produces* — events, the thing Lesson 3 put beside the footage as buckets and the design record refused to make a subsystem of. This lesson is the case that settles why: the writer of camera 7's line-crossing events is the process that holds `det/epoch/7-linecross`, fenced like any writer, on its own prefix beside the VMS's; events are the shape it writes in, not a process of their own. It is also where the question *does a detector get its own console?* is answered — with a mount, not a screen.

> **What you can verify without hardware.** `tests/test_lesson9_det.py`: a model placed on the GPU worker only, running against the camera's fan-out and writing two events under its epoch; disabled, re-enabled under the same epoch, deleted with its buckets left; a camera nobody holds leaving the model *waiting*; an unknown kind *unsupported*; the unplaceable model placed when a GPU arrives. The model itself (`FakeModel`) fires on a schedule; a decode-and-infer pipeline is bench work.

## Prerequisites

- **Lesson 3** — event buckets on the resource, `EventLog`, retention by `<sub>/retention/*`.
- **Lesson 6** — `SpecController`, `labels-subset`, `/unplaceable`.
- **Lesson 7** — the console's mount.
- **Lesson 8** — a worker that finds a camera's RTP from the heartbeat.

## Learning objectives

1. Write a subsystem whose unit is a pair (a model on a camera) named by the operator.
2. Build a worker that writes events under the epoch it holds, and show them on the same resource under their own prefix.
3. Place by a label the worker's server carries, and say what happens when nothing carries it.
4. Add the operator's actions on a second subsystem to the camera's page through the console's mount.

---

## Step 1 — A model on a camera, placed where a GPU is

The third subsystem answers the question the other two leave open: what does a subsystem that *produces events* look like, and who gives it a screen? `vms/det.subsystem.yaml`: a unit is **one model on one camera**, named by the operator for the pair (`7-linecross`), with `cam`, `kind`, `params` (opaque to the platform), `enabled`, and `labels` defaulting to `gpu`; capacity is **streams** a GPU can decode and infer on; `labels-subset` decides where it may run. `vms/detector.py`'s `DetWorker` extends the platform's `Worker` like the gateway does: it finds the camera's RTP from the VMS worker's heartbeat (never by calling it), runs a `Model` (`FakeModel` here — a decode-and-infer pipeline on a box), and writes what the model saw into **its own bucket on the resource**, `det/<unit>/e<epoch>/…events.jsonl`, under the epoch it took for the unit. That last part is the whole reason detectors are a subsystem and events are not (the design record's *What is a subsystem* row): the writer of camera 7's `linecross` events is the process that holds `det/epoch/7-linecross`, fenced like any writer, and the events sit beside the VMS's under their own prefix, retained by `det/retention/*`, mirrored by the resource, merged onto the camera's timeline by the index. A camera that stops recording leaves the model *waiting*, not failed, and the placement untouched; an unknown `kind` is *unsupported* and runs nothing; a unit nothing with a GPU can reach is on `/det/unplaceable` with its labels, and is placed the moment a GPU worker heartbeats — nothing else moves.

## Step 2 — The console fronts it; the page shows it

Lesson 7, Step 5 built the mount for exactly this. `python3 -m vms console` mounts `det` beside `live`: `/det/spec`, `/det/units` with the read model from the detector workers' heartbeats, `/det/where/<name>`, `/det/unplaceable`, `/det/metrics` — `det_worker_headroom` being what the autoscaler moves `N` on. The console's token carries `det`'s operator rows, so the camera's page can add a model to this camera (`POST /det/units {name: "7-linecross", cam, kind, params}`), list this camera's models with the worker's word on each — *running · 2 events · d-2*, *waiting · camera not recording*, *unsupported* — and enable, disable or delete them through `/det/units/<name>`. Nobody opens a detector console; the person's world is cameras. And the events arrive where the person looks: the resource's event database (Lesson 10) tails `det/7-linecross/e1/…` like any bucket, so a line crossed shows as an amber tick on the camera's timeline — `linecross (det)` — beside the worker's `silent` and the operator's `mark`, and fenced the moment another detector instance takes the unit's epoch.

```
POST /det/units {name: 1-linecross, cam: 1, kind: linecross}   -> 201 {labels: [gpu], worker: null}
POST /det/units {…, worker: d-2}                                -> 400            placement is not the operator's
detcontroller: place(1-linecross) -> d-2 (most free capacity (8) among 1 worker(s) reaching gpu; on srv-1)   d-1 has no gpu
d-2: epoch 1, subscribe udp://srv-1:20001, run linecross         det/1-linecross/e1/<bucket>.events.jsonl: {kind: linecross, pass: 3, cam: 1}
GET  /det/units       -> rows: [{id: 1-linecross, phase: running, events: 2, worker: d-2, port: 20001}]
GET  /det/unplaceable -> [{id: 1-lpr, labels: [gpu], workers_live: 1}]        nothing with a GPU is live
PUT  /det/units/1-linecross {enabled: false}  -> the model stops; the row says pending; the epoch is kept
```

**Deliverable:** a line-crossing model added to the camera from the page, placed on the worker with a GPU label, its events under `det/1-linecross/e1/` on the resource beside the VMS's and on the camera's timeline within one tail; a second worker without the label never chosen; the model waiting, not failed, while the camera is down; `test_lesson9_det.py` green.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| The model is placed on a worker with no GPU | Its `labels` are empty — the default is `["gpu"]`, but a `POST` with `labels: []` means *anywhere*. The reason line says which labels were reached. |
| Events under `det/…/e2/` while `e1` is still being written | Two workers hold the unit — a reassignment window. The index fences the older epoch; the newer is current. |
| `phase: waiting` with the camera held | The VMS heartbeat is stale or has no `live_url` (a worker from before Lesson 4's fan-out). The detector reads the heartbeat, never the worker. |
| Deleting the model deletes nothing on disk | Correct. Buckets are retained by `det/retention/<unit>` on the resource; the row's deletion takes the placement back and stops the model. |

## Recap

- A detector is a subsystem: a movable worker, a placed unit (a model on a camera), a capacity in streams.
- Its events are its own buckets on the resource under its own epoch — events are the shape, the detector is the process.
- Placement by label: nothing with a GPU means *unplaceable, and here is why*; a GPU arriving places it and moves nothing else.
- No console of its own: the VMS console mounts it, and the operator acts from the camera's page.

## Exercises

1. Two models on one camera, on two workers. How many subscriptions to the camera's fan-out, and what does the worker holding it see?
2. Give `det` a `params` schema per kind instead of an opaque string. Where does the platform stop and the subsystem start?
3. A model that should run on *every* camera (motion). Who creates the units — the operator, the console, or a controller pass? Defend the answer with the *demand-created* rule from Lesson 8.
4. Write `det`'s retention row. Which process deletes an old bucket, and which one deletes the manifest line if there were one?

## Where this is going

Four subsystems on one box, through one platform, one console, and ten units. [**Lesson 10**](10-events-the-database-that-is-a-cache.md) gives the resource its process and the events their database — the thing that answers `/events`, and why it is not a subsystem; [**Lesson 11**](11-on-the-box.md) puts all of it on the box М9 built — Quadlet units over one image, everything that must survive an OS update under `/data`, and the health check that rolls an update back when footage stops.
