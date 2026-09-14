# Lesson 9 — Events: the Database That Is a Cache

**Module:** ServerVMS — the platform's shape on one server (Module 10)
**You will build:** the resource as a process — `python3 -m vms resource`, the platform's `Resource` with the VMS registered on it: a heartbeat, the policy pass, its HTTP, and an `EventDatabase` over its own tree — and a console that holds no database and asks. Three subsystems' events on one camera's timeline, fenced by their own epochs; a database that proves it is a cache by being thrown away; retention that takes the rows with the file.
**Time:** ~75 minutes.

## Why this lesson exists

Events have been written since Lesson 3 and shown since Lesson 6, and two questions were left open on the way: *who answers `/events?cam=7`*, and *why is that not a subsystem*. Lesson 6 answered the first with a database inside the console, over the local archive — which worked on one box and was wrong in shape: М11 would have needed a second answer, one job reading every server. This lesson gives the one answer that is the same on a box and on a cluster. The archive was a **resource** from Lesson 3 — pinned, registered on, never placed — and it now has the process that stands for it, the same process М11 runs as a `system` job on every server. What lies on a resource is indexed *by* that resource; a console with a question asks every resource it can find and merges. The events lesson is really the resource-process lesson, because the database is what a oneshot on a timer could not hold.

> **What you can verify without hardware.** `tests/test_lesson9_events.py`: the resource process served over HTTP and found by its heartbeat; a detector's `linecross`, an operator's `mark` and the worker's `silent` on one camera's timeline through the console, in time order, each under its own subsystem and fenced when *its* epoch moves; the resource process stopped and the console saying so by name; a fresh database over the same tree giving the same rows; retention on the resource taking a bucket's rows with its file; nothing about events in any store.

## Prerequisites

- **Lesson 3** — event buckets on the resource, `EventLog`, `ArchivePolicy`, retention by `<sub>/retention/*`.
- **Lesson 6** — the console, its mounts, `/events` on the page.
- **Lesson 8** — a worker of another subsystem writing events under its own epoch.
- **М9 Lesson 5** — the data partition; the units in Lesson 10 mount it.

## Learning objectives

1. Run the archive resource as a process: heartbeat, policy pass, HTTP, and say what the old timer could not do.
2. Build the event database as a cache over one resource's tree, and prove it by rebuilding it.
3. Make the console ask and merge, and say why it holds nothing.
4. Fence events per unit by the unit's own subsystem's epoch, and say why only the console can.
5. Answer *why events are not a subsystem* with the design record's three questions.

---

## Step 1 — The resource is a process

Lesson 3 called the archive a resource and ran its policy from a timer: `vms-archive-retain.timer` started a oneshot every ten minutes that repaired the manifests, closed the buckets, retained media by each camera's days and buckets by `vms/retention/<cam>`, and exited. Correct, and less than a resource. М11 Lesson 2 gave the same resource a *job* — a heartbeat under `platform/resources/<server>`, a port serving `/buckets` and `/events/<path>`, a mirror to the next server — and the box had none of it, so the two modules drifted: the box's console read the archive directly, the cluster's console asked a job.

`vms/resource.py` ends that. `vms_resource(archive, server, url, vars, objects)` is the platform's `Resource` on the archive's root with `ArchivePolicy` registered as the `vms` hook and an `EventDatabase` attached; `python3 -m vms resource` serves it, heartbeats every ten seconds, runs `pass_()` every 600 s — the VMS's hook first (repair, close, media retention), then the platform's bucket retention, then the mirror, which is off on one box — and starts the database. `deploy/vmsresource.container` replaces the timer and its oneshot. М11's `cluster/resource.py` is now one line: `cluster_resource = vms_resource`. One process, two modules.

```
python3 -m vms resource
  platform/resources/srv-1/heartbeat   {server, ts, url: http://127.0.0.1:8090, usage, units: {vms: [1], det: [1-linecross], console: [...]}}
  GET :8090/buckets/vms/1  /events/<path>  /mirrored/<server>  /manifest/1  /segment/<path>
  GET :8090/events?from&to&cam&kind&subsystem&unit     the event database over this tree
  every 600 s: policy {vms.added, vms.dropped, vms.closed, vms.media_removed, removed, enabled: false}
```

What the process has that the oneshot could not: a heartbeat, so the console can find it without being configured; a port, so nothing else needs to read the archive; and memory that lives longer than a pass, which is where the database goes.

## Step 2 — The event database, which is a cache

`psimplatform/eventdatabase.py` — `EventDatabase(root, server)` — is a SQLite table over one resource's tree: every bucket of every unit of every subsystem under `<root>/`, and every copy under `<root>/.mirror/<server>/` (М11's knob; empty on a box). `rebuild()` truncates and reads the tree; `tail()` reads what is new — a closed bucket once, an open one by the lines past what is held, since a bucket is append-only; `start()` does the first and then the second every three seconds in a thread. A row is `(subsystem, unit, cam, epoch, t, kind, server, bucket, fields)`, where `cam` is a field the event carried or the unit itself when the unit is a number: the database does not know what a camera is, only that a VMS timeline will ask by that column.

```
resource process:  rebuild -> {added: 2, segments: 2, mirrored: []}       state: live
a second EventDatabase over the same tree -> the same rows                  a cache, and it proves it
vms/retention/7 {days: 1}; resource.retain() -> 1                         the file went — and its rows with it
box.vars.list("vms/events") == []  objects.list("vms/events") == []      nothing about events in any store
```

Two properties, the controller's from Lesson 5 in the form that matters for a cache: it holds nothing it cannot rebuild — `test_the_database_is_a_cache_and_retention_takes_the_rows_with_the_file` builds a second one over the same tree and gets the same rows — and nothing running depends on it: workers write buckets, the policy retains files, and the database is told (`Resource.retain` calls `forget`) rather than consulted. A restarted process says *catching up* until its rebuild is done rather than answering short; on one box that is seconds for a day of events. `EVENTDB` is `:memory:` in the unit because the next start rebuilds it anyway.

## Step 3 — The console asks, and merges

The console has no database. `MergedIndex(objects)` — the same class in the platform for both modules — reads the resource heartbeats, asks every live resource `GET <url>/events?…`, merges by time, and returns the shape `SpecConsole` has always expected: `{events, state}`. On the box there is one resource and the merge is a pass-through; on a cluster it is a fan-out, the way `/timeline` has merged manifests since М11 Lesson 3. Three things happen in the merge and nowhere else. A copy a peer holds for a server that is itself live is dropped — the owner answers for itself. A server nobody answered for is named: `state: "live; srv-1 unreachable"`, not an empty list pretending to be complete. And every event is fenced by its unit's current epoch — `current_epochs` is `{(subsystem, unit): epoch}`, built by the console from *every* subsystem's `<sub>/epoch/*` rows — because only the console holds the cluster's rows; the resource has the files and the files do not know who holds the epoch now.

```
POST /det/units {name: 1-linecross, cam: 1, kind: linecross}   the detector, placed, fires on its third pass
POST /marks {cam: 1, note: check this}                          the operator's own bucket, console/<instance>/e1/
the camera goes dead: the worker writes silent                  vms/1/e1/
resource.tail() -> added 3;  the console was not told
GET /events?cam=1 -> [(det linecross srv-1) (console mark srv-1) (vms silent srv-1)]   state: live
det/epoch/1-linecross -> 2:  GET /events?cam=1 -> det fenced, console and vms not
the resource process stops: GET /events?cam=1 -> []   state: "live; srv-1 unreachable"
```

`test_three_subsystems_events_reach_one_timeline_through_the_resource_process_and_the_console`. Read the fencing line twice. The detector's events are fenced when `det/epoch/1-linecross` moves and the worker's are not, because fencing is per unit and each unit has exactly one subsystem that issues its epoch; a database that fenced everything about camera 1 by the VMS's epoch would mark a detector's true observation stale because a *recorder* restarted. And read the last line: the console stays up with no database behind it, and says what it does not have.

The page shows all of it, and nothing on the page knows what an event means. Under the timeline, an *Events* list: every row the merge returned for the unit and the window — time, kind, `subsystem/unit`, server, epoch, and the event's own fields (a mark's note and user, a detection's score, a `pass`) — with a filter by subsystem, a legend that assigns a colour to each subsystem in order of first sight, and the ticks on the timeline in the same colours; a fenced event is faded in both places; a click on either plays the recorded span that holds that moment and seeks the player to it, or says *not recorded at that moment*. The merge's `state` is beside the heading, red the moment it says anything but `live`. A *Mark* box posts the operator's own event, which comes back through the resource's tail three seconds later like anyone else's. And beside the live picture, a feed: while a viewer watches, the page polls `/events?from=<since>` every three seconds — the resource's tail interval — and prepends what arrived, a detector's crossing under the live frame it happened in. The picture comes from the gateway, the events from the resource; the page joins them by time and nothing else. The list refreshes every five seconds on its own, so the archive view lags the resource by one tail and the page by one poll, and neither the console nor the page holds a row.

## Step 4 — Why events are not a subsystem

The design record's row *What is a subsystem, and what is not* asks three questions: a process that moves, a unit that is placed, a capacity the worker measures. Events answer no to the first: they are a data shape on a resource, written by whoever holds the unit's epoch, so an "events subsystem" would be a second writer on camera 7 needing an epoch for a unit it does not run. The database answers no to the second: it indexes what lies on a resource, so it has nothing to place — it is where the buckets are, and moving it would mean moving the disk. And the console's merge answers no to the third: it has no capacity, because it holds nothing. The first draft of М11 had one `count = 1` index job reading every resource over HTTP — a cache, but a cluster-wide one, with a rebuild that scaled with the cluster and every server's rows on one server; the second draft put it beside every console; both were an index looking for a home, and the home was the resource all along. What *would* make it a subsystem is written down so the rule is testable: when "the buckets of resource srv-a" is a unit worth placing somewhere other than srv-a — and until then it is not placed at all.

**Deliverable:** `vmsresource.container` running beside the worker, the controller and the console — heartbeating, retaining, serving `/events`; a line-crossing model, an operator's mark and a silent camera on one timeline through `/events?cam=`, each fenced by its own subsystem's epoch; the resource process restarted and the timeline back within seconds; a camera's `retention_days` shortened and its old events gone from the timeline on the next policy pass; the page's events list, its live feed and its Mark button all fed by `/events`; `test_lesson9_events.py` green.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `/events` answers `[]` with `state: "live; srv-1 unreachable"` | The resource process is not running or not on `RESOURCE_URL`; the console found its heartbeat (younger than 45 s) but nothing answered on the port. `curl 127.0.0.1:8090/events` first. |
| `/events` answers `[]` with `state: "live"` and no server named | No resource heartbeat at all: the process never started, or `PLATFORM_DIR` differs between the two units. `/resources` on the console lists what it sees. |
| A detector's event shows `fenced: true` while the detector runs | Its epoch moved — another instance took the unit — and the event was written under the old one. Correct: the fence is per unit, by *that* subsystem's epoch. |
| The timeline lags a new event by more than a few seconds | The tail interval is 3 s and the bucket must be on disk: a worker buffers nothing, but a detector fires on its own schedule. Rebuild is never needed for an open bucket. |
| Old events remain after `retention_days` was cut | Retention runs on the policy pass, every 600 s; the database forgets on the same pass. `/events` never lies in between: the rows are there because the file is. |

## Recap

- The archive resource is a process — heartbeat, policy pass, HTTP, database — the same process М11 runs as a job; the timer is gone.
- The event database is a cache over one resource's tree, rebuilt on start, tailed every few seconds, told by retention what it lost.
- The console holds no database: it asks every live resource and merges, names what did not answer, and fences by every subsystem's epochs.
- Events are a data shape on a resource, the database has nothing to place, the merge has no capacity: none of the three is a subsystem.

## Exercises

1. Set `EVENTDB=/data/archive/.eventdb` and restart the resource process. What changes in the first three seconds, and what does not change at all?
2. Two detector instances race for `det/epoch/1-linecross`. Draw the timeline as `/events` shows it before and after the CAS, and mark which rows the loser's tail added.
3. Move the merge from the console into the resource — each resource asking its peers. What breaks first: the fence, the state line, or the number of requests?
4. The page polls `/events` every few seconds. Estimate the request rate on a cluster of forty resources with ten operators, and say which of the three places (page, console, resource) should cache — and why the answer is not the database.

## Where this is going

One box now runs exactly М11's set of processes: a worker, a controller and a console per subsystem, and one resource process with the event database in it. [**Lesson 10**](10-on-the-box.md) puts all of it on the box М9 built as Quadlet units, `vmsresource.container` among them, and М11 turns the same units into jobs with a server dying between them.
