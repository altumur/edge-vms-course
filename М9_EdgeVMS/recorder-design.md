# М9_EdgeVMS — Module Design

**One recorder learns what it should be, and closes the gap itself.**

М8 built a VMS with no database — Kinesis held the configuration and the archive both. М9 made the box atomic and replaceable, but a box still only knows what was flashed onto it. This module is where a recorder learns **what it should be**: five lessons in which `INSERT INTO cameras` causes a camera to start recording, `DELETE` causes it to stop, and nothing sits in between but a loop the student wrote.

**What this module builds is a recorder**, and the capital letter matters from М11 onward. A recorder is not a server: it is a VMS instance that owns its own database, its own cameras and its own archive — and in М11 it becomes a scheduler allocation that moves between servers, carrying its cameras with it. Everything built here travels intact.

> **Scope note.** This module has been assembled three times, and the last move is the one worth knowing. The course plan first had М9 as Postgres alone with the loop deferred to М11; the loop came back, because a database nothing acts on is not a working system. Then М9's multi-node half landed here — it is desired-state work, and М9 is supposed to be one box. It did not stay: a Nomad cluster and the layer above it turned out to be one arc cut in the wrong place, so scheduling went on to [М11](../М11_ClusterVMS/module-design.md). What is left is one recorder, which is what the name promises — and М11 takes that same recorder, runs several of them, and moves them between servers without changing anything built here.

---

## The thesis

Every layer so far has had a single source of truth that lived somewhere else. Now the box owns it, and owning truth means being able to answer one question:

| | Holds | Written by | Survives |
|---|---|---|---|
| **Desired state** | What the operator asked for | The operator, through the API | Reboots, OS updates, the AppHost dying |
| **Actual state** | What is running right now | The AppHost, by observation | Nothing — it is re-derived every time |

> **The rule that organises the whole module: desired state is persisted, actual state is derived.**

A student who persists actual state has built a cache that goes stale and lies. A student who forgets to persist desired state has built something that forgets its cameras on reboot. Both mistakes are worth making once, deliberately, in Lesson 6.

The convergence test is one comparison, and it is the same one at every layer above this: **applied means `observed_revision >= revision`.**

### The pattern is not exotic

What the student builds here is what a scheduler already is: desired state in one place, actual state observed, and a loop closing the gap. They write it by hand first, at a scale where both ends fit in one terminal.

М11 then hands them a production implementation of the same idea — a Nomad jobspec *is* desired state and its scheduler *is* the loop — and puts the two side by side. Meeting a scheduler after having written one is what stops it being magic, and it is why М11 can argue for two reconcilers at two levels without that sounding like an abstraction.

---

## The demo the module is built backwards from

```sql
INSERT INTO cameras (name, rtsp_url, site_id, enabled)
VALUES ('front-door', 'rtsp://10.0.0.41/stream1', 'store-14', true);
```

Within a few seconds, without anyone restarting anything: a pipeline is running, segments are landing on the data partition, and `SELECT name, phase, observed_revision FROM camera_status` says so. `UPDATE ... SET enabled = false` stops it. `systemctl kill apphost` loses nothing but the open segment, and the box converges again on restart.

If a lesson does not move that demo forward, it does not belong in this module.

---

## Decisions taken

| Decision | Choice | Why |
|---|---|---|
| Scope | **One recorder, one server, end to end** | The loop is the lesson, and it is far easier to see when both ends are in one terminal. Scheduling recorders across servers is М11's. |
| Worker model | **N pipelines in one Python process** | The conclusion of [`apphost-and-process-model.md`](apphost-and-process-model.md), now built. Container-per-camera is М9's world and stops being right near fifty. |
| Actual state | **Derived, never persisted** | Kill the AppHost and it must rebuild its picture from Postgres plus observation. Anything it remembers across a restart is a bug. |
| Change notification | **Poll on a timer, `LISTEN/NOTIFY` for latency** | NOTIFY is not durable — a listener that was disconnected misses it forever. Notify for speed, poll for correctness. Teaching only NOTIFY produces a system that silently stops converging. |
| recorder visibility | **Decided for the operator, never by them** | See below. The `cameras` table has no recorder column a client may write. |
| Language | **Python for the course; Go + C++ for the product** | Python teaches the loop and makes the language boundary visible. The product splits it — Go for the controller, C++ for the media worker — and Lesson 9 says why that split costs almost nothing. |
| Databases | **One, and the recorder owns it** | Configuration, archive index and events in one Postgres. М11 adds recorders, not a second database — the domain above them is a Nomad Variable and an object store, so nothing here is ever demoted to a cache. See [`where-the-database-lives.md`](../М12_DomainVMS/where-the-database-lives.md). |
| Database placement | **On the data partition, as a Quadlet unit** | М9's three-way boundary with consequences: `PGDATA` in a rootfs slot is destroyed by the next OS update. |
| Authentication | **One hand-provisioned operator, marked temporary** | On one recorder there is nothing to decide. The `grants` table exists from Lesson 5 so М12 adds policy rather than schema — but the `operators` table is **superseded** there rather than extended: with N recorders a local password hash is N Alices, and М12 replaces it with an issuer's public key. |
| Local storage engine | **Postgres, not SQLite** | The archive index and the event stream need a real database regardless, so a second engine for a small cache is pure cost. Partitioning is the deciding feature. |
| Camera credentials | **Split out of the URL and encrypted at rest** | An RTSP URL carries the password inline, so `rtsp_url text` silently stores a thousand customer passwords in plaintext — and in every log line that URL reaches. The key placement is the real problem: the data partition here, a Nomad Variable or the TPM later. |
| DB credentials | **Hand-provisioned, marked temporary** | Follows the course's existing discipline. М12 replaces this with certificate auth once the box has an identity, and the replacement is the lesson — but the temporariness is stated here, not discovered there. |

---

## Prerequisites

- **М8 Lesson 2** — process supervision, signals, and the self-matching `pkill` bug. The AppHost is what `looper.py` grows into.
- **М8 Lesson 4** — GStreamer pipelines and what each element does. Lesson 7 builds them from Python instead of a shell string.
- **М9 Lesson 4** — Quadlet, and the OS/app/data boundary that decides where `PGDATA` goes.
- **М8 Lesson 6** — configuration is read from the environment; credentials are never in the image.

New assumed knowledge: SQL at the level of `CREATE TABLE`, `JOIN` and `INSERT`. No prior Postgres administration.

---

## How the shard is actually organised in Python

The question this module has to answer honestly, because the intuition is that Python cannot do this and the intuition is wrong for a specific and teachable reason.

### Where the work happens

Once `pipeline.set_state(Gst.State.PLAYING)` returns, buffers move on **GStreamer's own native threads**, inside libgstreamer, in C. Python is not in that path. And PyGObject documents that "all PyGObject calls release the GIL during their execution and other Python threads can be executed during that time."

So a worker holding fifty recording pipelines is running fifty pipelines' worth of C and a trickle of Python: a bus message every few seconds, a state change when configuration changes, a status write every five seconds. The GIL is close to uninvolved.

### The seam where it goes wrong

PyGObject also documents that "signals get executed in the context they are emitted from." A callback attached to a signal or a pad probe therefore runs **in the streaming thread**, and to run Python there it must take the GIL.

Attach a `GST_PAD_PROBE_TYPE_BUFFER` probe to fifty cameras at 25 fps and that is **1,250 GIL acquisitions per second**, serialised through one lock, each one interpreting Python. That is how a Python media worker dies, and it has nothing to do with how many pipelines are in the process.

> **The rule: Python touches control, never data.**
>
> Banned in the recording path: `appsink`, `identity handoff`, buffer-level pad probes.
> Fine: bus messages, state changes, `splitmuxsink::format-location` (once per segment).

### Stall detection without touching a buffer

The obvious way to notice a camera that has stopped sending while its TCP socket stays open is to timestamp every buffer — which is exactly the banned thing. GStreamer already solves it in C: the **`watchdog`** element from `gst-plugins-bad` passes buffers through untouched and posts an **error on the bus** if none arrive within `timeout` milliseconds (default 1000; a few seconds is right for cameras).

```
rtspsrc ! rtph264depay ! h264parse ! watchdog timeout=8000 ! splitmuxsink
```

Zero Python in the data path, and the failure arrives on the bus the AppHost is already reading. This one element is worth a section of Lesson 7 on its own, because it is the model for the whole design: push the per-frame concern into C, keep Python at control rate.

### The event loop, and not having two of them

The AppHost speaks asyncio to Postgres and to its API. Running a `GLib.MainLoop` alongside it gives the process two schedulers and two notions of "later".

Don't. Each pipeline has its own bus; one asyncio task drains all of them with the **non-blocking** `bus.pop_filtered(...)` on a short tick:

```
Worker  (one process = one shard)
  asyncio tasks
    reconcile()    every 2 s, and on NOTIFY     desired (Postgres) vs actual (dict)
    pump_buses()   every 200 ms                 non-blocking pop on each pipeline's bus
    report()       every 5 s                    write observed_revision + conditions back
  CameraPipeline   per camera: state machine, backoff, current segment
                   IDLE -> STARTING -> RUNNING -> FAILED -> (backoff) -> STARTING
```

Fifty non-blocking pops every 200 ms costs nothing measurable. `bus.get_pollfd()` with `loop.add_reader()` is the tidier version and makes a good exercise; it is not worth the fragility as the default.

### What it costs

Each recording pipeline creates roughly three to five native threads, so fifty cameras is 150–250 threads in the process. Linux is fine with that, but it is a number to measure rather than assume — `reference/shard-memory-probe.py` from М9 is extended in Lesson 7 to report it alongside PSS.

And the honest cost: **one segfault takes the whole shard.** That is the price of sharding, paid in exchange for the per-process baseline. It is bounded by shard size, by systemd restarting the unit, and by `splitmuxsink` — a crash loses the open segment and nothing else.

### The rule is not really about the GIL

Worth stating explicitly in the lesson, because it is the part that survives a change of language. The constraint is not Python's lock. It is **crossing a language boundary once per frame.**

| | What a per-buffer callback costs | Verdict |
|---|---|---|
| **Python** | Acquire the GIL and interpret. Fifty cameras at 25 fps is 1,250 acquisitions per second through one lock | Fatal |
| **Go** | Enter the Go runtime from a C thread through cgo. Cheaper than Python, still real, and the rules on passing pointers make it awkward | Same discipline required |
| **C++** | Nothing. There is no boundary | The rule dissolves |

That last row is the real argument for C++ in the media worker, and it is a better one than "C++ is faster" — which, for a pipeline that never decodes, would barely be true.

---

## What the operator never decides

The instinct is right: an operator wants to assign cameras, not machines. The useful part is knowing exactly where that stops being true — and that needs two words kept apart, because the course uses them precisely from here on.

| | What it is | Who decides |
|---|---|---|
| **recorder** | a VMS instance — this module builds one. Its own database, its own cameras, its own archive index. From М11 it becomes a scheduler allocation with stable identity and **moves between servers** | an operator, when capacity is bought |
| **Server** | a box with CPUs and disks, running whichever recorders it is given | the scheduler, continuously |

This module has exactly one recorder on exactly one server, so the distinction costs nothing here. It becomes load-bearing in М11, where a server dying **moves the recorder** rather than reassigning its cameras — which is why failover there rewrites no ownership at all.

**Which recorder owns a camera is decided for the operator, never by them.** The schema consequence Lesson 5 makes concrete: the `cameras` table has **no recorder column a client may write**. Placement is a separate, controller-owned row with its own revision, and the API refuses it.

But servers are physical, and physics leaks in four places where hiding it would be a lie:

| Where it surfaces | What the operator actually needs to know |
|---|---|
| **Capacity** | "You cannot add camera 1001." Expressed as *the system is full*, not *recorder 3 is full* — but the number has to come from somewhere real. |
| **Storage locality** | Recordings live on the **server** that wrote them, and a recorder moving does not move them. A dead server is unavailable footage until it returns, and that must be visible before it dies. |
| **Failure grouping** | When a server fails, its recorders move and two hundred cameras go red together. The console must show one cause, not two hundred faults — which means grouping by failure domain, which means naming the **server** at that moment. |
| **Reachability** | A camera on an isolated VLAN may be reachable from only some servers. The operator expresses this as a **site**; the controller turns it into a constraint on where that recorder may run. |

> **Site is a first-class operator concept. Server is not, and recorder barely is.** Sites are where cameras are; recorders are how the work is divided; servers are how much hardware it took.

So: invisible in configuration, visible in diagnostics and capacity. The same relationship a filesystem has to disks — you do not assign files to spindles, and you certainly see the spindle when one fails.

---

## Lessons

*Five lessons, one recorder on one server. The student writes the reconciler.*

All five are written: see [`README.md`](README.md) for the index and what can be verified without hardware.

### Lesson 5 — The database the cloud VMS didn't need

- Why М8's spec forbade a database, and why the answer flips on-prem: in the cloud KVS held the configuration; on a box, the box holds it
- **One database, and this recorder owns it.** Not a cache of anything: the recorder is the authority for its own configuration, and М11 keeps it that way when several recorders appear — nothing above ever writes these rows. **There is no second database here and none arrives later**, which is worth saying plainly because most control-plane courses would put one in
- **Three kinds of data, one engine.** *Configuration* — cameras, streams, sites, retention policies — is what an operator asked for. The *archive index* and the *event stream* are what this box observed. They differ in almost every property except the engine they run on, and Lesson 8 depends on the difference
- **Configuration schema:** cameras, streams, sites and retention policies
- **Observation schema:** the archive index (which segment covers which camera over which range, as a `tstzrange` with a GiST index — М8's timeline query, answered directly) and the event stream (motion, camera offline, operator actions, with a JSONB payload because detectors differ)
- **Time partitioning from day one.** Both index and events are rolling windows taking on the order of a hundred rows a second at scale. Retention drops whole partitions rather than deleting rows — **note the syntax: PostgreSQL has no `DROP PARTITION` statement** (that is Oracle/MySQL), it is `ALTER TABLE … DETACH PARTITION` then `DROP TABLE`. Measured while writing Lesson 5: `DELETE` of 276,768 rows took 231 ms and **freed no disk at all**; detach-and-drop took 5 ms and returned 38 MB. Lesson 8 collects on this
- **The pruning trap**, found by running it: partition pruning needs a predicate on the *partition key*, so `span && …` alone opens every partition's index. Queries must bound `lower(span)` explicitly
- **Events are not metrics.** An operator searches events; an engineer alarms on metrics. They look alike and belong in different modules — М13 has the second kind
- **Operators and grants, in the schema from the start.** An `operators` table, and a `grants` table carrying `subject`, `capability` and `valid_until`. On one recorder authorization is a non-problem — one operator, all rights — so this lesson builds the tables and no policy. **The expiry column is unused here and present so that М12 populates rather than migrates.** The lesson says so, rather than leaving a student to wonder why a column does nothing
- **The credential hiding in `rtsp_url`.** The URL carries `user:pass@` inline, so the obvious schema stores every camera's password in plaintext. Split it into `cred_username` and an encrypted `cred_secret`, and be honest that **key placement, not encryption, is the hard part** — a key on the same partition as the database is in every backup of it. These are the *customer's* secrets, not the product's: unrotatable, unchosen, and often identical across every camera an installer touched
- **Operator-owned columns versus controller-owned columns.** `enabled`, `rtsp_url`, `cred_username`, `retention_days`, `site_id` are written by people; `revision`, `assigned_worker`, `observed_revision`, `phase` are written by machines and never appear as form fields
- `revision` as a monotonic, controller-assigned integer per object — not a hash, not a timestamp
- Migrations as a shipped artifact, and the appliance constraint: they run at boot on a box nobody visits, so they must be idempotent and must never be able to leave it unbootable
- **`PGDATA` on the data partition.** Postgres as a Quadlet unit with its volume outside both rootfs slots — М9's boundary with teeth
- The database password and the operator credential are both hand-provisioned and **marked temporary in the lesson text**; М12 replaces them

**Deliverable:** schema and migrations applied, Postgres surviving a simulated A/B update with its data intact.

---

### Lesson 6 — A reconcile loop with nothing in it

The `camera_sim.py` move, applied to control: build the loop before the thing it controls.

- Read desired from Postgres, compare against an in-memory dict of actual, log the difference. The actuator is a `print()`
- The vocabulary: desired, actual, converged, lagging, stalled — and `observed_revision >= revision` as the only test of "applied"
- **Poll versus `LISTEN/NOTIFY`.** Notify makes it fast; the timer makes it correct. A disconnected listener misses notifications permanently, so a system with only NOTIFY stops converging and does not say so
- asyncio structure: one task per *concern*, not one task per camera
- **Deliberate mistake, then fix:** persist actual state, restart the process, and watch it confidently report pipelines that are not running

**Deliverable:** an AppHost that converges a fake world, and passes a test that kills it mid-change.

---

### Lesson 7 — Fifty pipelines in one process

Swap the `print()` for GStreamer. This is the module's technical centre; see *How the shard is actually organised* above.

- Building pipelines from Python with PyGObject rather than a shell string
- **The global interpreter lock (GIL) boundary**, demonstrated rather than asserted: add a buffer pad probe, watch the worker fall over, remove it
- Draining buses from asyncio without a `GLib.MainLoop`
- The per-camera state machine, and where backoff lives
- **The `watchdog` element** — stall detection in C, delivered on the bus
- Re-run the М9 probe against the real worker; add thread count to what it reports
- **The spool becomes the archive.** М9 wrote segments to `/data/spool` to survive an uplink outage and deleted each one on acknowledgement. Here the same `splitmuxsink` writes the same files and **nothing deletes them** — an index row is written instead, and the uploader becomes optional. *The pipeline barely changes; what changed is who owns the footage.* Point at it, because it is the module's thesis in one diff

**Deliverable:** insert a row, get a recording. Delete the row, the recording stops. Fifty cameras in one process, with measured memory and thread counts — and a `git diff` against М9's pipeline that fits on one screen.

---

### Lesson 8 — Failure is the feature

Each failure mode reproduced on purpose, then handled.

- **Camera offline** → exponential backoff **with jitter**. Two hundred cameras reconnecting in lockstep after a switch reboot is a self-inflicted outage, and the jitter is the whole fix
- **Stalled stream, socket still open** → `watchdog` fires, that one pipeline restarts, the other forty-nine never notice
- **Disk full** → retention enforcement degrades by policy, and this is М9's spool-bound question returning with the answer changed: there, a full disk meant choosing between dropping the oldest and stopping recording, because the footage was in transit. Here it is *the archive*, so retention decides and the choice is the customer's, written down. The deletion loop must be conservative: never delete what it cannot prove is superseded. With Lesson 5's partitioning this is a partition detach-and-drop plus a segment unlink, not a scan — which is what makes it fast enough to run under pressure. Order matters: drop the index rows *before* unlinking, so a crash leaves orphaned files rather than index rows pointing at nothing
- **The AppHost dies** → systemd restarts it, state is re-derived, and the segment discipline bounds the loss
- **The fencing rule, introduced small:** on restart, never resume the previous segment — open a new one. Leases and epochs are М11's problem; the rule that makes them necessary lands here

**Deliverable:** a test suite that kills, fills, stalls and unplugs, and asserts convergence after each.

---

### Lesson 9 — What the console shows, and what Python stops being right for

- The joined view: desired and observed in one query, so "is this camera actually recording?" is not three round trips
- **The console requires a login**, against Lesson 5's `operators` table — one hand-provisioned account, all capabilities, **marked temporary**. No VMS ships with an open API, and this is the last module where there is exactly *one* surface to protect: М11 gives every recorder its own, which is where authorization stops being trivial
- Status vocabulary for the UI: `converged`, `lagging`, `stalled`, `unreachable` as *positions*; licence, storage and reachability as **conditions** — reasons an object cannot converge, kept out of the phase enum
- **The recorder-versus-server conversation**, from the section above: what the operator is asked, and the four places the server has to surface anyway
- **The rewrite sidebar.** Three things end Python's case for the product: the per-process baseline `B` is larger than a compiled worker's, one segfault takes the whole shard, and any requirement for per-frame work in Python is fatal by the table above
- **The split that follows from it.** **Go for the controller** — it is a gRPC-and-Postgres service, which is Go's centre of gravity, and its per-frame exposure is zero because the controller never touches a buffer. **C++ for the media worker** — GStreamer is a C library, so C++ calls it with no binding layer at all, and existing pipeline code can be reused rather than ported
- **What the rewrite does *not* touch**, which is the point of having written it in Python first: the schema, the reconcile loop, the state machine, the backoff policy and the desired/actual contract are all language-independent. Only the actuator changes. Building it in Python proved the design cheaply; it did not waste the work
- **Binding reality**, because it is easy to choose wrong here: `gstreamer-rs` is maintained by GStreamer's own developers and is the strongest non-C binding; `go-gst` is the live Go one; `gstreamermm` for C++ has been archived, so C++ means calling the C API directly — which is what C++ projects do anyway

**Deliverable:** the console view, and a written statement of every decision the operator is never asked to make.

---

## Verification plan

Better than М9's, because almost nothing here needs hardware.

**Track 1 — verified in the authoring sandbox, and now actually run.** Postgres 16.13 and plain Python produced every figure printed in Lessons 1, 2 and 4 — the retention timings, the pruning plans, and the seven reconciler tests including the 200-camera jitter spread. The schema, migrations, the reconcile loop and the state machine are all ordinary software. The loop is tested against a fake actuator exactly as М8 Lessons 5–8 tested against fake AWS objects, which means convergence, backoff, restart and the deliberate mistakes are all provable here.

**Track 2 — needs a real bench.** Anything with GStreamer in it: the pipeline strings, the GIL demonstration, the `watchdog` timing, and the memory and thread measurements. The authoring sandbox has no GStreamer and the package mirrors are blocked, so Lesson 7's numbers come from the student's box, produced by a script that ships with the module rather than from figures asserted in the text.

Every lesson marks which of its claims were run and which are documentation-derived.

---

## Open questions

1. **Does the API belong here or in М11?** Lesson 9 builds a read view. A write API with authentication is arguably М11's and arguably М12's; М11 currently builds it.
2. **How much retention policy is domain design rather than infrastructure?** The deletion loop is М9; schedules, per-camera overrides and legal-hold are product decisions that may deserve their own lesson in М11.
3. **Postgres in a container or on the host?** The module currently says Quadlet unit. On an appliance that nobody administers, a host package with systemd is a defensible alternative and the tradeoff is worth teaching either way. Note this now carries both databases, so the answer applies to the archive index and the event stream too.
4. **Does Lesson 8 need a real camera that misbehaves?** Cheap cameras stall in ways a simulator does not reproduce faithfully, and the module's most valuable failure mode is the hardest to fake.
5. **Does the shard-memory probe belong here or in М11?** It ships with this module and Lesson 7 runs it, but М11 Lesson 1 runs it again to derive a shard size. Duplicated use, single home — worth confirming that is the right call.

---

## Sources

- [PyGObject — Threads & Concurrency](https://pygobject.gnome.org/guide/threading.html) — "all PyGObject calls release the GIL during their execution"; "signals get executed in the context they are emitted from"
- [GStreamer `watchdog` element](https://gstreamer.freedesktop.org/documentation/debugutilsbad/watchdog.html) — `timeout` in ms, default 1000, posts an error to the bus when no buffers arrive
- [`gstwatchdog.c`](https://github.com/GStreamer/gst-plugins-bad/blob/master/gst/debugutils/gstwatchdog.c) — the implementation, for the lesson that reads it
- [GStreamer pipeline manipulation](https://gstreamer.freedesktop.org/documentation/application-development/advanced/pipeline-manipulation.html?gi-language=python) — probes and their thread context
- [GStreamer bindings](https://gstreamer.freedesktop.org/bindings/) — which bindings are officially maintained
- [`go-gst`](https://github.com/go-gst/go-gst) — the live Go binding, successor to `tinyzimmer/go-gst`
- [`gstreamermm`](https://github.com/GNOME/gstreamermm) — archived, which is why C++ uses the C API directly
- [`apphost-and-process-model.md`](apphost-and-process-model.md) — the process model this module implements

*Written 4 September 2026.*
