# Lesson 3 — What Stays on the Server, and What Does Not

**Module:** ClusterVMS — workers that outlive their server (Module 11)
**You will build:** the resource job's heartbeat and served manifests, a timeline that spans two servers and names the one it cannot reach, and the proof that an edit made *during* a failover is simply there when the worker returns — because the RPO inside a cluster is zero.
**Time:** ~150 minutes.

## Why this lesson exists

The first ClusterVMS design spent a whole lesson on making a recorder's state outlive its server: publish the configuration as an object, point a Variable at it, restore in six steps, measure the RPO. Under *workers, resources, one controller* that lesson dissolves, and it is worth spending an hour on *why*, because the reason is the module's central claim: **nothing has to travel.** The controller wrote the camera rows and the assignment into raft *before* the failure; raft is on every server; the replacement worker reads the same rows. Configuration did not survive the server — it was never on the server.

What *was* on the server is footage, and this lesson is honest about it. Footage stays on the resource, the resource stays with its disks, and while the server is down that footage is **unavailable** — a state with a server's name in it — and not lost. The manifest beside it returns when the disks do, and nothing is rebuilt.

> **What you can verify without hardware.** `tests/test_lesson3_resources.py`: the edit during the failover, the timeline across two resources with one silent, the resource's policy running with neither worker nor controller, and a worker with no assignment inventing nothing; `tests/test_lesson3_events.py`: events held by each resource's database over its own tree and merged by a console that holds none, a detector's event found by its `cam` field, a resource's database rebuilt to the same answer, a silent resource named, the resource's policy per subsystem, the mirror knob keeping the merge complete. The power pull itself is Lesson 4's and needs the bench.

## Prerequisites

- **М10 Lesson 3** — the archive as a resource: promote, manifest, repair, retain; the epoch in every path.
- **М10 Lesson 5** — the controller acknowledges after the CAS commit.
- **Lesson 2** — the resource as a `system` job; the worker's constraint to servers with `meta.archive`.
- **М9 Lesson 4** — the open segment is lost; never resume it.

## Learning objectives

1. For each thing on a dead server, say whether it travels, stays, or is lost — and why.
2. Show that an edit made during a failover needs no publication to be present afterwards.
3. Explain why the archive is not a volume that follows the worker, from the tracker rather than on trust.
4. State the storage knob and its arithmetic.
5. Give a resource a heartbeat and serve its manifests; merge a timeline across two resources and name the unreachable one.
6. Say what the acknowledgement problem became, and where the RPO went.
7. Say where events live, what the event index is, and why an event controller over a two-server database is the wrong shape.

---

## Step 1 — The table

Worker `w-1` runs on Server A. Server A dies. Nomad places `w-1`'s replacement on Server B. What was on A?

| On Server A | Under the recorder model | Under 2c | The test |
|---|---|---|---|
| **Configuration** — cameras, assignment, placement | in the recorder's Postgres; published upward; restored with an RPO | **in raft already**; `w-1` reads it on B. Nothing travels | `test_an_edit_during_the_failover_is_simply_there` |
| **Footage** | on the dead disk | **on the dead resource**: unavailable until A returns; `w-1` records into B's resource from its first segment | `test_a_timeline_spans_two_resources…` |
| **The index** | in the dead Postgres; rebuilt by scanning | **the manifest, beside the footage**: returns with the disks; the timeline merges two | same |
| **The open segment** | lost, up to one segment length | lost, up to one segment length — the number is the segment length | М10 Lesson 3 |
| **Identity, the epoch, the slot** | in Variables | in Variables — unchanged | Lesson 2 |

So *what must travel* is **nothing**, and *what is lost* is the open segment and access to old footage until the server returns.

## Step 2 — The edit during the failover

```python
ctl.create_camera({"name": "before"}); w-1 on srv-a records it
srv-a dies; 20 s pass
ctl.update_camera(1, {"name": "edited during the failover"})   # acknowledged after the CAS commit: it is in raft
b = worker(index 1, srv-b)                                       # the replacement
b.reconcile_once() -> [('start', 1)];  b.rows[0]["name"] == "edited during the failover"
objects.get("vms/config") is None                                # nothing was published for this to work
```

Read the last line twice. The first design had `publish.py`, `rehydrate.py`, a six-step restore and a measured RPO of a few seconds. All of it existed because the recorder's truth was on the recorder. Move the truth into the cluster's raft with one writer, and the replacement reads it the way the original did — the same call, the same rows, one index further on. There is no *saved · not yet replicated* inside a cluster: the write is in raft or it was refused.

## Step 3 — Why the resource is not a volume that follows the worker

The tempting shortcut: a CSI volume attached to whichever server runs `w-1`, so failover carries the disk. [Nomad issue #12118](https://github.com/hashicorp/nomad/issues/12118), still open, is why not: when a client holding a CSI volume dies, the volume stays attached to it, the rescheduled allocation fails to place, and the documented workaround is a person at the storage provider's console. Shared storage buys fencing and loses the unattended recovery the cluster was adopted for. A resource that *stays* has no such problem, because nothing tries to move it.

### The storage knob

| Archive resource | Survives | Costs | Fits |
|---|---|---|---|
| **per server, unreplicated** — a directory per box, the platform's `resource` job pinned to it | a dead server's footage is unavailable until it returns; every other camera records on | nothing new: locality kept; a manifest that may span resources | **the default**, every cluster size |
| **events mirrored to a peer** — `platform/mirror {enabled: true, copies: 1}`: each resource copies its *closed* buckets to the next live resource after it, under `.mirror/<its name>/` on the peer's disks (Step 5a) | a dead server's events are still searchable from its peer; its footage is not; a server back with an empty disk pulls its buckets home | kilobytes per unit per span, once; an RPO of one bucket plus the policy interval, on observations; lose two servers and one server's events are gone — the cluster's own threshold | a customer who searches events across the site more than they play footage |
| **erasure-coded pool across servers** | a dead server's footage is still readable | every recorded byte crosses the LAN — 200 cameras × 4 Mbit/s = 800 Mbit/s east-west plus parity, so 10 GbE between tiers; ≥ 4 drives across servers; **loss of write quorum stops every camera at once** | a customer who buys durability over locality and has the network |

The second row's last cost is the one to say out loud: the unreplicated form has no failure that stops *every* camera. The pool does.

## Step 4 — The resource says it exists

On one box the console read the manifest from the disk it shared with the worker. On a cluster the manifest is on *a* server, and the console is on another. So the resource job does two things a directory could not:

```
platform/resources/srv-a/heartbeat   {server, ts, url: http://10.0.0.11:8090, usage, units: {vms: ["7"], det: ["d-12"]}, mirrors: {}}
GET http://10.0.0.11:8090/buckets/vms/7       any subsystem's closed buckets — the platform's
GET http://10.0.0.11:8090/manifest/7          the manifest's lines — the VMS's route, plugged in
GET http://10.0.0.11:8090/segment/vms/7/e3/…mp4   the bytes, Range honoured — the only path footage ever takes
```

The resource is a **platform job** — `psimplatform/resource.py`, job `resource` — and it knows nothing about video: a heartbeat every ten seconds under `platform/resources/<server>` (which subsystems have buckets here, whose copies it keeps, how much disk), HTTP over every subsystem's buckets, the mirror (Step 5a), and a retention pass that reads each subsystem's own `<sub>/retention` and `<sub>/retention/<unit>` rows. What the VMS adds is registered on it: `ArchivePolicy` — repair the manifests, close buckets into them, retain media per camera — runs as the `vms` hook of the same pass, and `vms_routes` plugs `/manifest` and `/segment` into the same server. The controller writes `vms/retention/<cam> {days: events_retention_days}` when a camera is created or edited, and that is the whole of how the VMS's events policy reaches the platform: a row. `resources_seen()` is the console's list of resources, with a state: live, or silent past `lost_after`. And the policy from М10 runs here as the job's ten-minute pass, `repair()` then `retain()` per camera from the row in Variables — `test_the_resource_policy_needs_neither_worker_nor_controller` deletes a file behind the manifest's back and shows `{'added': 0, 'dropped': 1, 'removed': 1}` with nobody else involved.

## Step 5 — One camera, two resources, one unreachable

Camera 7 recorded on A under epoch 3 until the failure, then on B under epoch 4. `cluster/timeline.py` asks every resource that reports the camera for its manifest and merges:

```
segments: (srv-a, e3, fenced) (srv-a, e3, fenced) (srv-b, e4)      unreachable: []
```

Now A's heartbeat goes stale:

```
segments: (srv-b, e4)      unreachable: ['srv-a']
note: ranges on srv-a are unavailable until the server returns — not lost
```

And when A heartbeats again, three segments, nobody rebuilt anything: the manifest was on the disks that came back. `test_a_timeline_spans_two_resources_and_names_the_unreachable_one` is those three states in order. Note what the console does *not* do while A is down: it does not guess at A's ranges from the epoch, and it does not print *gap*. It prints a server's name, because the operator's next action — go and look at srv-a — depends on it.

## Step 5a — Events: the database that is a cache

There is no database per recorder any more, and the question *where do events go* has an answer that follows from everything above rather than adding to it. An event is an observation — written by the worker that holds a unit's epoch, into that unit's bucket on its server's resource, recording or not (М10 Lesson 3, Step 5a). The VMS's buckets sit beside its footage under `vms/<cam>/`; a detector's sit under `det/<job>/` on the GPU server it runs on; a counter's under `counter/<unit>/`. On a cluster that means events live on **resources**, promoted, retained and fenced there, served by the resource job (`GET <resource>/buckets/<sub>/<unit>`, `GET <resource>/events/<path>`), unavailable when the server is and never lost.

What a cluster adds is search — across cameras, and across subsystems — and `psimplatform/eventdatabase.py` is that: **one database per resource, none per console, none cluster-wide.** Each resource job keeps an `EventDatabase` over its own tree — a SQLite table it fills by reading its own buckets (and the copies it holds of its peers', below) and that it can throw away — and answers `GET <resource>/events?cam=…` from it. The console holds no index: its `/events` asks every live resource and merges by time, exactly as `/timeline` merges manifests, naming the ones that did not answer and fencing each event by its unit's current epoch, which only the cluster's rows know (`cluster.console.MergedIndex`). Nothing crosses the network to *build* a database; nothing anywhere holds every server's rows. And it is М10's resource process (Lesson 9 there), run as a job: one architecture. The first draft had one `count = 1` index job reading every resource over HTTP — a cache, but a cluster-wide one, with a rebuild that scaled with the cluster and a copy of every event on one server. The three questions of the design record's *What is a subsystem* row settle where it belongs: the database has nothing to place, so it is not a subsystem; and what it holds is on a resource, so it lives with the resource. Its two properties are the controller's, in the form that matters for a cache:

```
srv-a: EventDatabase.rebuild() -> {added: 2, segments: 1, mirrored: []}     its own tree, nothing else; srv-c: {added: 2, segments: 2}
console: MergedIndex.query(t0, t1, cam=7, current_epochs={("vms","7"): 4})                          state: live
  -> (vms 7 motion srv-a e3 fenced) (det d-12 person srv-c e1) (vms 7 silent srv-a e3 fenced) (vms 7 motion srv-b e4)
query(subsystem="lpr")  -> [{kind: plate, plate: AB123}]                  a third subsystem, indexed without a line of code here
srv-a's job restarted: rebuild -> added 2, the same rows                  it is a cache, and it proves it
a new bucket on srv-c: srv-c tail -> added 1; the merge shows it          nobody else is told — the console asks
srv-a silent:   merge -> [8]                    state: "live; srv-a unreachable"     unavailable by name, not wrong
srv-a back:     merge -> [7, 8]                 state: "live"                        its database answers again; nothing rebuilt
srv-b live but not answering: merge -> [7]      state: "live; srv-b unreachable"
retention on srv-b removed a bucket: srv-b's database forgets it; the merge -> [7]
```

An operator's mark is the third kind of writer: `POST /marks` on the console writes into `console/<instance>/…` on the console's own server's resource (the controller job is constrained to `meta.archive` for this), and the index finds it by `cam` like a detector's. Read the second line of the query result. The detector's event is *about* camera 7 — it carries `cam: 7` as a field — and it was found by that field, not by living in camera 7's bucket, because it lives in `det/d-12/e1/…` on srv-c under the detector's own epoch: two subsystems, two writers, one join on a field. Fencing is per unit and the index only compares — `current_epochs` is keyed `(subsystem, unit)`, and only the unit's own subsystem knows its current epoch.

`test_events_are_indexed_by_each_resource_and_merged_by_the_console_which_holds_none`, `test_a_dead_resource_makes_the_answer_incomplete_by_name_not_wrong`, and `test_the_resource_policy_retains_each_subsystems_buckets_by_its_own_row` — the platform's pass retains a camera's buckets by the `vms/retention/<cam>` row the controller wrote and a detector's by `det/retention` or a year; the VMS's registered pass drops the manifest lines whose files went, and the resource's own database forgets the rows (`Resource.retain` calls `database.forget`). The database is `psimplatform/eventdatabase.py`, the resource job's. The second test's last line is the rule: `vars.list("vms/events") == []` — no controller wrote an event, nothing went to raft, nothing went to the object store. A resource job restarted rebuilds its database in seconds for a day of its own events and says *catching up* meanwhile, rather than answering short; a console restarted rebuilds nothing, because it holds nothing.

**Why not one eventcontroller writing a replicated database on two servers** — the obvious design, and the module's own rules say no twice. A single writer of all events is a serialization point on the hot path and a process on the recovery path of something that happens continuously; controllers write desired state, and observations are written by whoever observed them. And a database replicated across *two* servers with automatic promotion is the zombie writer one layer down: two is the number that cannot have a quorum, and a promotion is a decision made on a silence. If a customer needs event search to survive a dead server without waiting for it, the storage knob has an events row, and it is one Variable: `platform/mirror {enabled: true, copies: 1}`. No store sits in between — the first draft of this step mirrored to MinIO, and the honest question *why not just copy the events to another server* had no good answer. So the resource's policy pass grows a fourth verb, `mirror()`: every *closed* bucket on this server, any subsystem, is `PUT` to the next live resource after it in sorted order — srv-a to srv-b, srv-b to srv-c, srv-c to srv-a — which keeps it under `.mirror/srv-a/<original path>` on its own disks, outside its subsystem tree, and lists what it holds in its heartbeat (`mirrors: {"srv-a": 412}`). Nobody assigns peers; the rule is the assignment, and `copies: 2` means the next two. The peer says what it already has, so each bucket is copied once, by the server that owns it: one writer per key, still. The peer's own database covers the copies it holds — rows inserted under the *real* server and the original path, since only the source differed — and the console's merge, finding srv-a silent, takes them from whichever live resource answered with `srv-a`'s events, drops them again the moment srv-a answers for itself, and says where they came from:

```
knob off:  srv-a silent -> the merge: state "live; srv-a unreachable"
knob on:   pol[srv-a].once() -> mirrored: 2, peers: [srv-b]   closed buckets only; the open one is the RPO
           srv-b's database: rebuild -> {added: 3, mirrored: ['srv-a']}   its own bucket and srv-a's two copies, under srv-a's name
           srv-a live:   the merge -> srv-a's three events, from srv-a     the owner answers; srv-b's copies are dropped
           srv-a silent: the merge -> srv-a's two closed ones, from srv-b   state: "live; srv-a from mirror"
           srv-a back with an EMPTY disk: restore() -> pulled 2, manifest rebuilt      the owner brings its buckets home
           its database rebuilt: added 2; the merge -> the same two rows, state "live"    the resource is the source again; no duplicates
```

`test_the_events_knob_is_a_peer_copy_and_the_owner_restores`. Two things to hold onto. The mirror is never the source while the resource answers, and nothing but the owner ever copies a bucket back — `restore()` runs on the returning server, first thing, and only fills what it lacks. And the threshold is the cluster's own: lose two of three servers and one server's events are gone, which is also when Nomad's raft stops, so the knob adds no new failure domain. The RPO is one bucket length plus the policy interval, on observations; a customer who wants it smaller shortens `bucket_seconds`, not anything about footage. When there are *consumers* — a SIEM, a rules engine, a cloud uplink — events become a stream and NATS JetStream (Apache-2.0, R=3) is the on-prem transport with the index as one more consumer; nothing here prevents adding it.

## Step 6 — Where the acknowledgement problem went

The first design had to explain what the operator is told when they save a camera, because the recorder acknowledged on local commit and published later. Under 2c the controller acknowledges **after the CAS commit into raft**, which is replicated before it returns. The problem does not exist inside the cluster.

It survives one level up. М12's read model is built from the snapshot the controller publishes as an object (`objects/vms/snapshot`, a Variable here — Lesson 5) and from the workers' heartbeats, and *that* copy is stale by the interval — which is why every row the domain's console shows carries an age. The RPO moved from the cluster to the domain and shrank to a display age.

### Two things that stay open by design

**A worker with no assignment invents nothing.** `test_a_worker_with_no_assignment_invents_nothing`: index 5 comes up on srv-c, claims `w-5`, records nothing, and heartbeats `status: [], headroom: 50`. The controller assigns to it when there is work.

**Old footage on a dead resource is unavailable, not lost.** Playback of the ranges on A's manifest waits for A; the console says which ranges and which server.

**Deliverable:** kill the server under `w-1`; show it recording on another server into another resource within the measured time; show the edit made *during* the failover present when it returns; `GET /timeline/7` with A's ranges named as unavailable; bring A back and play across the boundary — three segments, two epochs, two servers, one answer.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| The timeline shows nothing from srv-a although it is up | Its heartbeat is stale — the resource job is not running there, or `meta.archive` is not set and the `system` job never placed. |
| `unreachable` names a server whose heartbeat is fresh | The manifest fetch failed: `RESOURCE_URL` is wrong (it must be the server's address, not `localhost`), or port 8090 is blocked. |
| A camera's segments appear under two servers *for the same minute* | Correct during the two-writer window (Lesson 4); the epochs differ and one is marked fenced. |
| The replacement worker starts with the *old* camera name | It is reading a cached row. The worker reads raft every pass; there is no cache to be stale. Find the cache and delete it. |
| Footage "disappears" when a server is drained for an update | It is unavailable for the drain's length. If that is unacceptable, the storage knob is the EC pool, with its costs. |

## Recap

- Nothing travels: configuration was never on the server. The RPO inside a cluster is zero.
- Footage stays on the resource; the manifest returns with the disks; the open segment is the loss.
- Not a CSI volume: #12118, and the recovery that needs a person.
- Per-server unreplicated by default; an EC pool by choice, with 800 Mbit/s and a quorum that stops every camera.
- A resource heartbeats and serves its manifests; a timeline merges across resources and names the unreachable one.
- The acknowledgement problem dissolved; what remains is the domain's display age.
- Events are buckets on the resource, per subsystem and unit, written by the worker holding the epoch; each resource's `eventdatabase` is a cache over its own, the console merges them and holds none, subsystems are joined on a field, and a silent server is named, not guessed; a controller writes none of them, and two servers cannot hold a quorum for anything.

## Exercises

1. Reintroduce the first design's `publish_once` on the controller and measure what it adds — writes per edit, objects per day — for a benefit you should be able to name and cannot.
2. Set `lost_after` for resources to 5 s and run the timeline test with a heartbeat delayed by 6 s. What does the operator see, and is it wrong?
3. Mirror events to MinIO in erasure-coded mode instead of to a peer. List what the cluster now runs that it did not, and find the failure in which the store is up and the copy is still unreadable.
4. Compute the storage knob's east-west traffic for your camera count and bitrate, and write the sentence you would say to the customer who asks for "replication".
5. A resource's disk is replaced with an empty one. Walk through what the console shows, what `repair()` does, and what the manifest can and cannot recover.

## Where this is going

What stays is settled. [**Lesson 4**](04-failover-and-the-two-instances-of-one-worker.md) is the failure itself: the `disconnect` numbers, the power pull measured, and the moment the dead server comes back with a worker that still thinks it owns three cameras.
