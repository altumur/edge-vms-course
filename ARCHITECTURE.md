# The Architecture, and How It Got This Shape

**A single account of the system the course ends with, and of every decision that produced it — in the order they were taken, with the reversals left in.**

This is the record to read when a module design says *"as decided earlier"* and you want to know where, and why, and what it replaced. Part 1 describes the system as it stands. Part 2 walks the decisions in sequence, because the sequence is most of the lesson: almost every component in Part 1 exists in its current form because an earlier version of it was built, found wanting, and taken out.

The short version, for orientation:

> **A box records on its own. A cluster owns its own truth. A cluster survives any server in it. A domain is the top of the product and works with everything above it gone. The vendor is a counterparty across a one-way boundary, not a layer.**

Everything below is the long version of that sentence.

---

## Part 1 — The system as it stands

### 1.1 Four words, kept apart — and one retired

The design uses four nouns precisely, and most of its early mistakes were the result of two of them being confused. A fifth, *Node*, was retired on 12 September 2026 when М10 dissolved the process it named.

| Word         | What it is                                                                                                                                                                                                                                                                                                                                                    | Its boundary is set by | Who names it                                          |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------- | ----------------------------------------------------- |
| **Server**   | a box with CPUs and disks — and everything on it: a replica of the cluster's stores, the platform's *resource* job on its disks, the *workers* the scheduler placed there (М10 ServerVMS)                                                                                                                                                                     | purchase               | nobody — the scheduler uses it                        |
| ~~**Node**~~ | *retired.* М9's recorder — a process with its own Postgres, cameras and archive index, distinct from the box. Its work is a *worker* (moves) and a *resource* (stays); their sum on a box is the *server*. Kept in М9 as history; М12 was rewritten to 2c on 12 September 2026 and its decision record keeps the word only in its preserved earlier revisions | —                      | —                                                     |
| **Cluster**  | servers close enough to share a network you would bet recording on — one LAN, usually one server room                                                                                                                                                                                                                                                         | **physics**            | an installer                                          |
| **Domain**   | the clusters under one directory, one signer and one set of operators — one customer installation                                                                                                                                                                                                                                                             | **administration**     | the customer                                          |
| **Site**     | where cameras physically are                                                                                                                                                                                                                                                                                                                                  | the building           | **the operator — the only one of the five they name** |

Two relationships carry most of the weight:

- **A worker is not a server.** A server dies; the worker moves to another server *in the same cluster*, reads its assignment from the cluster's raft and takes a new epoch for each camera. Nothing is reassigned, because nothing was ever assigned to a server; the footage stays on the dead server's resource, unavailable rather than lost.
- **A cluster is not a domain, and a domain is bigger.** A campus is one domain, three clusters, three sites. A cloud deployment is one domain, one cluster, fifty sites. Sites and clusters are many-to-many on purpose.

And one rule that both of those rest on:

> **A worker fails over within its cluster and never across one.** Its footage is on that cluster's disks, and its fencing epoch comes from that cluster's raft. A whole cluster dying is not a failover; it is a larger event the domain reports honestly and does not try to heal.

### 1.2 The layers, bottom up

| Layer           | Built in          | What it knows                                     | Where truth lives                                           | What can disagree                                                                             |
| --------------- | ----------------- | ------------------------------------------------- | ----------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| **The box**     | М9 EdgeVMS        | what it *is*                                      | the image that booted                                       | nothing — a box is whatever was flashed onto it                                               |
| **The server**  | М10 ServerVMS     | what it *should be*                               | the platform's stores on the box: Variables and objects, one controller writing placement | desired state and actual state — a row and a worker's heartbeat                              |
| **The cluster** | М11 ClusterVMS    | what it should be, *on whichever server survived* | the cluster's raft, unchanged when a server dies            | **two instances of the same worker**                                                          |
| **The domain**  | М12 DomainVMS     | what it should be, *and which cluster holds it*   | each cluster's controller, with a directory across clusters | clusters, with the directory — and the directory with itself, because it cannot be consistent |
| **Seeing it**   | М13 Observability | whether any of the above is true right now        | —                                                           | *broken* versus *unreachable*                                                                 |
| **The vendor**  | М14 VendorVMS     | *not a layer*                                     | nowhere the product depends on                              | the customer, with the vendor                                                                 |

Every boundary between the first four is a network you stopped trusting — except the cluster/domain one, which is set by administration and spans machines that may well share a rack. The last boundary is not a network at all; it is an organisation.

### 1.3 The rule that runs through all of it

> **Every layer is allowed to be unavailable to the layer beneath it, and the layer beneath caches what it needs to carry on.**

Concretely: a camera keeps recording when its controller is down; a worker keeps recording when its cluster's directory is down, and the cluster keeps being edited when the domain is unreachable; a cluster fails over with the domain unreachable; a domain runs for thirty days — recording, renewing certificates, logging operators in, failing servers over — with the vendor gone.

The rule has one sharp edge, and it is the thing most worth carrying away:

> **Anything cached from above may keep recording forever, and must never delete anything.** Destructive operations expire; recording does not.

A cluster owns its own retention policy, so it cannot go stale on that. Entitlement and placement come from above, and those can — so a cluster that cannot confirm its entitlement keeps every camera it has and refuses to add one, and a resource that cannot confirm its retention keeps footage and reports that it is doing so. Disks filling is a visible, recoverable problem. Deleted footage is neither.

### 1.4 What runs where

**On every box** (М9): an A/B root filesystem under RAUC, signed bundles, one-attempt rollback decided by a health check that reaches all the way to *is footage being written*; Podman under Quadlet; and a data partition holding everything that must outlive both an OS update and an application update — container storage, configuration, and the archive.

**Per server** (М10): the platform's stores (Variables and objects — on one box, files on the data partition; in a cluster, the cluster's raft and object store), the archive **resource** on its disks with its manifests and event buckets, and the **workers** placed there — each running the reconcile loop and up to ~50 GStreamer pipelines in one process (in the product, DriverPack, see §1.11), each heartbeating its status into an object; a console exporting `/metrics`. No database: М9's Postgres was retired in М10.

**Per cluster** (М11): Nomad servers and clients — the cluster *is* a Nomad region; an object store on the cluster's own servers holding every worker's heartbeat and the controller's snapshot; the cluster directory, which is nothing more than each worker's assignment Variable, scanned; and, per subsystem, the three processes of §1.12 — workers (`count = N`), one controller, a console (`count = 2`) — plus the platform's resource job on every server.

**Per domain** (М12) — five services, hosted by one designated cluster — the **domain cluster**, Nomad choosing the server, no controller and no state that is not backed up beyond that cluster:

| Service | Kind | When it is down |
|---|---|---|
| **The signer** — CA and token issuer, one job, two keys | holds keys, signs | certificate renewal stops, bounded by lifetime; nobody *new* logs in; break-glass |
| **Placement** (which cluster) | stateless computation | new cameras get no cluster |
| **The read view** | stateless, federated reads | the console sees only its own cluster |
| **The update server** (hawkBit) | pull-based delivery | no new OS bundle arrives |
| **The remote observer** | stateless, scrapes the other clusters | nobody is told a cluster went silent |

plus the **registrar**, the door a box knocks on to join the domain. Every outage in that column is bounded, and none of it is recording or recovery.

**At the vendor** (М14): the MASA that vouches for its own hardware; the licence system — a customer database, one signing key, and a signed document naming a domain id that the domain verifies offline, pulled through the domain's own update server and counted only at admission; the bundle signing key and the publishing pipeline; a support view of whatever inventory customers chose to report; and, optionally, a hosting business that rents clusters — the one place OpenBao appears, for a multi-tenant vendor holding many customers' secrets.

### 1.5 The stores, chosen by shape

There is no database in the design — М9's per-box Postgres was the last one, and М10 retired it. Everything is the scheduler's store, an object store, or files on a resource, and the rule for which is which turned out to be simple once found:

> **Small and consistent goes in the scheduler's store. Large and opaque goes in an object store. Bulk stays on the server's disks as a resource, and a question over it is a manifest, not a table.**

| Store               | Scope              | Holds                                                                             | Why not one of the others                                                                                                              |
| ------------------- | ------------------ | --------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| **Nomad Variables** | per cluster (raft) | the operator's rows (`vms/cameras/*`), each worker's assignment and placement, slots, epochs, the signer's keys | raft is memory-resident and replicated to every server, so it must stay small; it is also the only store that offers **check-and-set** |
| **Object store**    | per cluster        | every worker's heartbeat; the controller's snapshot — the only thing that leaves the cluster | a blob nobody but its author parses; durability and a timestamp are the whole requirement                                   |
| **The resource**    | per server (disks) | segments, the manifest beside them, event buckets — mirrored to a peer resource   | bulk; *what footage covers this window* is the manifest, rebuilt from the files alone; it never moves because it cannot               |

And one thing deliberately in no store: the domain root's backup, kept somewhere the domain cluster's death cannot reach.

**The domain has no database.** Its directory is a federated read across each cluster's Variables. This was the sixth revision of that decision, and every revision moved in the same direction.

### 1.6 The flows: one shape, three data types

Everything that crosses a boundary in this design does so the same way:

> **A one-way publication, with a stated recovery point.**

| What              | From               | To                               | The RPO                                                             |
| ----------------- | ------------------ | -------------------------------- | ------------------------------------------------------------------- |
| **Footage**       | a worker's spool   | the resource on its server, or a cloud archive (М8) | one segment — the open one, on a kill                            |
| **Configuration** | a cluster's controller | its snapshot in the object store, read by the domain | the publication interval — the age the domain shows on every row |
| **Status**        | every worker       | its heartbeat object; the console's read model | the heartbeat interval                                               |

The acknowledgement rule is the same everywhere: **acknowledge only what is committed where it is safe, delete or promote only on acknowledgement from the far side, and show the age of every copy.** Inside a cluster that means after the CAS commit into raft — there is no *saved · not yet replicated*, the write is in raft or it was refused; across the uplink it means the snapshot's `ts`, printed on every row the domain shows. Never acknowledge what you cannot vouch for; never block the write on it either.

Two things that look like flows and are not: **rights** — a cluster holds its own grants and enforces them at its console and gateway, with an expiry that bounds the revocation window, carried in by the domain agent; and **identity** — a cluster holds the signer's public key and verifies a token offline, holding nobody's password. A worker never learns a user exists.

### 1.7 Correctness: fencing, epochs, and CAS

The one place a mistake corrupts customer footage rather than stopping a service is a server death: the old instance of a worker may not be dead, only paused, and when it wakes it will try to keep writing to the same archive the replacement is now writing to. Two writers to one video stream cannot be merged, and nothing above can arbitrate after the fact.

The design's answer, from Kleppmann: a lock cannot stop a paused client writing, so **the resource must reject the stale token**. Every worker instance carries an **epoch** per camera — a monotonic integer issued by check-and-set against a Nomad Variable, whose `ModifyIndex` is raft-assigned and cannot go backwards — and the epoch is **part of the archive path**. The stale instance cannot name the files it would otherwise corrupt; its writes land where nobody reads.

The same principle, one level up, in a cheaper form: **at the domain, correctness comes from how a write is made, never from how many instances Nomad promises.** `count = 1` is not exactly-one during a reschedule; placement is safe against two instances because it writes with CAS and the second gets a 409, not because there is one of it.

What is **not** used for fencing, deliberately: Nomad's variable locks (an opaque UUID, not a monotonic token); a Postgres sequence (reissues numbers after a restore); shared block storage (Nomad cannot detach a CSI volume from a dead client, so 2a needs a human before it fails over).

### 1.8 Trust: who is what, and who says so

| Subject                       | Proves itself with                                   | Issued by                                          | Verified by                                   | Lifetime                                         |
| ----------------------------- | ---------------------------------------------------- | -------------------------------------------------- | --------------------------------------------- | ------------------------------------------------ |
| **A box joining**             | a factory IDevID, or an administrator's approval     | the manufacturer / the domain's console            | the domain's registrar                        | once                                             |
| **A worker, on every stream** | an LDevID — mTLS, naming the *worker* not the server | the domain signer                                  | every peer, offline                           | hours to days                                    |
| **A person**                  | a signed token naming a subject                      | the domain signer, federated to the customer's IdP | every cluster's console, offline, against a public key | short; the grant it points at has its own expiry |
| **The domain itself**         | its self-signed root                                 | itself — **there is nothing above**                | —                                             | years; rotated on a drill                        |
| **The vendor's hardware**     | a MASA voucher                                       | the vendor                                         | the registrar                                 | once, at enrollment                              |

Two properties of that table are load-bearing.

**The domain's root is the customer's, and nothing sits above it.** A vendor-held root that signs the customer's CA is a vendor who can impersonate the customer's whole trust domain. So recoverability comes from backup and rotation, not delegation, and losing the root means every box re-enrolls — the honest cost of the customer owning their own trust.

**Delegate an authority; never distribute a secret.** N clusters holding password hashes is N places to steal from; N clusters holding a public key is zero. Most of the secrets earlier drafts of the course wanted a vault for turned out to exist because something had not been given an identity.

### 1.9 The failure matrix

| What dies | Recording | Editing at a cluster | Failover | Creating a camera | Logging in | What the console says |
|---|---|---|---|---|---|---|
| A **camera** | that camera stops | — | — | — | — | `camera_silent_seconds` rises; the phase says why |
| A **server** | ~one segment per worker on it | continues | **yes, within the cluster** | continues | continues | one alert, naming the server |
| The **cluster's directory** (Nomad servers) | continues | continues | **no** — no epoch, no Variable | not in that cluster | continues | *unreachable*, not broken |
| A **whole cluster** | those cameras stop | — | no — nothing to fail over to | not there | continues elsewhere | *unreachable*; footage *unavailable*, not lost; **never rebalanced elsewhere** |
| The **domain services** | continues | continues | **yes** — both dependencies are in the cluster | no | existing tokens to expiry; break-glass | the local cluster only |
| The **vendor** | continues | continues | continues | within the entitlement's grace period | continues | no new bundle; day 31 the entitlement floor |

The column to read is *Recording*. Only a camera or a whole cluster stops it, and both are physical.

### 1.10 Deployment shapes

The same software, three placements, and the difference is a number:

```
50 cameras × 4 Mbit/s  =  200 Mbit/s sustained upstream, 24/7  ≈  2 TB/day
```

| | workers run | What crosses the uplink | Right for |
|---|---|---|---|
| **Edge** | on hardware at the site | kilobytes of status and configuration | any site with more than a handful of cameras |
| **Cloud** | on a cluster the domain rented from the customer's cloud account | **every camera's full bitrate**, continuously — and there is no spool, so the camera's own SD card is the buffer | a shop with six cameras and nobody to install hardware |
| **Mixed** | at the site, with the domain services in the cloud | the same kilobytes | **the default shape** |

A rented cluster is a cluster. A worker cannot tell where it is running, and М12 Lesson 8 proves it by diffing the artifacts.

---

### 1.11 Platform and VMS: the boundary

Most of what Part 1 describes knows nothing about a camera. A scheduler that places processes by constraint, an object store, a small consistent config store, a signer and an agent that carry trust, a web gateway, an observer — that is a **platform**, and it would host any fleet of stateless shards writing bulk data. The **VMS** is what is specific to video: the worker that holds the pipeline (DriverPack), the detectors, the schema of cameras, sites and grants, and the UI. The boundary decides which team owns what and which invariants travel across it.

М9's **worker process was a stand-in for the worker's own controller.** It exists because the course had no DriverPack and needed something to supervise pipelines, reconcile them against desired state and report positions — in Python, so the design could be built and tested. It is not the platform's job (Nomad supervises processes, not the threads inside one; nobody but the VMS turns *camera 7, revision 12* into a running pipeline) and it is not a separate process in the product: the thing that holds the source owns everything that happens to the stream, including where its branches go. Row by row:

| М9's worker | Product | Why |
|---|---|---|
| start/stop/restart pipelines; backoff with jitter; bus pumping; watchdog | **DriverPack** | only the holder of the source knows a pipeline died |
| reconcile desired cameras against running pipelines; `>=` on the revision | **DriverPack** | *make the running set equal the assigned set* is the worker's loop |
| routing — record to storage, tee to live, tee to detectors | **DriverPack** | it directs the stream where needed |
| positions and conditions per camera; the heartbeat snapshot | **DriverPack** | the only thing that knows the phase; it publishes an object, the platform stores it |
| retention, disk-full policy, the segment index | **platform** (storage) | lifecycle, quota, listing — nothing about video in it |
| identity of a shard; config restore; publish-then-point | **platform** (Variables + objects) | generic config distribution for any stateless shard |
| epoch and lease | **platform** issues; **DriverPack** consumes | fencing is generic to any writer that can have two instances; the worker puts the epoch in the key and refuses a start without a live lease |
| the console; playback file serving | **platform** (web tier) | browsers were moved off the worker in М12 Lesson 3; playback reads the resource |
| the data model — cameras, sites, grants | **VMS**, as a schema in platform stores | the platform stores blobs and Variables; what is in them is ours |

What remains of the "controller" is a few dozen lines inside DriverPack that turn *the platform's assignment for this shard* — a Variable naming a config object — into DriverPack's desired set. Not a process, not a layer.

**What crosses the boundary unchanged is the contract, and it is the point of М9 and М11.** DriverPack's controller must satisfy what the course's tests define, because those tests were written against a design rather than a language: desired is persisted and actual is derived (a fresh process rediscovers everything and persists nothing about what it runs); a report can never move desired (`>=`); exponential backoff with jitter, so cameras that failed together do not retry together; positions kept apart from reasons in what it reports; the epoch in every key it writes and a lease gate on every start; local commit first, publish second; the heartbeat carrying its status so nobody has to call it. `recorder/`, `vmsserver/`, `clustervms/` and their Go ports are the **reference implementation of that contract** — the thing DriverPack's tests are ported from, the way the Go port's were — and no longer a claim about what the product's process tree looks like.

Two things the boundary must keep explicit. **Crash isolation:** М9 separated controller from worker partly so that a vendor SDK's segfault would not take the control loop with it; with both in DriverPack it does, and that is acceptable *only because* the state is outside — Nomad restarts the shard, it reloads its assignment, and the lease and epoch make the restart harmless. **The per-frame rule:** DriverPack is C++, so М9 Lesson 7's rule holds by construction; a Python plugin API "for analytics" inside it would bring the argument back. The detector tier exists so that inference never runs in the writer's process.

**Decided beside it (М11, *2c*):** the cluster is **workers** (1+, by workload — DriverPack shards with stable identity, movable), **resources** (N — the archive on a server's disks, GPU compute, a camera-VLAN NIC; server-bound `system` jobs) and **one controller** (placement and rebalance; stateless, correct by CAS, safe at two, never on the recovery path). Footage stays on the dead *resource* and recording continues on another; the per-recorder Postgres goes; the epoch stays in the key. The storage resource is per-server by default, an erasure-coded pool by choice, with the failure arithmetic of each in [М11's design record](./М11_ClusterVMS/module-design.md).

### 1.12 Controller, worker, console — and two of them as data

Every subsystem on the platform is the same three processes, split by *what each may write* and *what stops when it stops* — never by what it computes:

| Process | Count | May write | Holds | When it is down |
|---|---|---|---|---|
| **worker** | N, by workload (the autoscaler's number, from `<sub>_worker_headroom`) | its heartbeat; `<sub>/epoch/<unit>` when it starts a unit; its slot | the pipelines, and nothing that outlives it | its units fail over to another worker; the rest keep recording |
| **controller** | 1 — safe at 2, never needed at 2 | `<sub>/workers/*` (assignment), `<sub>/placement/*` (why), `<sub>/slots/*`, the snapshot | nothing: every pass starts from the store | a new unit waits for placement; a dead worker's units wait for redistribution; recording, edits and the screen continue |
| **console** | one per server (a `system` job), nothing in front | `<sub>/<rows>/*`, `<sub>/next_id`, the derived rows (`vms/retention/*`), `<sub>/idem/*` | nothing: the read model is rebuilt from heartbeats on every request; a retry's key is a Variable, so any instance answers it | that server's address stops working; every other server's console is the same console; recording and placement continue |

The two writers never share a key. The console writes the operator's rows; the controller reads them and writes placement; a console that tried to place gets a 403 from the store's ACL, not from a rule in its code — `test_the_console_over_http` makes it try. `POST /cameras` therefore answers `worker: null`, and the row is placed on the controller's next pass; `DELETE` marks the row, and the pass takes the assignment back. The two counts follow from the two jobs: the controller is `count = 1` because its correctness is CAS and a second copy would merely repeat the pass — economy, not safety; the console runs on every server because a person is waiting on it and must be able to type any server's name — no load balancer, no ingress, no gateway in front; stateless copies share nothing and coordinate through the store alone (a retried `POST` is claimed under `<sub>/idem/<key>` by create-only CAS, so whichever instance gets the retry serves the first reply and never repeats the write). Neither number is a correctness claim. A web *gateway* appears only when live video does — and it is a worker of its own subsystem, not a tier: `live` (М10 Lesson 7), whose unit is a camera's fan-out and whose capacity is viewers; it subscribes to a worker's tee once per camera and fans out to browsers over WebRTC, scaled by viewer headroom, placed by label. Per-frame, per-viewer work never sits in the process an operator edits rows with, and never reaches a worker from a browser. Without live view, the console on every server is the whole web tier; with it, the console is still only the door (`/whep`) — the media goes gateway → browser.

**Both are data.** A subsystem's `<sub>.subsystem.yaml` says what a unit is (the rows' name, the id rule, the operator's fields with types and defaults, the derived rows and what happens to them on delete), how it is placed (capacity and headroom read from the heartbeat, a constraint and a tie-break by name, the rebalance dead band), what leaves the cluster (the snapshot's fields), and what the console counts as running. `SpecController` runs from that file; `SpecConsole` runs from the *same* file — `/spec` for the page, `/<rows>` for the read model, `/where`, `/metrics` under the subsystem's prefix, `/marks`, and the three writes with the spec's refusals — and the page builds its list and its forms from `/spec`, so it names no camera either. The two ACLs are derived from the same file too. What a subsystem writes, then, is the YAML, the worker — the only thing that knows what a unit *does* — and optionally a few routes registered as extras (the VMS: `/timeline` and `/segment`, where the bytes are) and a constraint registered under a name. М10's live subsystem (Lesson 7) has a controller and a console with no code for either, with a unit that is *demanded* rather than configured — a camera's fan-out, created by the first viewer, deleted by the gateway after the last — and a capacity counted in viewers, its workers being the live gateways; М10's detector subsystem (`det`, Lesson 8) is a YAML with `labels: [gpu]` and a worker whose events are its own buckets on the resource under its own epoch — and the console that fronts them all is one process: the VMS's `SpecConsole` at `/`, every other subsystem's mounted under its name (`/live/…`, `/det/…`), the operator's actions on them from the camera's page. `psimplatform/spec.py`, `psimplatform/console.py`, `vms/vms.subsystem.yaml`; the Go port the same.

What is *not* a subsystem is decided by the same three questions — a process that moves, a unit that is placed, a capacity the worker measures. The archive answers no to all three: it is a **resource**, pinned to its server by physics, registered on rather than placed, and giving it a placement row would let the controller try to move a disk (the CSI-volume failure М11 rejects). Events answer no to the first: they are a **data shape on a resource**, written by whoever holds the unit's epoch, so an "events subsystem" would be a second writer on the same unit. The console and the eventindex answer no to the second: **stateless jobs with no units**, needing no controller. Four rows, and every process on a server sits in exactly one of them.

---

## Part 2 — How it got this shape

The decisions, in the order they were taken. Each has the question, the first answer, what broke it, and what replaced it. Where an earlier version is still visible in a decision record, that is deliberate: the course leaves revisions in because the sequence is the lesson.

### Step 0 — The starting point: a VMS with no local truth (М8)

The course opens by renting a cloud VMS. Kinesis holds the configuration *and* the archive; the student's software is a client of somebody else's service. This is not a mistake — it is the cheapest way to build the product surface — but it means the box holds nothing. Everything after М8 is the consequence of the box having to hold its own.

### Step 1 — One process per camera, or fifty in one?

**The question:** a server can handle a thousand cameras; should that be a thousand containers?

**The first instinct:** yes — isolation, and the orchestrator handles lifecycle.

**What broke it:** not overhead, *lifecycle*. Container-per-camera makes camera CRUD a deployment operation, and camera lifecycle must survive the control plane being down. And a camera's RTSP session must be owned exactly once — cameras cap concurrent sessions at two to four — so recording is a `tee` branch inside the process that owns the session, not a second consumer.

**The answer:** sharded workers, 20–50 pipelines per process, measured in PSS rather than RSS because summing RSS double-counts shared library pages. **The orchestrator manages shards; the reconcile loop manages cameras.** Two supervisors, never merged.

And the rule that made Python viable for the course: **Python touches control, never data.** Buffers move on GStreamer's native threads in C; PyGObject releases the GIL; a per-buffer callback across fifty cameras at 25 fps is 1,250 GIL acquisitions a second and kills the worker. The rule generalises past Python — it is about crossing a language boundary once per frame — which is the real argument for C++ in the media worker, and a better one than "faster".

### Step 2 — The box owns its OS (М9)

**The question:** how does an appliance nobody visits update itself?

**The answer:** A/B root filesystems under RAUC; signed bundles with a two-level CA and a keyring holding trust anchors only; a one-attempt rollback where **the absence of a success signal is the failure signal**; and a health check that decides rollback *on the box, offline* — never by asking a monitoring server, or a network fault rolls back a good update across the fleet.

**What was found later:** the health-check ladder is an alert-quality ladder, and its bottom row — *is footage being written* — is the highest-stakes alert in the course, written three modules before alerting is taught. And the module had been shipping a defect since its first lesson: `kvssink` publishing straight to AWS with nothing behind it, so an uplink blink was data loss. The **spool** was added — segments to the data partition, a separate uploader, delete on acknowledgement, a bound with a stated policy, a rate-limited drain — and those segments became the first artifact a later module *upgrades* rather than replaces.

### Step 3 — The box owns its truth (М9)

**The question:** where does a camera's configuration live now that Kinesis does not hold it?

**The first answer:** two databases — a domain database of what the operator asked for, and a node database of what the box observed — sharing one instance.

**What broke it:** that was the pre-inversion design, with the word *cached* still in it. Once the recorder owned its configuration (Step 5), the "domain database on the box" was a component with no referent.

**The answer:** one Postgres, the recorder's, holding three kinds of data that share an engine and nothing else: configuration (the only thing that cannot be re-derived), the archive index (rebuildable by scanning), and events (observations). SQLite was considered and rejected — not on seriousness but because the index and events need a real database regardless, and partitioning is the deciding feature: `DELETE` of 276,768 rows took 231 ms and freed zero disk; detach-and-drop took 5 ms and freed 38 MB.

Three rules were set here that everything above inherits:

- **Desired state is persisted; actual state is derived.** Persist the second and you have built a cache that lies — a green console over a box recording nothing, and the student builds that bug on purpose in М9 Lesson 6.
- **`observed_revision >= revision` is the only definition of applied**, at every layer. An integer, because ordering expresses *distance*; a hash expresses only difference and a timestamp needs clocks to agree.
- **Operator-owned versus controller-owned columns is a security boundary.** `phase` and `observed_revision` are never settable by a client. And the `cameras` table has no recorder column a client may write, because **which recorder owns a camera is decided for the operator, never by them.**

**Found on audit, much later:** `rtsp_url text` stores the customer's camera passwords in plaintext, because an RTSP URL carries `user:pass@` inline — and every code path that formats that URL into a log leaks it. Split out, encrypted, with the honest note that key placement is the hard part.

### Step 4 — A recorder is not a server (М11)

**The question:** when a server dies, who owns its cameras?

**The first answer:** a controller reassigns them to a surviving server.

**What broke it:** the objection was aimed at camera→server. But a recorder is a Nomad allocation with stable identity — its configuration does not change when Nomad moves it. Camera 7 belongs to *recorder 3*, permanently, and recorder 3 has simply moved. **Failover rewrites nothing, because ownership never changed.**

This was conceded cleanly and the design was rebuilt around it. The consequence the rest of the course rests on: **the recorder owns its own configuration and replicates it one way upward.** It does not cache anyone else's. What sits above is a *directory*, not a configuration store.

Three candidate designs were built and compared for this; the chapter was later removed and the choice simply stated, because a course should state its organising decision rather than arrive at it.

### Step 5 — What must outlive a server, and the zombie

**The question:** recorder 3 lands on Server B with an empty disk. Where does its state come from?

**The table:** footage stays (a replacement records the future); the index is rebuilt by scanning; events are expendable; **configuration is the one thing that must travel.** Two ways to make it travel:

- **2a — shared storage**, a CSI volume: exclusive attachment even fences for you. **Rejected**, and for a reason worth checking rather than assuming: Nomad issue #12118, still open, says the volume stays attached to a dead client and needs manual detach. Shared storage buys fencing and *loses* unattended failover — the wrong option precisely for an appliance nobody visits.
- **2b — local storage, replicated one way** to an off-box restore point. **Built.**

**The zombie:** the old instance is not dead, only paused, and wakes up writing. Kleppmann: a lock service cannot stop a paused client; the resource must reject the stale token. So the **epoch** goes in the archive path, issued by check-and-set against a Nomad Variable. Nomad's variable *locks* were the trap — an opaque UUID, not a monotonic token. A Postgres sequence was the other trap — a restored database reissues numbers already written into paths.

**And the RPO became visible:** the configuration that comes back may be behind what the operator last saw acknowledged. Hence the acknowledgement rule — ack on local commit, show *not yet replicated*.

### Step 6 — Where the domain's database lives (six revisions)

This decision record was revised six times, each revision left visible, and every one moved in the same direction.

1. *"A domain exists when its Postgres exists"* — Postgres on every host, synchronised. **Wrong:** multi-master for desired state is exactly what must not happen.
2. *"Hosts cache a slice of the domain database"* — with SQLite for the cache. **Wrong on the engine:** the index and events need Postgres anyway. Two databases, one engine.
3. *"The recorder owns; the domain observes"* — inverted by Step 4. The domain becomes a directory.
4. Found wrong in five places about the epoch: it said the domain database issues it. It does not; the epoch is a Nomad Variable, so a restored directory cannot reset the fencing tokens.
5. **The domain database removed entirely.** The directory was doing two jobs with nothing in common — a *list* (kilobytes, read constantly, needs consistency) and a *restore point* (megabytes, read once on failover, needs durability). A database is a defensible answer to either alone and a poor one to both. The list went to a Nomad Variable per recorder; the restore point to an object per recorder. One-writer-per-key became a platform property (a Nomad ACL) rather than a convention. The repmgr-versus-Patroni HA argument the record had been heading for simply did not happen.
6. **The restore point reclassified as the cluster's** (Step 8), so the domain is not needed to recover either.

The rule that came out of it — *small and consistent in the scheduler's store; large and queryable in a database; large and opaque in an object store* — is the one Part 1 §1.5 states.

### Step 7 — Trust and rights, split by where they must work

**mTLS on every stream**, from a domain CA, certificates naming the *recorder* rather than the server. Issued inside the domain so renewal never reaches outside it. First introduced as a stand-in marked temporary, to be replaced by a delegated intermediate — and later *promoted* instead (Step 10).

**Rights are recorder-local**, because enforcement must survive the domain being down. The asymmetry that decided it: a stale camera edit is benign and self-announcing; a stale *revoke* is silent, adversarial, and unbounded — the removed administrator keeps the site until someone reaches that recorder. So grants carry `valid_until`, renewed on the stream that already carries configuration, converting an unbounded window into a number the product states.

**Human credentials removed from the recorders.** М9's per-recorder `operators` table became N Alices and N stealable hashes, with a grant that expires attached to a credential that does not. The recorder now holds the signer's public key and verifies a short-lived token offline. Two lifetimes — token and grant — and the revocation window is the shorter, which most people get wrong. Break-glass named as the honest residue.

### Step 8 — Cluster and domain become different sizes

**The question:** the modules had been split into ClusterVMS and DomainVMS, but the two spanned the same machines, and the split felt like a framing rather than a fact.

**The answer:** make them different scales. A **cluster** is servers on one network you would bet recording on — physics. A **domain** is the clusters under one directory — administration. A campus is one domain and three clusters.

**And the rule that fell out:** a recorder never crosses a cluster during failover — its footage is on that cluster's disks. Chosen for archive locality, it happened to put the epoch at exactly the scope Nomad's per-cluster raft provides; regions share no state, so there is no domain-wide raft, and none is needed. Two unrelated arguments landing on one line is usually a sign the line is real.

**Consequences:** the restore point is cluster-scoped, so the domain drops out of the recovery path entirely. Nomad federation moved down from the top of the course to М12, because a domain of several clusters *is* federated regions. And the "three things a recorder cannot know" — lookup, placement, rebalance — turned out to be cluster questions, already answered by the Variables М11 had built and called something else. **М12 became a directory *of directories*, which cannot be strongly consistent** — the CAP boundary, drawn by a network you stopped trusting — and that difference in kind is what makes it a module rather than the same one with bigger nouns.

### Step 9 — No domain controller

**The question:** where does the domain controller live?

**The answer:** there isn't one. What runs at the domain is five small services — a signer, placement, a read view, an update server, a remote observer — hosted by one designated cluster — the **domain cluster**, Nomad choosing the server, failing over within that cluster like any allocation, and dying with it. **The signer's key is the only state that cannot be regenerated — and, where the customer has no identity provider, the local user records beside it** (М12, *Where users live*; both published as objects for restore): a software key in the cluster's raft, on purpose, because a TPM-sealed key pins the signer to one server and defeats the failover it just gained; acceptable because everything it signs is short-lived. The word *domain controller* was retired, because it named a component the design had dissolved and implied an authority the layer does not have.

### Step 10 — The layer above the domain does not exist

**The question:** why is there a level above the domain?

**The first answer**, over three renames — FederatedVMS, then OrchestratedVMS: a layer holding a root CA, a federated identity, a vault, a fleet inventory, and rented capacity.

**What broke it:** a domain can be as large as a customer's whole estate, so **one customer is one domain**, and "above the domain" means "across customers" — which is not the product. It is the vendor. Item by item, every function of the layer turned out to be either something the domain does for itself (provision a cluster from the customer's cloud account; be its own root; federate to the customer's IdP; run its own update server; cache its entitlement) or something the vendor does across customers (vouch for hardware; issue entitlement; publish bundles; rent capacity).

**And the security improvement hiding in it:** a vendor-held root that signs the customer's CA is a vendor who can impersonate the customer's domain. The self-signed CA that every earlier module had marked *temporary* was the right design all along, and was promoted to the customer's permanent root. Five hand-provisioned stand-ins were collected inside М12: four replaced by giving things identities, one promoted.

**What М14 became:** VendorVMS — not a scope, a counterparty. Its thesis is the property enterprise buyers ask for by name: **the product must work with the vendor unreachable, or gone.** Its centrepiece is a table of what the vendor *may* do against what it *must never be able to* — and the right-hand column is a list of things earlier drafts of the course would have let the vendor do.

**The one thing that grew on that side:** the licence system, which had been a bullet, became a lesson once the thesis was applied to it. A licence is a signed document the domain verifies offline, bound to the domain id (a recorder is not a server, and the root has a rotation drill, so neither may anchor it), pulled through the domain's own update server like a bundle, and enforced only at admission — because no consistent domain-wide counter exists and the rule that *cached from above may keep recording forever* admits no exception for money. Revocation does not exist; lifetimes do, and a perpetual licence is the honest offer to a customer asking what happens if the vendor is gone.

### Step 11 — Observability is domain-level

**The question:** does monitoring belong at the lower levels?

**The finding:** it was already there, unnamed — the health-check ladder, spool age, `camera_silent_seconds`, failover time, replica lag — each defined where its failure was introduced. The observability module collects rather than introduces. And its remote observer is *one more domain service*, so the module is domain-level and belongs directly after the domain, before the vendor — which resolved a sequencing hedge that had survived three restructures, since the vendor module's demo (*the vendor disappears for thirty days*) can only be demonstrated with instrumentation in place.

The module's own thesis is the one datacentre monitoring gets for free and this product cannot: **in a datacentre, no news is bad news; at the edge, no news is no news.** Two observers — local, seeing everything and dying with the cluster; remote, in the domain cluster, whose only job is to tell silence from health. Detail is local, summary is domain, for the fourth data type — which the database record had predicted would arrive.

### Step 12 — What was designed out, and why

| Removed | Reason |
|---|---|
| **Consul** | The product runs a PKI regardless (no mesh issues an identity to a device never on the network), so a mesh CA is a second hierarchy that buys nothing. Accepted cost: no health-check-filtered discovery. |
| **A vault, from the product** | Most secrets existed because something had not been given an identity. The one that remains — camera credentials — must work with everything above the cluster unreachable. OpenBao survives only for a multi-tenant vendor. |
| **The domain database** | Two jobs with nothing in common; a Variable and an object each did one better. |
| **The domain controller** | Dissolved into five stateless-or-one-key services. |
| **The root above the domain** | A vendor who can sign your CA can impersonate you. |
| **A shipped dashboard** | Grafana, Loki, Tempo and Mimir are AGPLv3, and §6 triggers on conveying at all. Teach Prometheus; let the customer install Grafana against an Apache-2.0 endpoint. |
| **The words** *domain controller*, *orchestration layer*, *federation* (product sense) | Each named a component that no longer existed. |

### Step 13 — Licensing, settled by reading the licences

**Nomad** is the only BUSL component left. The Additional Use Grant forbids offering the software hosted or embedded *in competition with the licensor's paid products*, and "embedded" is itself defined relative to a competitive product. A VMS does not significantly overlap Nomad Enterprise, so shipping an appliance with Nomad inside is permitted as written. The risk to watch is the analytics-plugin roadmap. The version floor is **1.8.0** (the `disconnect` block), and the Change Date is not an escape route: a version reaches MPL two years after its support ends.

**Grafana** is the sharper case, and it is why the product ships no dashboard.

---

### Step 14 — М9's worker was a stand-in, and the boundary it stood on

The last question asked of the design was the one an engineer asks first: *why is there a controller process at all, when Nomad supervises processes and DriverPack holds the pipeline?* The answer split. Half of the worker — supervision inside a process, the reconcile against desired state, the routing, the reporting — is the worker's and always was; the course built it in Python because it had no worker, and it moves into DriverPack as a contract carried by the tests. The other half — config distribution, fencing tokens, retention, serving people — was never VMS-specific and is the platform's. §1.11 is the row-by-row assignment; the modules keep their invariants and lose a process. The archive's location is the decision that follows from it and is not yet taken.

### Step 15 — The controller and the console became data

Once the controller was the only writer, the question was what it actually knew about a camera: the names of the rows, the fields an operator may set, which heartbeat field is capacity, and a rule for which workers are eligible. That is a description, not a program, so М10 moved it into `vms.subsystem.yaml` and ran one `SpecController` from it; the live and detector subsystems that followed needed no controller of their own, and М11's `ClusterController` turned out to be the one-box class with N = 1 and was deleted. The console followed for the same reason: a page that lists units, edits fields and shows a gauge needs the same description and nothing more. The split into two processes with two tokens (§1.12) came first — the console holds the operator's rows, the controller holds placement, the store refuses the crossing — and then the console became `SpecConsole` over the same YAML, with the VMS registering only where its bytes are. The rule that fell out is the one the platform boundary (§1.11) had been circling: a subsystem is a worker and a description; the platform supplies both processes that surround it.

## What the sequence teaches

Read as a whole, Part 2 moves in one direction: **every step took state and authority *out* of the layers above and pushed them *down* to where the thing that needs them already lives.** The domain lost its database, its restore point, its controller, its root's superior, and finally its own superior. The recorder gained ownership of its configuration, its rights, its identity's verification, and its archive's fencing. And what was left in the middle — the controller and the console — stopped being code at all and became a description the platform runs.

The pattern is not minimalism for its own sake. It is what falls out of taking one rule seriously — *every layer may be unavailable to the one beneath it* — and refusing, each time, to let a component exist merely because the layer above used to be thin enough to need it.

*Written 7 September 2026, after the course reached its current shape.*
