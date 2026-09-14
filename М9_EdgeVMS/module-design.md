# М9_EdgeVMS — Module Design

**From cloud service to a box a customer plugs in and forgets about — and a box that knows what it should be.**

М8 built a VMS that runs when you type `make serve`, with Kinesis holding the configuration and the archive both. This module, in two halves, turns it into an appliance and then gives the appliance its own truth. **Lessons 1–4** are the box: an operating system that can be replaced atomically and rolled back with nobody on site, and an application that survives that replacement. **Lessons 5–9** are the recorder that runs on it: a database holding what the box should be, and a loop the student writes that closes the gap. One project ([`edgevms/`](edgevms/README.md)), one box, nine lessons.

> **Revision note.** This module has been assembled more than once. It originally ran to nine lessons with a Part B on multi-node scheduling; that work moved to [М11](../М11_ClusterVMS/module-design.md), where workers are scheduled across servers and made to survive one dying. The recorder's five lessons were then written as a module of their own, and folded back in when it was clear that a database nothing acts on is not a working system and that the box and what runs on it are one deliverable. The two design records became the two parts below; the orchestrator comparison that shaped the old Part B is still recorded in [`kubernetes-vs-nomad.md`](../М11_ClusterVMS/kubernetes-vs-nomad.md).

---

## Part 1 — The appliance: Lessons 1–4

### The thesis

There are **two independent update planes** in any real edge product, and the whole module is built to make that distinction land:

| Plane | Question it answers | Tool here | Changes when |
|---|---|---|---|
| **Below** | What operating system is this box running? | RAUC | You ship a new appliance image |
| **Above** | What workload is running on it? | Podman and Quadlet | You ship a new app version |

Students routinely conflate these, then build systems where a config change requires an OS flash, or where an OS update silently destroys recordings. The module's spine is: **the OS is atomic and replaceable; the app is a container; the data is neither and must outlive both.**

That third clause is the one students nod at and then violate, so Lesson 4 makes it expensive to get wrong. **Footage recorded but not yet uploaded is data**, and an appliance that keeps it anywhere an OS update can reach has not understood the sentence.

Both planes are fully visible on one box, which is why this module needs only one box. Lesson 4 is where the distinction bites: Podman's storage must be redirected to the data partition, because container images and volumes left in a rootfs slot are destroyed by the next OS update. That single configuration line is the thesis made concrete.

**М9 adds a scheduler above this**, not instead of it. Podman remains the runtime there — Nomad's Podman task driver means the scheduler sits on top of what students already know rather than replacing it. One runtime, one mental model, from a single appliance onward.

---

### Decisions taken

| Decision | Choice | Why |
|---|---|---|
| Target platform | x86-64, UEFI + GRUB | How a VMS appliance actually ships. Testable end-to-end in QEMU before touching metal. |
| Payload | M1–7 app, **KVS retained** | The edge box becomes a managed gateway that still publishes to AWS. Deployment and lifecycle are the new skill. |
| Uplink loss | **Spool to the data partition, then upload** | The one media change this module makes, and it is forced: you cannot buffer behind `kvssink`, and an appliance that loses footage whenever the link blinks is not an appliance. Segments are written locally and uploaded by a separate process. |
| Those segments | **Become the archive in М9** | They are written here as a buffer and never thrown away: М9 puts an index over the same files, М12 makes the upload optional. The first thing in the course a later module *upgrades* rather than replaces. |
| Scope | **One appliance** | Scheduling, clustering and multi-site delivery moved to М9 and М12. A module called EdgeVMS should not build a raft cluster. |
| Bundle format | **`verity`, set explicitly in the manifest and enforced with `bundle-formats=-plain`** | RAUC still defaults to the legacy `plain` format with only a warning if you configure neither. `verity` is also what makes installing from an HTTP URL possible at all. |
| Slot status storage | **`data-directory`, not `statusfile`** | `statusfile` is deprecated in current RAUC. Most tutorials still use it. |
| Bundle delivery | Plain HTTP(S) | `rauc install https://…` keeps the focus on the update mechanism. **Eclipse hawkBit** is the production answer and now matters more than it did: it restores pull-based OS updates, partly offsetting the reconciliation lost with Fleet. Candidate for promotion out of a footnote. |

---

### Prerequisites

Carries forward from the existing course:

- **М8 Lesson 3** — containers, images vs. containers, Dockerfile, why credentials are passed by name and never baked in
- **М8 Lesson 6** — `config.py` reads settings but never credentials; boto3 finds them in the environment. That discipline is what makes an appliance image shippable
- **М8 Lesson 2** — process supervision and signals. systemd replaces `looper.py`'s hand-rolled supervision here; М9 later adds a scheduler above systemd for multi-node work. The comparison is worth making explicit at both steps

New assumed knowledge: none.

---

### Lessons

All four are written: see [`README.md`](README.md) for the index and the honest account of what can be verified without hardware.

*Four lessons, one box. Nothing here depends on an orchestrator, which is the point.*

#### Lesson 1 — The appliance problem, and your test bench

**Why:** A box in a customer's server room has three enemies your laptop doesn't: power loss halfway through an update, a bad update with nobody on site to fix it, and an operator with no Linux skills. `apt upgrade` fails all three.

- Mutable vs. atomic systems; why "update in place" is the thing being replaced
- A/B (dual-slot) design: two complete root filesystems, one active, one being written
- The partition layout, and why the data partition is the most important decision in it — **size it for the spool**, because how long the box survives an uplink outage is decided here, three lessons before anyone mentions it
- Build the QEMU x86-64 UEFI bench and boot it

```
/dev/sda1   ESP        vfat    ~512M   GRUB + grubenv      ← persistent, NOT redundant
/dev/sda2   rootfs.0   ext4    ~8G     bootname=A
/dev/sda3   rootfs.1   ext4    ~8G     bootname=B
/dev/sda4   data       ext4    rest    config, container storage, spool/recordings
```

**Deliverable:** a VM that boots, with both slots present and manually selectable.

#### Lesson 2 — RAUC: slots, bundles, and signatures

- `system.conf`: `[system]` compatible/bootloader, `[keyring]`, `[slot.rootfs.N]` with `device`, `type`, `bootname`
- `bootloader=grub` and why `grubenv` must live outside both rootfs slots
- The GRUB contract: `ORDER`, `<bootname>_OK`, `<bootname>_TRY`
- Bundles are **signed**; the device verifies against its keyring. Build a real CA and signing cert with `openssl`, sign a bundle, and watch an unsigned or wrong-key bundle get refused
- `rauc status`, `rauc install <bundle>`, reboot into the other slot

**Deliverable:** an update installed into the inactive slot and booted.

#### Lesson 3 — Rollback that actually works

The payoff lesson, and the one that must be *seen*, not described.

- Boot counters and the `_TRY` / `_OK` handshake
- `rauc status mark-good`, and what should mark it in a real system (a health check, not a timer)
- **Deliberately ship a broken update** — one whose service never comes up — and watch the box come back on the old slot by itself
- Pull the power mid-install and confirm the active slot is untouched

**Deliverable:** a written record of three induced failures and the observed recovery.

#### Lesson 4 — Podman, Quadlet, the three-way boundary, and the spool

- Quadlet: `.container`, `.volume`, `.network`, `.pod` files that systemd turns into services
- Unit locations: `/etc/containers/systemd/` for root, `~/.config/containers/systemd/` for rootless
- Key fields: `Image`, `Exec`, `Volume`, `PublishPort`, `AutoUpdate`
- Run the M1–7 VMS as containers: the FastAPI server, the edge agent, the web assets
- **The critical detail:** Podman's storage must be redirected to the data partition. Container images and volumes in the rootfs slot get destroyed by the next OS update, and both slots must stay identical
- Where AWS credentials live on an appliance: not in the image (both slots ship identical), but provisioned at commissioning onto the data partition — М8 Lesson 6's rule, now with teeth
- `podman-auto-update`, and why an appliance might *not* want it

##### The failure the module would otherwise ship

Do this before building anything: **run the appliance, pull the network cable for ten minutes, plug it back in, and go looking for those ten minutes of video.** They are not anywhere. `kvssink` publishes straight to AWS with nothing behind it, so an uplink blink is not a visibility problem, it is data loss — and a student who has *watched* footage disappear will build the rest of this correctly.

That is the argument for the one media-layer change this module makes. **You cannot buffer behind `kvssink`**: the pipeline has to write segments locally and hand them to something that uploads them.

```
capture ──▶ splitmuxsink ──▶ /data/spool/<camera>/<ts>.mp4
                                     │
                             uploader (separate process)
                                     │  on success: delete
                                     ▼
                                    KVS
```

- **The spool is the third thing on the data partition**, beside container storage and configuration — and it is the one with the worst failure mode. Images in a rootfs slot are destroyed by an OS update and can be pulled again; **footage cannot be pulled again**. This is the module's thesis with something irreplaceable behind it
- **Delete on acknowledgement, never on send.** The upload is not complete when the write returns; it is complete when the far side says so. Everything else in the course will repeat this shape — М11 acknowledges configuration on local commit and shows *not yet replicated* until the domain confirms
- **The spool needs a bound, and hitting it is a decision the student makes, not the disk.** When the partition fills: drop the oldest, or stop recording? Both are defensible and they are different products. Pick one, write it down, and make the appliance say which it did rather than failing silently
- **Catch-up is its own outage if you let it be.** Ten minutes of backlog from every camera arrives the instant the link returns, competing with live upload — and the live stream is the one someone is watching. Rate-limit the drain, prioritise live over backlog, and know how long full recovery takes. A recovery that saturates the uplink for an hour has turned a ten-minute fault into a seventy-minute one

> **Why this is not premature.** The spool exists here because the link can fail here. **М9 does not throw it away** — it puts an index over the same files and they become the archive. **М12 makes the upload conditional**: an on-prem recorder has nobody to upload to, and a cloud recorder *is* the destination. Same segments, three meanings.

**Deliverable:** the VMS running under systemd on the appliance, surviving reboot, publishing to KVS — and then the uplink pulled for ten minutes with **nothing lost**, plus a stated number for how long the spool can survive an outage before the policy you chose takes effect.

**Sidebar (context, not taught):** RAUC is not the only atomic-update approach, and for a product shipping on x86-64 UEFI it may not be the best one — **bootc** ships the OS itself as an OCI image, through the same registry and signing chain as the containers above. RAUC is kept here because A/B slots are legible, its signature verification is unconditional, and its bootloader coverage means these lessons port to ARM. Alternatives compared in [`rauc-alternatives.md`](rauc-alternatives.md).

**Second sidebar (setting up М11 Lesson 1):** one container per camera is correct at this scale and stops being correct somewhere near fifty. Say so here rather than letting students generalise the pattern silently; М11 Lesson 1 breaks it deliberately. Reasoning in [`worker-and-process-model.md`](worker-and-process-model.md).

---

### Verification plan — and an honest limitation

Modules 1–15 held to a rule: every step shows a real, observed result, and nothing ships unverified. **Module 9 cannot fully meet that bar in the authoring sandbox.** `qemu`, `podman` and `rauc` are all absent, package downloads are blocked (403), and there is no `/dev/kvm`.

**Track 1 — verified here.** Everything that is a file, a schema, a signature, or logic:

- The **RAUC signing chain** with real `openssl` — build a CA, sign, verify, and prove a wrong-key bundle fails. *Already demonstrated: correct bundle accepted, rogue-CA bundle rejected, tampered bundle rejected.*
- `system.conf` and bundle manifest structure, parsed and checked
- Quadlet unit files checked with **`podman-system-generator --dryrun`**, not `systemd-analyze verify` — *corrected while writing Lesson 4*: `systemd-analyze` does not know the `[Container]` section and reports `Unknown section 'Container'. Ignoring.` while ignoring the whole file. Verified against systemd 255. The generator needs Podman, so this is Track 2, not Track 1
- Partition arithmetic and any shell logic
- **The spool, in full.** Segments on disk, an uploader, delete-on-acknowledgement, the bound and its policy, and the rate-limited drain — all of it is files and a queue, testable against a fake uploader that can be told to fail. The ten-minute-outage deliverable runs here with the network fault simulated rather than a cable pulled

**Track 2 — verified on real hardware, by you or a student.** Booting either slot, an actual rollback, and RAUC installing a bundle end to end. Each lesson carries an explicit *expected output* block so a deviation is recognizable rather than mysterious.

Every lesson will mark which claims are run-here versus documentation-derived.

---

### Open questions

1. **Lesson numbering.** This assumes М9 continues at 16, i.e. М8 doesn't add numbered lessons.
2. **Commissioning.** Does the appliance need a first-boot setup flow (network, credentials, stream name)? Adds roughly a lesson.
3. **Hardware acceleration.** GPU/codec passthrough into containers — in scope here, or deferred to М9 where non-containerised task drivers become available?
4. **Is four lessons the right size?** RAUC, rollback and the three-way boundary are a focused subject and the module is tight. Commissioning (question 2) is the obvious candidate if it should be five.

---

---

## Part 2 — The recorder: Lessons 5–9

**One recorder learns what it should be, and closes the gap itself.** *(Lessons 5–9 — written as its own design record on 4 September 2026 and folded in here when the recorder's five lessons joined the appliance's four.)*

М8 built a VMS with no database — Kinesis held the configuration and the archive both. М9 made the box atomic and replaceable, but a box still only knows what was flashed onto it. This module is where a recorder learns **what it should be**: five lessons in which `INSERT INTO cameras` causes a camera to start recording, `DELETE` causes it to stop, and nothing sits in between but a loop the student wrote.

**What this module builds is a recorder**, and the capital letter matters from М11 onward. A recorder is not a server: it is a VMS instance that owns its own database, its own cameras and its own archive — and in М11 it becomes a scheduler allocation that moves between servers, carrying its cameras with it. Everything built here travels intact.

> **Scope note.** This half of the module has been assembled three times, and the last move is the one worth knowing. The course plan first had М9 as Postgres alone with the loop deferred to М11; the loop came back, because a database nothing acts on is not a working system. Then М9's multi-node half landed here — it is desired-state work, and М9 is supposed to be one box. It did not stay: a Nomad cluster and the layer above it turned out to be one arc cut in the wrong place, so scheduling went on to [М11](../М11_ClusterVMS/module-design.md). What is left is one recorder, which is what the name promises — and М11 takes that same recorder, runs several of them, and moves them between servers without changing anything built here.

---

### The thesis

Every layer so far has had a single source of truth that lived somewhere else. Now the box owns it, and owning truth means being able to answer one question:

| | Holds | Written by | Survives |
|---|---|---|---|
| **Desired state** | What the operator asked for | The operator, through the API | Reboots, OS updates, the worker dying |
| **Actual state** | What is running right now | The worker, by observation | Nothing — it is re-derived every time |

> **The rule that organises the whole module: desired state is persisted, actual state is derived.**

A student who persists actual state has built a cache that goes stale and lies. A student who forgets to persist desired state has built something that forgets its cameras on reboot. Both mistakes are worth making once, deliberately, in Lesson 6.

The convergence test is one comparison, and it is the same one at every layer above this: **applied means `observed_revision >= revision`.**

#### The pattern is not exotic

What the student builds here is what a scheduler already is: desired state in one place, actual state observed, and a loop closing the gap. They write it by hand first, at a scale where both ends fit in one terminal.

М11 then hands them a production implementation of the same idea — a Nomad jobspec *is* desired state and its scheduler *is* the loop — and puts the two side by side. Meeting a scheduler after having written one is what stops it being magic, and it is why М11 can argue for two reconcilers at two levels without that sounding like an abstraction.

---

### The demo the module is built backwards from

```sql
INSERT INTO cameras (name, rtsp_url, site_id, enabled)
VALUES ('front-door', 'rtsp://10.0.0.41/stream1', 'store-14', true);
```

Within a few seconds, without anyone restarting anything: a pipeline is running, segments are landing on the data partition, and `SELECT name, phase, observed_revision FROM camera_status` says so. `UPDATE ... SET enabled = false` stops it. `systemctl kill worker` loses nothing but the open segment, and the box converges again on restart.

If a lesson does not move that demo forward, it does not belong in this module.

---

### Decisions taken

| Decision | Choice | Why |
|---|---|---|
| Scope | **One recorder, one server, end to end** | The loop is the lesson, and it is far easier to see when both ends are in one terminal. Scheduling recorders across servers is М11's. |
| Worker model | **N pipelines in one Python process** | The conclusion of [`worker-and-process-model.md`](worker-and-process-model.md), now built. Container-per-camera is М9's world and stops being right near fifty. |
| Actual state | **Derived, never persisted** | Kill the worker and it must rebuild its picture from Postgres plus observation. Anything it remembers across a restart is a bug. |
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

### Prerequisites

- **М8 Lesson 2** — process supervision, signals, and the self-matching `pkill` bug. The worker is what `looper.py` grows into.
- **М8 Lesson 4** — GStreamer pipelines and what each element does. Lesson 7 builds them from Python instead of a shell string.
- **М9 Lesson 4** — Quadlet, and the OS/app/data boundary that decides where `PGDATA` goes.
- **М8 Lesson 6** — configuration is read from the environment; credentials are never in the image.

New assumed knowledge: SQL at the level of `CREATE TABLE`, `JOIN` and `INSERT`. No prior Postgres administration.

---

### How the shard is actually organised in Python

The question this module has to answer honestly, because the intuition is that Python cannot do this and the intuition is wrong for a specific and teachable reason.

#### Where the work happens

Once `pipeline.set_state(Gst.State.PLAYING)` returns, buffers move on **GStreamer's own native threads**, inside libgstreamer, in C. Python is not in that path. And PyGObject documents that "all PyGObject calls release the GIL during their execution and other Python threads can be executed during that time."

So a worker holding fifty recording pipelines is running fifty pipelines' worth of C and a trickle of Python: a bus message every few seconds, a state change when configuration changes, a status write every five seconds. The GIL is close to uninvolved.

#### The seam where it goes wrong

PyGObject also documents that "signals get executed in the context they are emitted from." A callback attached to a signal or a pad probe therefore runs **in the streaming thread**, and to run Python there it must take the GIL.

Attach a `GST_PAD_PROBE_TYPE_BUFFER` probe to fifty cameras at 25 fps and that is **1,250 GIL acquisitions per second**, serialised through one lock, each one interpreting Python. That is how a Python media worker dies, and it has nothing to do with how many pipelines are in the process.

> **The rule: Python touches control, never data.**
>
> Banned in the recording path: `appsink`, `identity handoff`, buffer-level pad probes.
> Fine: bus messages, state changes, `splitmuxsink::format-location` (once per segment).

#### Stall detection without touching a buffer

The obvious way to notice a camera that has stopped sending while its TCP socket stays open is to timestamp every buffer — which is exactly the banned thing. GStreamer already solves it in C: the **`watchdog`** element from `gst-plugins-bad` passes buffers through untouched and posts an **error on the bus** if none arrive within `timeout` milliseconds (default 1000; a few seconds is right for cameras).

```
rtspsrc ! rtph264depay ! h264parse ! watchdog timeout=8000 ! splitmuxsink
```

Zero Python in the data path, and the failure arrives on the bus the worker is already reading. This one element is worth a section of Lesson 7 on its own, because it is the model for the whole design: push the per-frame concern into C, keep Python at control rate.

#### The event loop, and not having two of them

The worker speaks asyncio to Postgres and to its API. Running a `GLib.MainLoop` alongside it gives the process two schedulers and two notions of "later".

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

#### What it costs

Each recording pipeline creates roughly three to five native threads, so fifty cameras is 150–250 threads in the process. Linux is fine with that, but it is a number to measure rather than assume — `reference/shard-memory-probe.py` from М9 is extended in Lesson 7 to report it alongside PSS.

And the honest cost: **one segfault takes the whole shard.** That is the price of sharding, paid in exchange for the per-process baseline. It is bounded by shard size, by systemd restarting the unit, and by `splitmuxsink` — a crash loses the open segment and nothing else.

#### The rule is not really about the GIL

Worth stating explicitly in the lesson, because it is the part that survives a change of language. The constraint is not Python's lock. It is **crossing a language boundary once per frame.**

| | What a per-buffer callback costs | Verdict |
|---|---|---|
| **Python** | Acquire the GIL and interpret. Fifty cameras at 25 fps is 1,250 acquisitions per second through one lock | Fatal |
| **Go** | Enter the Go runtime from a C thread through cgo. Cheaper than Python, still real, and the rules on passing pointers make it awkward | Same discipline required |
| **C++** | Nothing. There is no boundary | The rule dissolves |

That last row is the real argument for C++ in the media worker, and it is a better one than "C++ is faster" — which, for a pipeline that never decodes, would barely be true.

---

### What the operator never decides

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

### Lessons

*Five lessons, one recorder on one server. The student writes the reconciler.*

All five are written: see [`README.md`](README.md) for the index and what can be verified without hardware.

#### Lesson 5 — The database the cloud VMS didn't need

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

#### Lesson 6 — A reconcile loop with nothing in it

The `camera_sim.py` move, applied to control: build the loop before the thing it controls.

- Read desired from Postgres, compare against an in-memory dict of actual, log the difference. The actuator is a `print()`
- The vocabulary: desired, actual, converged, lagging, stalled — and `observed_revision >= revision` as the only test of "applied"
- **Poll versus `LISTEN/NOTIFY`.** Notify makes it fast; the timer makes it correct. A disconnected listener misses notifications permanently, so a system with only NOTIFY stops converging and does not say so
- asyncio structure: one task per *concern*, not one task per camera
- **Deliberate mistake, then fix:** persist actual state, restart the process, and watch it confidently report pipelines that are not running

**Deliverable:** a worker that converges a fake world, and passes a test that kills it mid-change.

---

#### Lesson 7 — Fifty pipelines in one process

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

#### Lesson 8 — Failure is the feature

Each failure mode reproduced on purpose, then handled.

- **Camera offline** → exponential backoff **with jitter**. Two hundred cameras reconnecting in lockstep after a switch reboot is a self-inflicted outage, and the jitter is the whole fix
- **Stalled stream, socket still open** → `watchdog` fires, that one pipeline restarts, the other forty-nine never notice
- **Disk full** → retention enforcement degrades by policy, and this is М9's spool-bound question returning with the answer changed: there, a full disk meant choosing between dropping the oldest and stopping recording, because the footage was in transit. Here it is *the archive*, so retention decides and the choice is the customer's, written down. The deletion loop must be conservative: never delete what it cannot prove is superseded. With Lesson 5's partitioning this is a partition detach-and-drop plus a segment unlink, not a scan — which is what makes it fast enough to run under pressure. Order matters: drop the index rows *before* unlinking, so a crash leaves orphaned files rather than index rows pointing at nothing
- **The worker dies** → systemd restarts it, state is re-derived, and the segment discipline bounds the loss
- **The fencing rule, introduced small:** on restart, never resume the previous segment — open a new one. Leases and epochs are М11's problem; the rule that makes them necessary lands here

**Deliverable:** a test suite that kills, fills, stalls and unplugs, and asserts convergence after each.

---

#### Lesson 9 — What the console shows, and what Python stops being right for

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

### Verification plan

Better than М9's, because almost nothing here needs hardware.

**Track 1 — verified in the authoring sandbox, and now actually run.** Postgres 16.13 and plain Python produced every figure printed in Lessons 1, 2 and 4 — the retention timings, the pruning plans, and the seven reconciler tests including the 200-camera jitter spread. The schema, migrations, the reconcile loop and the state machine are all ordinary software. The loop is tested against a fake actuator exactly as М8 Lessons 5–8 tested against fake AWS objects, which means convergence, backoff, restart and the deliberate mistakes are all provable here.

**Track 2 — needs a real bench.** Anything with GStreamer in it: the pipeline strings, the GIL demonstration, the `watchdog` timing, and the memory and thread measurements. The authoring sandbox has no GStreamer and the package mirrors are blocked, so Lesson 7's numbers come from the student's box, produced by a script that ships with the module rather than from figures asserted in the text.

Every lesson marks which of its claims were run and which are documentation-derived.

---

### Open questions

1. **Does the API belong here or in М11?** Lesson 9 builds a read view. A write API with authentication is arguably М11's and arguably М12's; М11 currently builds it.
2. **How much retention policy is domain design rather than infrastructure?** The deletion loop is М9; schedules, per-camera overrides and legal-hold are product decisions that may deserve their own lesson in М11.
3. **Postgres in a container or on the host?** The module currently says Quadlet unit. On an appliance that nobody administers, a host package with systemd is a defensible alternative and the tradeoff is worth teaching either way. Note this now carries both databases, so the answer applies to the archive index and the event stream too.
4. **Does Lesson 8 need a real camera that misbehaves?** Cheap cameras stall in ways a simulator does not reproduce faithfully, and the module's most valuable failure mode is the hardest to fake.
5. **Does the shard-memory probe belong here or in М11?** It ships with this module and Lesson 7 runs it, but М11 Lesson 1 runs it again to derive a shard size. Duplicated use, single home — worth confirming that is the right call.

---

## Appendix — Porting to ARM and other platforms

The module targets x86-64 UEFI, but the stack is portable. Of the nine lessons, **only Lesson 2 is platform-bound.**

#### Layer by layer

| Layer | Portable? | What changes |
|---|---|---|
| **RAUC** | Yes — ARM is its native territory | The bootloader backend and its tooling |
| **Podman / Quadlet** | Yes, arch-agnostic | Images must be built multi-arch |
| **Nomad** | Yes — single Go binary, linux arm64 builds published | Nothing structural |

#### The bootloader is the only real swap

RAUC supports `barebox`, `u-boot`, `grub`, `efi` and `custom` backends, selected by one key in `system.conf`.

| Platform | Backend | Boot-state tool |
|---|---|---|
| x86-64 or ARM server, UEFI | `grub` / `efi` | `grub-editenv` / `efibootmgr` |
| ARM SBC (most) | `u-boot` | `fw_setenv` / `fw_printenv` |
| ARM SBC (barebox) | `barebox` | `barebox-state` |

What differs when porting: the boot mechanism, where boot state is stored, partition naming (stable paths versus raw device names), and kernel command-line handling.

What does **not** differ: slots, `bootname`, the `ORDER` / `_OK` / `_TRY` handshake, `mark-good`, atomic install, and rollback semantics. Lessons 1, 3 and 4 port unchanged.

#### Multi-arch images

Every image needs an arm64 build. The awkward one is **`kvssink`** — a compiled C++ SDK, not a pip install.

- AWS documents a native Raspberry Pi build, so ARM is genuinely supported
- Cross-compiling to aarch64 has known friction (open issue in the SDK repo)
- Plan on **native ARM builders**; `qemu-user` emulation works but is slow for a build this size
- Build per architecture, publish one multi-arch manifest

#### Hardware video acceleration — the cost that actually bites

This does not port. Each vendor has its own stack:

| Platform | GStreamer plugin | Notes |
|---|---|---|
| Intel / general GPU | `va` (gst-plugins-bad) — `vah264dec`, `vapostproc` | Supersedes the older `vaapi` plugin |
| NVIDIA | `nvcodec` (gst-plugins-bad) | NVDEC/NVENC, Fermi and newer |
| ARM SoCs | `v4l2` (gst-plugins-good) | Kernel API exposing the SoC's codecs |
| AMD | `amfcodec` | |
| Apple | `applemedia` | |

**The mitigation worth teaching:** GStreamer selects decoders by *rank*, and `GST_PLUGIN_FEATURE_RANK` re-ranks them at runtime. A portable appliance can ship **one pipeline description plus one environment variable per SoC**, instead of per-platform pipeline code.

**Why this doesn't bite the course yet:** the pipeline is pure pass-through — `filesrc ! qtdemux ! h264parse ! splitmuxsink`, mux only, no decode and no encode. Add real cameras with transcoding, or analytics needing decoded frames, and this becomes the dominant porting cost — larger than RAUC by a wide margin.

#### Storage cautions on ARM

- For a VMS, continuous recording destroys SD and eMMC flash. Attach real storage
- Nomad's raft consensus is also write-sensitive; keep server state off flash
- **Avoid 32-bit ARM.** A 32-bit address space and video buffers are a bad pairing. arm64 only

#### Suggested treatment

Keep x86-64 / UEFI as the taught target. Ship this appendix as student-facing reading after Lesson 2, with one exercise: *name the three things that change if this appliance ships on an ARM SoC instead, and the three that don't.*

---

---

## Sources

- [RAUC integration](https://rauc.readthedocs.io/en/latest/integration.html) — bootloader backends, slot config, GRUB/EFI, commands
- [Podman Quadlet (`podman-systemd.unit`)](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html) — unit types, paths, fields, auto-update
- [GStreamer hardware-accelerated decoding](https://gstreamer.freedesktop.org/documentation/tutorials/playback/hardware-accelerated-video-decoding.html) — `va`/`nvcodec`/`v4l2` plugins, rank-based selection
- [KVS producer SDK on Raspberry Pi](https://github.com/awslabs/amazon-kinesis-video-streams-producer-sdk-cpp/blob/master/docs/raspberry-pi.md) · [aarch64 cross-compile issue](https://github.com/awslabs/amazon-kinesis-video-streams-producer-sdk-cpp/issues/827)
- [PyGObject — Threads & Concurrency](https://pygobject.gnome.org/guide/threading.html) — "all PyGObject calls release the GIL during their execution"; "signals get executed in the context they are emitted from"
- [GStreamer `watchdog` element](https://gstreamer.freedesktop.org/documentation/debugutilsbad/watchdog.html) — `timeout` in ms, default 1000, posts an error to the bus when no buffers arrive
- [`gstwatchdog.c`](https://github.com/GStreamer/gst-plugins-bad/blob/master/gst/debugutils/gstwatchdog.c) — the implementation, for the lesson that reads it
- [GStreamer pipeline manipulation](https://gstreamer.freedesktop.org/documentation/application-development/advanced/pipeline-manipulation.html?gi-language=python) — probes and their thread context
- [GStreamer bindings](https://gstreamer.freedesktop.org/bindings/) — which bindings are officially maintained
- [`go-gst`](https://github.com/go-gst/go-gst) — the live Go binding, successor to `tinyzimmer/go-gst`
- [`gstreamermm`](https://github.com/GNOME/gstreamermm) — archived, which is why C++ uses the C API directly
- [`worker-and-process-model.md`](worker-and-process-model.md) — the process model this module implements

*Written 2 and 4 September 2026 as two records; merged 14 September 2026.*
