# Lesson 5 — `vmsrecorder`: the Fourth Subsystem, and the Only One on the Archive

**Module:** ServerVMS — the platform's shape on one server (Module 10)
**You will build:** recording as a subsystem of its own — `rec.subsystem.yaml`, whose unit is one camera's *recording* and whose capacity is what a server's disks can take; the recorder, a worker that holds no camera but subscribes to the fan-out of the worker that does and writes footage into `rec/<cam>/e<epoch>/` on *its* server's archive; the operator's *Record* toggle; and the proof that when the camera's worker fails over, the recording stays where the disks are and re-subscribes.
**Time:** ~120 minutes.

## Why this lesson exists

Lesson 4 ended with a worker that *holds* a camera — one connection, one epoch, one fan-out, its events into the resource — and records nothing. That was a decision, and this lesson is its other half. Three things wanted to be true at once and could not be while the worker recorded: a worker should run wherever the camera is reachable from, which is a network question; footage should be written where the disks are, which is a storage question; and a camera that is watched but not recorded — live only, analytics only — should cost nothing on the archive. One process cannot be placed by two different constraints. So recording became what live video and detection already were: a subsystem, with its own unit, its own worker, its own controller from the same YAML machinery, and its own placement rule — `requires: resource`, `servers: distinct` — the only one in the module that must be *on the archive*.

The recorder is not a second connection to the camera. It is a subscriber, like the gateway and the detector: it reads `live_url` from the VMS worker's heartbeat and opens `rtsp://<server>:8554/<cam>`. The worker that holds the camera never learns it is being recorded, exactly as it never learns it is being watched.

> **What you can verify without hardware.** `tests/test_lesson5_recorder.py`: a recording created from the console's token and placed by the rec controller on the recorder whose server has the archive; the pipeline built from the worker's fan-out under the *recorder's* epoch; a segment promoted into `rec/<cam>/e<epoch>/` and indexed beside it while the worker's tree stays events-only; the camera's worker moved to another server and the recorder re-subscribing on the spot — same recorder, same disks, a new epoch; a recording that waits while nobody holds the camera; and the toggle turned off. `gstvms/actuator.py`'s `GstRecActuator` — `rtspsrc ! rtph264depay ! h264parse ! watchdog ! archivesink` — is the bench's.

## Prerequisites

- **Lesson 3** — promotion, the manifest, the epoch in the path: all of it is the recorder's now.
- **Lesson 4** — the worker holds the camera and publishes `live_url`; the slot, the epoch, the lease.
- **Lesson 1** — one YAML, one `SpecController`: a subsystem is a prefix, a row shape and a placement rule.

## Learning objectives

1. Say why recording is a subsystem and not a property of the camera, and what its unit and capacity are.
2. Build a worker that is fed by another subsystem's worker — through the heartbeat, never a call.
3. Place by `requires: resource` and `servers: distinct`, and read both in the placement reason.
4. Show two trees under two epochs for one camera, and say who writes and who retains each.
5. Re-subscribe when the camera's holder moves, without moving the recording.

---

## Step 1 — A recording is a unit

`vms/rec.subsystem.yaml`, the fourth spec on the box:

```yaml
name: rec
unit:
  rows: recordings                   # rec/recordings/<cam>
  id: cam                            # one recording per camera, named by it
  fields:
    cam:            {type: string, required: true}
    retention_days: {type: int,    default: 30}
    enabled:        {type: bool,   default: true}
    labels:         {type: list}
placement:
  capacity:   {from: capacity, fallback: 50}
  constraint: labels-subset
  requires:   resource               # the only subsystem that must be where the archive is
  servers:    distinct               # one recorder per server carries recordings (the console may say shared)
```

The row is the operator's: *record camera 1, keep thirty days*. It is created explicitly — the page's *Record* toggle `POST`s `/rec/recordings {cam}` and *Stop recording* deletes it — never derived from the camera row, because a camera exists without a recording and the two have different owners, different retention and different placement. `retention_days` moved here from the camera in Lesson 3; the camera row keeps `events_retention_days`, because events are the worker's and footage is the recorder's:

```
rec_ctl.policy()  -> {"servers": "distinct"}       the recorder's default: a second recorder on the same disks is no second place to record
ctl.policy()      -> {"servers": "shared"}         the worker's: several workers on one server hold different cameras
```

`REC_SPEC.requires == "resource"` is the line the whole lesson turns on: the rec controller's pool is the recorders whose server's resource answers (three states: `live`, a resource that heartbeats; `silent`, one that stopped; `unknown`, one that never did — only `silent` excludes); the VMS worker has the same rule for its events, but only the recorder writes media.

## Step 2 — The recorder is a worker fed by a worker

`vms/recorder.py`'s `RecWorker` *is* `VmsWorker` over the `rec` rows — the same reconciler, the same slot claimed by CAS (`r-1`), the same epoch per unit and lease, the same heartbeat with capacity and headroom. What differs is what a pipeline needs:

```python
def source(self, cam):                         # where the camera's stream is: the VMS heartbeat, never a call to the worker
    for hb in heartbeats(self.objects, "vms/").values():
        for st in hb.status:
            if st["id"] == cam and st["phase"] == "running" and st.get("live_url"):
                return hb.extra["server"], st["live_url"]

def enrich(self, cam):                         # what the actuator gets: a source, a spool and an archive — not a camera
    src = self.source(cam["id"])
    if src is None:  self.waiting.add(cam["id"]); return None       # cannot start now; back off, say `waiting`
    return dict(cam, source=src[1], source_server=src[0], spool=self.archive.spool, archive=self.archive.root)
```

Run it:

```
rec_con.create({"cam": "1", "retention_days": 7})       rec/recordings/1 — the console's token; rec_con.place("1") -> Forbidden
rec_ctl.ensure_placed()   -> r-1, "… on srv-1, whose resource is unknown"      the rec controller's pass
r.reconcile_once()        -> [('start', 1)]
   started: source rtsp://srv-1:8554/1 from srv-1, epoch 1 (rec/epoch/1), spool, archive
   w.epochs == {"1": 1}                                    two epochs, two writers, one camera
heartbeat status: {phase: running, cam: "1", source: "rtsp://srv-1:8554/1", epoch: 1};  rec_recordings_running 1
```

The recorder's epoch is `rec/epoch/1`, taken by CAS like any writer's; the worker holds `vms/epoch/1`. They are different numbers about different things — who may write *this camera's events* and who may write *this camera's footage* — and the second one is what `archivesink` puts in the path.

## Step 3 — Two trees

A closed segment in the spool is promoted on the recorder's pass — the order of Lesson 3, Step 2, run by the recorder now — and lands under `rec/`:

```
<archive>/rec/<cam>/e<epoch>/<start>Z.mp4        promoted media — the RECORDER's epoch
<archive>/rec/<cam>/manifest.jsonl               one line per media segment; media only
<archive>/vms/<cam>/e<epoch>/<start>Z.events.jsonl   the camera's event buckets — the WORKER's epoch
```

```
r.pump_once(); r.promoted == 1;  Manifest(archive, 1).read()[0].path == "rec/1/e1/20260912T…Z.mp4"
w.observe(1, "motion");  subsystems_under(archive) == {"rec": ["1"], "vms": ["1"]}
```

The manifest indexes media and nothing else. Events are not lines in it any more: the resource's event database (Lesson 10) indexes the buckets, and the console draws them over the media spans. Retention follows ownership: `ArchivePolicy` — the pass the *recorder* registers on the resource process, as the `rec` hook — repairs the manifests and retains media by `rec/recordings/<cam>.retention_days`; the platform's own pass retains buckets by `vms/retention/<cam>`, which the VMS controller derives from `events_retention_days`. A camera recorded for a week and watched for a year is one row in each subsystem, and nobody's pass touches the other's files.

## Step 4 — The camera moves; the recording does not

The controller moves camera 1 to `w-2` on `srv-2` (a failover, a rebalance — it does not matter). The worker's heartbeat now says `live_url: rtsp://srv-2:8554/1`. The recorder notices before its reconcile pass:

```python
def resubscribe(self):                         # the camera's holder moved: the pipeline on the old URL is stopped and counted lost
    for cid in list(self.reconciler.actual):
        src = self.source(cid)
        if src is not None and self.sources.get(cid) not in (None, src[1]):
            self.actuator("stop", {"id": cid}); self.reconciler.lost(cid, self.now()); moved.append(cid)
```

```
ctl.move(1, "w-2"); w2.reconcile_once(); w.reconcile_once()          w-2 holds it under vms epoch 2; w-1 stopped it
r.resubscribe()      -> [1]     ("stop", 1)
r.reconcile_once()   -> [('start', 1)]   source rtsp://srv-2:8554/1, epoch 2         a new pipeline is a new writer: e2
rec_ctl.where("1")   -> "r-1"                                                        the recording did not move; its source did
```

Footage before the move is in `rec/1/e1/`, after it in `rec/1/e2/`, both on `srv-1`'s disks, both in one manifest with the earlier epoch marked *fenced* on the timeline. Nothing crossed the network except the stream itself — which is what the fan-out is for: the loopback `udpsink` of the first draft could feed one subscriber on one box; an RTSP server on the worker's port can feed a recorder, a gateway and a detector on three servers, and TCP interleaving keeps it inside one connection through any firewall. Multicast would have been cheaper on the wire and unroutable in practice.

When it is the *recorder's* server that dies, the rule is Lesson 6's, under `servers: distinct`: its slot lapsed and stayed lapsed, and its resource silent — two silences from one server — and the rec controller moves the recording to a recorder whose resource answers. The footage on the dead disks stays there, unavailable by name until the server returns. М11 Lesson 4 pulls the power on both subsystems at once and reads what each does.

## Step 5 — Waiting, and the toggle

A recording of a camera nobody holds yet:

```
rec_con.create({"cam": "2"}); rec_ctl.ensure_placed()
r.reconcile_once()   -> [('failed', 2)]      no fan-out to subscribe to; backoff with jitter, as for any failed start
heartbeat status:       {phase: waiting, why: "camera held by nobody", source: None}
ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()         the worker takes the camera
r.reconcile_once()   -> [('start', 2)]      source rtsp://srv-1:8554/2
```

And a camera with no recording row is watched, not recorded: held by a worker (live, detection, events under `vms/2/`), and with no `rec/2/` tree at all — `subsystems_under(archive) == {"vms": ["1"]}` after one event on camera 1 and none on 2. *Stop recording* deletes the row; the rec controller's next pass takes the placement back (`unplace_deleted`), the recorder's next pass stops the pipeline, and the footage stays until its retention runs out:

```
rec_con.delete("2"); rec_ctl.unplace_deleted()
rec_ctl.assignment("r-1").units == [];  r.reconcile_once() -> [('stop', 2)]
```

**Deliverable:** `vmsrecorder@r-1` beside `vmsworker@w-1` on the box; *Record* pressed on the page and a segment in `rec/1/e1/` ten minutes later; the worker killed and restarted, the recorder's log saying *re-subscribing* and a new `e2` directory; *Stop recording* and the pipeline gone within one pass; `test_lesson5_recorder.py` green.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Every recording is `waiting: camera held by nobody` | No VMS worker has the camera in phase `running` with a `live_url`. The recorder reads the VMS heartbeat; check `vms/w-*/heartbeat`, not the recorder. |
| The recording was placed on a recorder whose server has no archive | `requires: resource` only excludes a resource that is *silent*; `unknown` (no heartbeat yet) is allowed so a box works before `vmsresource` starts. Start the resource process; the reason says `whose resource is live`. |
| Two recorders on one server, one of them idle | `servers: distinct`, the default. Set `rec/policy {servers: shared}` from the console if that server's disks are really two places. |
| The worker moved and the recorder keeps writing `e1` | `resubscribe()` runs before every reconcile pass and compares `live_url`; a worker from before Lesson 4's fan-out has none. |
| Footage vanished after `Stop recording` | Not from the toggle: `rec/<cam>/` is retained by `ArchivePolicy` for `retention_days` after the row is gone (30 by default without a row). Check the resource's policy pass. |
| `rtspsrc` connects and nothing arrives | The fan-out is TCP-interleaved (`protocols=tcp`); a UDP-only client behind NAT sees a connection and no packets. |

## Recap

- Recording is a subsystem: unit *one camera's recording*, capacity *what these disks take*, prefix `rec/`.
- The recorder holds no camera; it subscribes to the worker's fan-out found in the heartbeat — never a second connection, never a call.
- `requires: resource` and `servers: distinct` are the recorder's rules; the reason names both.
- Two trees, two epochs, two owners: `vms/<cam>/` events by the worker, `rec/<cam>/` media by the recorder; the manifest is media only; each retained by its own row.
- The camera's holder may move; the recording stays with the disks and re-subscribes under a new epoch.
- No row, no footage: a camera is watched for free and recorded on purpose.

## Exercises

1. Make the recorder derive its rows from the camera rows instead of `rec/recordings`. List what the operator can no longer say, and what the rec controller can no longer place by.
2. Set `rec/policy {servers: shared}` on a box with two recorders and one archive. Write a segment from each and read the manifest. Say what *distinct* was protecting.
3. A camera's worker fails over every minute. Count the `e<epoch>` directories after an hour and say what the timeline shows; then say why re-subscribing under the *same* epoch would be wrong.
4. Record on motion: the recorder starts its pipeline on a `motion` event and stops it after a quiet minute. Which subsystem owns that rule — rec, vms, or a third — and where does the event come from?

## Where this is going

Four subsystems' workers are running with nobody telling them what to hold. [**Lesson 6**](06-vmscontroller.md) builds the controller — the only writer of placement, from a YAML, safe at two — that places cameras on workers and recordings on recorders by the same pass with different rules, and [**Lesson 7**](07-the-console.md) gives the operator the toggle.
