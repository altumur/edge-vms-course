# Lesson 3 — `archivesink`, and the Archive as a Resource

**Module:** ServerVMS — the platform's shape on one server (Module 10)
**You will build:** a sink element that writes segments into the spool and promotes them into an archive resource with the epoch in every path and a manifest beside the footage — then kill it mid-segment, rebuild the manifest from the files, and apply retention as a policy.
**Time:** ~150 minutes.

## Why this lesson exists

М8 sent footage to Kinesis; М9 gave the box a spool and taught the rule that made the spool safe — *a segment is deleted on acknowledgement, never on send*. The archive in this module is that rule with the acknowledgement pointed at ourselves. KVS is gone. The index table from М9 Lesson 5 is gone too, and not because it was wrong: it was on the wrong side of a boundary. An index that lives in a database on the same box as the footage is an index that does not travel with the footage, has to be rebuilt by scanning, and needs a database. An index that lives *beside* the footage — a manifest per camera on the same disks — returns with the disks, needs no scan, and needs no database.

That is what makes the archive a **resource**: server-bound, with no controller, and with a policy. The worker writes into it; nothing moves it; when its server dies its footage is unavailable and everything else carries on.

> **What you can verify without hardware.** Everything except the element: `tests/test_lesson3_archive.py` runs the acknowledgement order, the kill-at-minute-seven accounting, the manifest rebuilt from files alone, the fenced epoch on a timeline, two resources merged, and retention — against real files in a temp directory, no GStreamer. `gstvms/archivesink.py` wraps `splitmuxsink` and calls the same `promote()`; it needs М9's bench.

## Prerequisites

- **М9 Lesson 4** — the spool, and *delete on acknowledgement, never on send*.
- **М9 Lesson 5** — the `segments` table and the timeline query this lesson replaces.
- **М9 Lesson 8** — *on restart, never resume the previous segment; open a new one.*
- **М11 Lesson 4** (read ahead if you like) — the epoch in the path. This lesson puts it there.

## Learning objectives

1. State the promotion order and say what each step protects.
2. Account for a kill mid-segment: what is promoted, what is picked up on restart, what is lost.
3. Replace the index table with a manifest and rebuild it from the files alone.
4. Mark a fenced epoch on a timeline and merge two resources' manifests.
5. Apply retention as a policy on the resource, in the order that survives a crash.
6. Say what a resource is, and why it has no controller.
7. Say what an event is, where it lives, and who writes it.

---

## Step 1 — The paths

```
<spool>/vms/<cam>/e<epoch>/<start>Z.mp4        the open segment, and closed ones not yet promoted
<archive>/vms/<cam>/e<epoch>/<start>Z.mp4      promoted: the resource's media
<archive>/vms/<cam>/e<epoch>/<start>Z.events.jsonl   the camera's event buckets (Step 5a)
<archive>/vms/<cam>/manifest.jsonl             one line per media segment and per closed event bucket
```

The first path element is the subsystem. The archive is one resource and other subsystems keep their buckets on it under their own prefix — `det/7-linecross/…`, `live/7/…` — which is why the VMS's own footage lives under `vms/` rather than at the root.

The epoch is in every path from the first byte. `archivesink` gets it as a property from the worker when the camera starts (Lesson 4), and `splitmuxsink`'s `format-location` puts it in the directory name. Nothing downstream — promotion, the manifest, playback — has to be told about fencing; it reads the path.

```
parse("/a/7/e5/20260912T101000Z.mp4") -> (camera 7, epoch 5, 2026-09-12 10:10:00 UTC)
parse("/a/7/e5/manifest.jsonl")       -> None
```

## Step 2 — The acknowledgement order

`ArchiveResource.promote(spool_path)` is what the element calls on `splitmuxsink-fragment-closed`, and the order inside it is the lesson:

```
1. rename the closed segment into the archive      — atomic on one filesystem; on another, copy then appear whole
2. append its line to the manifest                  — {cam, epoch, start, end, path, bytes}
3. the spool copy is gone                           — a consequence of 1 on one filesystem; an explicit delete otherwise, last
```

The test asserts all three in that order: the spool path no longer exists, the archive path exists, the manifest has one line with the right epoch, a 600-second span and the byte count. A crash between 1 and 2 leaves a file no line names — `repair()` adds the line. A crash before 1 leaves a closed segment in the spool — the restart promotes it. There is no order of these steps in which footage that was written disappears.

## Step 3 — Kill it at minute seven

Ten-minute segments, a worker killed at minute seven of the eighth segment:

| | Where | What happens |
|---|---|---|
| six closed segments | promoted, in the archive, in the manifest | nothing; they were acknowledged |
| one closed segment | in the spool — closed, and the worker died before `promote()` | `closed_in_spool(grace=30 s)` finds it; the restarting worker promotes it first |
| the open segment | in the spool, being written | **lost** — up to one segment length, the number М9 Lesson 4 stated and М9 Lesson 8 repeated: *never resume it; open a new one* |

`test_kill_mid_segment_open_lost_closed_kept` builds exactly that spool and asserts the counts: seven lines after the restart's promotion, and `closed_in_spool` empty afterwards. The grace — two segment lengths in М11's sweep, thirty seconds here because the test controls the clock — is what tells a closed segment from an open one without asking the element: a file nobody has written to for that long is not being written.

## Step 4 — The manifest is the index

М9 Lesson 5's `segments` table had `camera_id, span, path, bytes, epoch` and a GiST index for the timeline query. `Manifest` has the same columns as one JSON line per segment, append-only, per camera, beside the footage:

```python
Manifest(archive, 7).append(seg)          # on promotion
Manifest(archive, 7).read()               # the rows
Manifest(archive, 7).timeline(t0, t1, current_epoch=4)
```

Two things the table could not do. **It returns with the disks**: when Server A comes back in М11, its manifests come back with its footage, and nothing is rebuilt by scanning. **It is rebuildable from the files alone** when it has to be — `repair()` walks the archive, adds a line for every segment no line names (epoch from the path, span from the name and the mtime), drops every line whose file is gone, and is idempotent:

```
delete the manifest; repair() -> {'added': 3, 'dropped': 0}     the same rows back, from the paths
delete one file;     repair() -> {'added': 0, 'dropped': 1}
                     repair() -> {'added': 0, 'dropped': 0}
```

That is М11's re-index sweep, renamed to what it now is: not a rebuild of an index that did not travel, but a repair of one that did.

## Step 5 — The fenced epoch, and two resources

The timeline marks what the epoch in the path makes visible. Camera 7 recorded under epoch 3, was taken over by a replacement under epoch 4, and the old instance kept writing for five minutes before its lease ran out:

```
timeline(10:05 → 10:30, current_epoch=4):
  10:00–10:10  e3  fenced       the last segment before the takeover
  10:10–10:15  e3  fenced       the zombie's — kept, marked, never deleted (М11 Lesson 4)
  10:10–10:20  e4               the live instance's
```

Nothing was deleted and nothing was corrupted: two writers, two epochs, two directories, one manifest that names both, sorted by start and then by epoch so the fenced one comes first. In М11 the same camera's footage may span two *resources* — Server A's until it died, Server B's after — and the console merges two manifests:

```
merged: e3, e3, e4  (resource A)  +  e5  (resource B)
```

## Step 5a — Events: buckets on the resource, recording or not

Where do events go, now that there is no database on the server? Ask what an event *is*: an observation — a detector fired, the camera went silent, an operator marked a moment — made by the worker that holds the camera, keyed by camera and time, never updated. That is the manifest's shape, not a table's. The first draft of this step put events *beside the open segment*, and the question that broke it was: what if the camera is not being recorded? A live-only camera, an analytics-only camera, recording on motion — and the event that says *silent* has, by definition, no segment open.

So the archive's unit is corrected: it is a **time span under an epoch**, not a media file. A span may hold media (`archivesink` wrote it), events (the worker observed something), or both. The events half is a platform piece, `psimplatform/events.py`, because nothing in it is about video:

```
<resource>/<subsystem>/<unit>/e<epoch>/<start>Z.events.jsonl     a bucket: JSON lines {t, kind, ...} for `bucket_seconds` from <start>
its writer:  the worker holding that unit's epoch — one writer per file, by construction
its fence:   the epoch in the path — a stale instance writes into its own bucket, marked afterwards
```

The worker's call is `observe(cid, kind, **fields)`, and it is valid whenever the worker *holds the camera's epoch* — which it takes when it takes the camera, before the pipeline starts, so an events-only camera holds an epoch and a lease like any other. Recording is not the condition; holding is. A camera assigned to somebody else is not mine to observe. And the lost pipeline writes its own event: `pump_once()` calls `observe(cid, "silent")` for every camera the bus reported dead.

```
log = event_log(archive, 7, 3)                        # camera 7, epoch 3: the worker's log for it
log.append(t0+12.5, "motion", zone="gate")            # not recording; still an event, in vms/7/e3/<t0>Z.events.jsonl
log.append(t0+40,   "silent")                         # the event with no segment, by definition
log.append(t0+700,  "person", score=0.9)              # the next bucket: rolled by the clock, not by anything the VMS did
res.close_buckets(now=t0+1300)  -> [2 events, 1 event]     spans over and quiet: the manifest line is written
timeline(t0, t0+1200, current_epoch=4)  -> (media None, events 2, fenced) (media None, events 1, fenced)   watched, not recorded
```

Then everything the segment already has, the buckets get: written in place and durable per line (closing a bucket is the *index* catching up, not an acknowledgement); the timeline counts a bucket's events onto a recorded span that overlaps it and shows an events-only span as what it is; `repair()` rebuilds both kinds of line from the files. Retention is per kind and per owner: media is the VMS's (`retain()` by `retention_days`), buckets are the **platform's** — the controller writes `vms/retention/<cam> {days: events_retention_days}` and the platform's resource job deletes the files, a month of footage and a year of events being the usual pair, which is why the camera row grew the second field; `repair()` then drops the lines whose files went. `test_events_are_buckets_on_the_resource_recording_or_not` is that list as assertions, and its last one is the point: no controller wrote an event, and nothing went to the store.

**Other subsystems record events the same way**, because the piece is generic: a detector's unit is a detector job with its own epoch and its own worker on a GPU server, and its buckets are `det/d-12/e3/…` on *that* server's resource. Its event about camera 7 carries `cam: 7` as a field — it does not write into camera 7's bucket, which has exactly one writer, possibly on another server. The detector subsystem (Lesson 8) writes `det/7-linecross/e1/…` through the same `EventLog`. And the resource itself is a platform job — `psimplatform/resource.py`: it heartbeats what is on its disks, serves every subsystem's buckets over HTTP, retains them by each subsystem's own rows, copies closed buckets to the next resource when `platform/mirror` is on and pulls them home after a disk is replaced, and runs whatever pass a subsystem registers on it. The VMS registers `ArchivePolicy` (repair, close, media retention). `test_the_resource_is_a_platform_job_that_mirrors_any_subsystems_buckets` runs two resources on one box with buckets from subsystems that do not exist, and the word *camera* does not appear in the file. Cross-camera and cross-subsystem search need an index; М11 builds it as a job that is a **cache**, reading every resource's buckets into SQLite and proving it is a cache by being deleted and rebuilt to the same answer. The rule, written down now while it is small: **events are observations, written by the worker holding the unit's epoch, into the unit's bucket on its server's resource; an index over them is a cache; a controller writes none of them.**

## Step 6 — Retention is a policy on the resource

There is no controller for the archive, and there should not be: the only decisions about it are a policy — keep N days per camera — and a repair. `retain(cam, days, now)` deletes the file first and rewrites the manifest second, so a crash between the two leaves a line that names nothing (which `repair()` drops) rather than a file nothing names (which would be footage the timeline cannot find):

```
three segments on 1, 10 and 19 October; retain(days=8, now=20 October) -> 2 removed; usage 1000 bytes
```

`deploy/vms-archive-retain.timer` runs `repair()` and then `retain()` per camera every ten minutes, reading each camera's `retention_days` from the config store — the policy is the operator's, the enforcement is the resource's own, and the worker is not involved. The disk-full policies from М9 Lesson 8 (`stop_recording`, `degrade_retention`, `by_priority`) become the same policy with a high-water input — and in М11, a bucket quota.

**Deliverable:** record a file-camera for ten minutes with `archivesink`; kill the worker at minute seven; show six promoted, one closed-but-unpromoted picked up on restart, the open one lost; delete the manifest and rebuild it from the archive alone; then show a timeline with a fenced epoch on it.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `promote()` raises *not a segment path* | The element's `format-location` did not produce `<cam>/e<epoch>/<start>Z.mp4` — the `epoch` property was not set before the first fragment. The worker sets it (Lesson 4). |
| Segments appear in the archive without manifest lines | A crash between rename and append. `repair()` is for exactly this; the timer runs it. |
| The manifest has lines for files that are gone | Retention crashed between file and line, or somebody deleted files by hand. `repair()` drops them. |
| `closed_in_spool` promotes the open segment | The grace is shorter than a segment's write interval. Two segment lengths, as М11 chose; thirty seconds only in tests with a fake clock. |
| Playback finds a gap where the timeline shows a fenced segment | The player filtered `fenced: true` out. Fenced footage is real footage; show it, marked. |
| Promotion across filesystems is slow | The spool and the archive are on different disks: a copy, not a rename. Put the spool on the archive's filesystem, or accept the copy — it is the acknowledgement, and it is allowed to take time. |

## Recap

- The archive is a **resource**: server-bound, no controller, a policy and a repair.
- Promotion is М9 Lesson 4's rule pointed at ourselves: into the archive, then the line, then the spool copy goes.
- A kill loses the open segment and nothing else; a closed segment in the spool is picked up on restart.
- The manifest is the index, beside the footage: it returns with the disks and is rebuildable from them.
- The epoch is in every path; the timeline marks fenced footage and never deletes it; two resources merge.
- Retention deletes the file first and the line second, on a timer, from the operator's per-camera policy.
- Events are buckets on the resource, written by the worker holding the unit's epoch — recording or not, `silent` included — under the subsystem's prefix; counted onto media, fenced, rebuilt, retained by their own days; a platform piece any subsystem uses; indexed by a cache, never by a controller.

## Exercises

1. Reverse the promotion order — line first, then rename — and construct the crash that loses footage.
2. Set the grace to one second and run a real `archivesink` for five minutes. Count how many open segments the restart "promoted".
3. Write the manifest as one file per epoch instead of one per camera. What does `repair()` gain and what does `timeline()` lose?
4. Two workers promote into one camera's manifest at once (the reassignment window of Lesson 4). Show the interleaving that corrupts a line, then fix it with an append lock or per-epoch files, and say which М11 needs.
5. Implement `by_priority` from М9 Lesson 8 as a policy on the resource: which cameras lose days first when the disk is at 95 %, and why the worker must not know.

## Where this is going

There is a source and a sink and nothing running them. [**Lesson 4**](04-vmsworker-driverpack-as-the-worker.md) builds the worker — DriverPack itself — with М9's loop inside it, an epoch per camera taken by CAS, a lease, a heartbeat, and the property everything else in the course depends on: it restarts without asking anyone.
