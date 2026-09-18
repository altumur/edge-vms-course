# Edge VMS Course

Course material for building a video management system, shipping it as an appliance, and operating a fleet of them.

---

## Edge → Server → Cluster → Domain — and then the vendor

The module names are not decoration. They mark one idea getting harder three times, and the course is arranged around it: **where the truth about the system lives, and how many things are able to disagree about it.**

|                      | The box knows                                                         | Truth lives                                                  | What can disagree                                  | The new hard problem                                                                                          |
| -------------------- | --------------------------------------------------------------------- | ------------------------------------------------------------ | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| **М9 · EdgeVMS**     | what it *is*                                                          | in the image that booted                                     | nothing — a box is whatever was flashed onto it    | replacing the OS underneath a running product without destroying the recordings                               |
| **М10 · ServerVMS**  | what it *should be*                                                   | in a database on the box                                     | desired state and actual state, inside one process | closing the gap — and never persisting the half that must be re-derived                                       |
| **М11 · ClusterVMS** | what it should be, *in the cluster's raft, written by one controller* | in each worker's heartbeat, from whichever server it runs on | **two instances of the same worker**               | surviving a server's death without two writers reaching one archive                                           |
| **М12 · DomainVMS**  | what it should be, *and which cluster holds it*                       | in each cluster's raft, with a directory across clusters     | clusters, with the directory                       | a layer that must stay useful while it is allowed to be down — and honest when a whole cluster is unreachable |
| **М14 · VendorVMS**  | *— not a scope of the product —*                                      | nowhere the product depends on                               | the customer, with the vendor                      | **working with the vendor unreachable, or gone**                                                              |

**Every boundary in that table is a network you stopped trusting — except one, and it is set by administration instead.**

A **cluster** is servers close enough to share a link you would bet recording on: one LAN, usually one server room. That is physics. A **domain** is the clusters under one directory, one CA and one set of operators — one customer installation, which may be one cluster or several. That is administration. A campus is one domain, three clusters, three sites; a cloud deployment is one domain, one cluster, fifty sites. **Sites and clusters are many-to-many on purpose**, and rented servers in a cloud region are a cluster like any other, which is what lets М13 offer edge, cloud and mixed from one codebase.

The rule that makes the two modules genuinely different, and everything above depends on it:

> **A worker fails over within its cluster and never across one.** Its cameras' footage is on that cluster's disks. So a whole cluster dying is not a failover but a larger event — and М12's job there is to report honestly what is unreachable, not to heal it.

That rule was chosen for archive locality, and it turns out to put the fencing epoch at the right scope too, since Nomad's raft is per-cluster. When two independent arguments land on the same boundary, the boundary is usually real.

### Three words the course keeps apart

|              | What it is                                                                                                                                                                                                                                                                                                            | Who decides                 |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------- |
| ~~**Node**~~ | *retired (12 September 2026).* М9's recorder — a process with its own database and disk, distinct from the box. Under М10's shape nothing on a server needs a fifth word: the platform's stores, a **resource** on its disks, and the **workers** the scheduler placed there. М9 Lessons 5–9 keep the word as history | —                           |
| **Server**   | a box with CPUs and disks: a replica of the cluster's stores, a **resource** (the platform's job on its disks), and whichever **workers** the scheduler places there                                                                                                                                                  | the scheduler, continuously |
| **Site**     | where cameras physically are. The only one of the three an operator names                                                                                                                                                                                                                                             | the customer's building     |

**A worker is not a server**, and М9 builds exactly one box without ever needing the distinction. It matters from М11 onward, where a server dying moves a *worker* — whose cameras are assigned to its name in the cluster's raft — rather than reassigning cameras, which is why failover rewrites nothing; what stays on the server is a *resource*.

[**М8**](./М8_KVS_VMS) comes before the progression starts: it builds the product itself with no local truth at all, because Kinesis holds the configuration and the archive both. Everything after it is the consequence of the box having to hold its own.

**EdgeVMS has no desired state.** You flash an image and containers run; actual state is the only state there is. М9's whole job is making that replaceable safely — which is why it is four lessons about atomic updates, rollback and signatures, and not one about cameras.

**Recorder introduces the wish.** A row saying a camera should be recording is not a camera recording, and something has to close the gap — five lessons in which the student writes that reconciler by hand, at a scale where both ends fit in one terminal. The rule it turns on runs through everything above: *desired state is persisted, actual state is derived.* Persist the second and you have built a cache that lies.

**ClusterVMS is where the second box appears, and it teaches one idea twice on purpose.** First a production reconciler — a Nomad cluster, М10's controller, workers and resource as jobs on it, a server pulled off the wall — because a jobspec *is* desired state and a scheduler *is* the loop, and Nomad, not the controller, decides how many workers run and where. Then the pivot: **Nomad's allocations belong to whoever placed them, and nothing argues.** A camera is *owned*, two instances of the same worker can claim it, and a paused process is indistinguishable from a dead one. That is why М11 is the only module where a mistake corrupts customer footage instead of stopping a service.

**DomainVMS is what is left once a cluster works alone — and it is less than expected.** A cluster fails over, restores from its own object store, and answers *where is camera 7* from one raft, strongly consistent, asking nothing above it. What only a domain can know is what stops being knowable with a second cluster: which cluster holds a camera, which cluster should get a new one, and **whether an answer is complete** — because across clusters there is no raft, only aggregation that is partial and bounded-stale. The domain is a directory *of directories* that cannot be consistent, and its honesty about that is the module.

**Every layer is allowed to be unavailable to the layer beneath it**, and the layer beneath caches what it needs to carry on. workers keep recording when the cluster's directory is down; clusters keep recording — and keep being edited — when the domain is unreachable; and **the domain keeps operating with the vendor gone**, which is where that rule stops being one decision among several and becomes a property a customer can be promised.

**The domain is the top of the product.** One customer is one domain, and a domain can be as large as their whole estate — three server rooms, or one cloud cluster serving fifty shops. Earlier drafts had a layer above it holding a root CA, a federated identity, a vault and a fleet inventory; item by item, each turned out to be either something the domain does for itself or something the *vendor* does across customers. So М14 is not a fifth scope. It is the far side of a boundary — and the reason the design has no vendor-held root over any customer's trust domain is that a vendor who can sign your CA is a vendor who can impersonate you.

The rule has a sharp edge, and it is the one worth carrying away: **anything a layer caches from above may keep recording forever, and must never delete anything.** Destructive operations expire; recording does not. A cluster owns its own retention policy, so it cannot go stale on that — but entitlement and placement come from above, and those can.

**М13 adds no scope either, for the opposite reason.** Observability is how you see the four you already have — and it is domain-level: the remote observer is one more domain service, and its thesis (*a silent cluster is unreachable, not broken*) is М12's honesty about incomplete answers applied to metrics. It comes **before** the vendor, because the vendor module's demo — *the vendor disappears for thirty days* — can only be demonstrated with instrumentation in place, and its canary halt condition is an alert rule. It does not get a VMS name because it builds nothing new to name.

---

## The arc

A shipped edge VMS is seven layers deep. One module per layer, each ending with something that runs.

| Module | Layer it builds | State |
|---|---|---|
| [**М8** — Cloud VMS](./М8_KVS_VMS) | The product itself, against a cloud archive | **Complete** · 8 lessons |
| [**М9** — EdgeVMS](./М9_EdgeVMS) | 1 · RAUC — OS, atomic, rollback<br>3 · Postgres — the recorder's own state<br>4 · The worker — the loop that acts on it | **Written** · 9 lessons |
| [**М10** — ServerVMS](./М10_ServerVMS) | The platform's shape on one server: `driverpacksrc`, `archivesink`, a controller and a worker — the subsystem contract, prototyped without a scheduler | **Written** · 9 lessons |
| [**М11** — ClusterVMS](./М11_ClusterVMS) | 2 · Nomad + Podman — workers that outlive their server, resources that stay, one controller<br>4 · The cluster's own directory | **Written** · 5 lessons · rewritten to *2c* with `clustervms/` on М10's `vmsserver/`, 29 tests |
| [**М12** — DomainVMS](./М12_DomainVMS) | 4 · Several clusters, one directory of directories<br>5 · The domain as its own root: enrollment, lifetimes, identity<br>7 · Its own update server, and clusters it rents for itself | **Written** · 8 lessons |
| [**М13** — Observability](./М13_Observability) | 6 · Prometheus + logs — collecting what М9–М12 emit, from the domain cluster | **Designed** · 4 lessons |
| [**М14** — VendorVMS](./М14_VendorVMS) | *Not a layer.* MASA, the licence system, publishing, the hosting business — and what the vendor must never be able to do | **Designed** · 5 lessons |

**[GLOSSARY.md](./GLOSSARY.md)** defines every term the course uses precisely — worker versus server, desired versus actual state, epoch and fencing, and the acronyms it would otherwise leave unexplained.

**[COURSE-PLAN.md](./COURSE-PLAN.md)** carries the full reasoning: why the modules run in this order, what each contains, and the two structural decisions that were taken along the way — the Nomad licence (BUSL, permitted for this use; version floor ≥ 1.8.0) and where secrets live once there is no secrets manager in the product.

**[ARCHITECTURE.md](./ARCHITECTURE.md)** is the system as it stands — the five words, what runs where, the three stores, the trust table, the failure matrix — followed by the fourteen steps that produced it, in order, each with the question that forced it and what it removed. Read it before the modules if you want the destination first.

---

## М8 — Cloud VMS

A simulated camera publishing to Amazon Kinesis Video Streams, and a single-page console that renders a timeline of archived footage and plays back any moment on it.

Fifteen lessons take a student who knows Python but has never built a web application from a first FastAPI route to a running system: HTTP and Pydantic, process supervision and signals, containers, GStreamer pipelines, boto3 and the KVS archive APIs, then the timeline and playback, ending with a run against the specification's own acceptance criteria.

- [Lesson index](./М8_KVS_VMS/README.md) — start here
- [Project specification](./М8_KVS_VMS/module-design.md) — the complete brief the project is built from
- [KVS capability map](./М8_KVS_VMS/kvs-capability-map.md) — every Kinesis Video Streams feature a VMS can use, tiered by distance from the MVP
- [`reference/web`](./М8_KVS_VMS/reference) — the finished frontend, for comparison rather than copying
- [`kvsvms/`](./М8_KVS_VMS/kvsvms/README.md) — the project, whole: server, edge, scripts, web, the agent image М9 runs, and the acknowledged spool uploader

## М9 — EdgeVMS

Nine lessons in two halves. **Lessons 1–4** turn that cloud VMS into an appliance: A/B partitions, signed update bundles, rollback proven by shipping a deliberately broken update, and then Podman and Quadlet. **Lessons 5–9** make the box own its truth: `INSERT INTO cameras` causes a camera to start recording, `DELETE` stops it, and killing the worker loses nothing but the open segment.

Its spine is that a real edge product has **two independent update planes** — the operating system underneath, the workload on top — and both are visible on one box. М9 Lesson 4 is where it bites: Podman's storage must be redirected to the data partition, because images and volumes left in a rootfs slot are destroyed by the next OS update. Conflate the planes and you build systems where a config change requires an OS flash.

The same lesson has the module's other sharp edge. **Pull the network cable for ten minutes and go looking for those ten minutes of video** — with `kvssink` publishing straight to AWS there is nothing behind it, so an uplink blink is data loss rather than a visibility problem. So the box spools segments to the data partition and uploads them separately, deleting only on acknowledgement. Those segments are the first thing in the course a later module *upgrades* rather than replaces: **М9 puts an index over the same files and they become the archive; М13 makes the upload conditional.**

*The multi-node half of this module moved to М11, where workers are scheduled across servers. A module called EdgeVMS should not build a raft cluster.*

- [Lesson index](./М9_EdgeVMS/README.md) — start here
- [Module design](./М9_EdgeVMS/module-design.md) — both halves in one record: the two update planes, the partition layout and the ARM appendix; then the recorder's lesson plan, the Python shard model, and what the operator is never asked to decide
- [RAUC alternatives](./М9_EdgeVMS/rauc-alternatives.md) — SWUpdate, Mender, bootc, systemd-sysupdate, and where each wins
- [`edgevms/`](./М9_EdgeVMS/edgevms/README.md) — the module's artifacts, whole: the bench, the PKI, RAUC config and bundle builder, the GRUB state machine, the health check (now reading the VMS's own signal — М10's worker and archive), Quadlet units, the spool
- [One container per camera?](./М9_EdgeVMS/worker-and-process-model.md) — the process model at 1000 cameras, and why the orchestrator must not own camera lifecycle

Both reach the same shape of conclusion, as does the orchestrator record now filed with М11: the tool that teaches best is not always the tool that ships best, and the documents say which is which.

**The recorder half (Lessons 5–9).** Five lessons in which one box starts owning its own truth. `INSERT INTO cameras` causes a camera to start recording; `DELETE` stops it; killing the worker loses nothing but the open segment. Between the row and the pipeline there is only a loop the student wrote.

Its organising rule is that **desired state is persisted and actual state is derived** — persist the second and you have built a cache that lies. It is also where the process model from М9's decision record gets built: fifty GStreamer pipelines in one Python process, with the GIL boundary demonstrated rather than asserted.

- [`reference/shard-memory-probe.py`](./М9_EdgeVMS/reference/shard-memory-probe.py) — measures what sharding actually saves, in PSS rather than RSS
- [`edgevms/`](./М9_EdgeVMS/edgevms/README.md) — the module's one project: the box (bench, PKI, RAUC, GRUB, health, Quadlet, spool) and, under [`edgevms/recorder/`](./М9_EdgeVMS/edgevms/recorder/README.md), the recorder that runs on it — migrations, reconciler, GStreamer actuator, retention, console, tests

## М10 — ServerVMS

The recorder rebuilt on the shape М11 arrived at — **workers, resources, one controller** — and prototyped on a single box first, without a scheduler, without KVS and without a database. From the GStreamer end: `driverpacksrc`, a source that plays files whose names stand in for RTSP addresses; `archivesink`, a local archive on the spool's discipline with the epoch in the path; then `vmscontroller`, the only writer of configuration in the cluster, and `vmsworker`, DriverPack itself as the worker. The result is the **subsystem contract** — a controller, a worker, a config prefix and a heartbeat — that detectors and the gateway will implement the same way, and that the platform knows without knowing what a camera is.

- [Lesson index](./М10_ServerVMS/README.md) — start here
- [Module design](./М10_ServerVMS/module-design.md) — the three elements, the two processes, where configuration lives, and the contract every subsystem gives the platform
- [`vmsserver/`](./vmsserver/README.md) — the module's code, whole: the platform's two stores with CAS, the epoch issuer and the lease, `driverpacksrc` and `archivesink`, `vmsworker`, `vmscontroller`, the console, the systemd units, identity by claim, events beside the segment, the resource as a platform job, and a second subsystem that counts seconds; 42 tests
- [`vmsserver-go/`](./vmsserver-go/README.md) — the same module in Go, standard library only, 96 tests: the platform, the controller, the console, the resource and both workers, whole. Both ports are kept complete, and `vmsserver/tests/test_cross_go_worker.py` runs this binary against the Python controller over one store — because a green suite on each side proves each side self-consistent and nothing about whether they agree

## М11 — ClusterVMS

Five lessons, built on the decision М10 arrived at: **workers, resources, one controller.** A worker is a scheduler allocation that claims its name by CAS, so when a server dies Nomad reschedules the worker, it claims the same name and its cameras — assigned to that name in the cluster's raft — come with it. A resource stays with its disks. The controller wrote the configuration before the failure and is not consulted during it. Failover rewrites nothing, because nothing was on the server that needed to travel; and nobody in the VMS decides how many workers run or where — Nomad places them and its Autoscaler moves `count` from the workers' own load.

The module states that decision rather than arriving at it, then spends five lessons earning it — because the deciding fact is not obvious: **two writers to one video stream cannot be merged.** Nothing above can arbitrate after the fact, which is why this is the only module in the course where a mistake corrupts customer footage rather than stopping a service.

**It is a complete product on its own**, which is the clearest evidence the split was real: one cluster, failing over from its own raft, asking nothing above it for permission. A single-building customer needs nothing else.

It is built backwards from one demo. Three servers, two workers, two hundred cameras; pull the power on a server and watch `w-1` reappear elsewhere in the same cluster reading the same assignment — with the edit made during the failover already there. Then bring the dead server back and let its old instance of `w-1` try to keep writing. **It is fenced at its slot and at every epoch, and the archive is intact — the student can prove it.**

The answer is that fencing belongs at the archive rather than at a coordinator: the epoch is part of the segment path, so the stale instance cannot name the files it would otherwise corrupt. You cannot stop a zombie from writing — you can only make its writes harmless.

- [Lesson index](./М11_ClusterVMS/README.md) — start here
- [Module design](./М11_ClusterVMS/module-design.md) — the cluster, what must outlive a server, the zombie writer, and fencing at the archive
- [`clustervms/`](./М11_ClusterVMS/clustervms/README.md) — the module's code, whole, built on М10's `vmsserver/`: Nomad Variables and MinIO as the platform's stores, the worker as an allocation claiming its slot from `NOMAD_ALLOC_INDEX`, the controller placing under label constraints, the archive resource as a system job with its manifests served, a timeline across servers, the event database that is a cache each resource keeps over its own buckets and a console merges, М10's resource process as the platform's job, the events mirror as a copy to the next resource; four jobs with the `scaling` policy and the Autoscaler, two ACL policies, the bench check and the failover drill; 31 tests
- [`reference/`](./М11_ClusterVMS/reference/README.md) — the first design's scripts, kept: the zombie with two real processes, the CAS issuer, the lease arithmetic
- [Kubernetes vs Nomad](./М11_ClusterVMS/kubernetes-vs-nomad.md) — why the orchestrator is Nomad, what it cost, and why neither belongs on one box

## М12 — DomainVMS

Eight lessons, and they open from an unusual position: **М11 already works.** A cluster's controller owns its configuration in its own raft, a worker survives its server by claiming a slot, and nothing above the cluster is asked, so this module has to justify why a layer above should exist. Exactly three things a cluster cannot know — where a camera is across clusters, which cluster should get a new one, and whether an answer is complete — and that is a **directory of directories**, not a configuration store.

Which makes it the first layer in the course that is **allowed to be unavailable** — and more so than it first looked. Recording continues without it, playback continues, an operator can still edit a camera at its own cluster's console, and **a dead server still fails over**, because М11 put both of failover's dependencies inside the cluster. What stops is choosing a cluster for a new camera, cross-cluster lookup, and issuing certificates and tokens. None of it is recording.

The shape that makes it a module rather than a chapter is a campus: **three server rooms, three clusters, one customer, one directory.** Nomad calls a cluster a region and joining them is federation, so the mechanism was always here. And when a whole cluster goes dark the domain's job is honesty rather than recovery — those cameras are on that cluster's network, so **rebalancing them elsewhere would produce workers failing to reach a dead network and hide the real fault.**

It turns out not to be a database at all: the domain reads two objects per cluster — the controller's snapshot and the workers' heartbeats — and asks by `ref`, the name it gave a camera, never by a cluster-local id. Six revisions of the decision record moved in one direction throughout, and the seventh removed the last database in the product.

Because each cluster is the writer of its own rows, each cluster is the thing that must be reachable and protected: М12 Lesson 4 gives every cluster↔domain stream **mTLS from the domain's own root** — the certificate names the server, never a worker — and that root is the customer's, self-signed on purpose, with nothing above it. Certificates are issued *inside* the domain, so renewal never depends on anyone — the same reason grants live in each cluster's own Variables, carried there by a domain agent, and carry an expiry rather than being looked up. A worker never sees a user, a token or a grant.

**The second half is the domain looking after itself.** A box enrolls into the domain (М12 Lesson 6) and receives its certificate from the domain's own signer; the root gets lifetimes and a rotation drill (36), because nobody above will re-issue anything; and the domain provisions a cluster in the customer's own cloud account (37) and proves the worker cannot tell where it is running. That last one closes the arc: **М8 rented a cloud VMS from Kinesis, and here the same product exists with nothing rented** — М9's hand-provisioned AWS credentials retired by no longer being needed. Its own update server and its entitlement cache (34) are what let it run with the vendor gone, which is М13's whole subject.

- [Lesson index](./М12_DomainVMS/README.md) — start here
- [Module design](./М12_DomainVMS/module-design.md) — the directory of directories, placement, the domain services, enrollment, lifetimes and rotation, and a cluster the domain rents for itself
- [Where the databases live](./М12_DomainVMS/where-the-database-lives.md) — six revisions ending with one database in the whole design, and a seventh that removed it when the cluster's controller took ownership of configuration; the retention rule that protects customer footage
- [`domainvms/`](./М12_DomainVMS/domainvms/README.md) — the module's code, whole, built on М11's `clustervms/` and М10's `vmsserver/`, with the real controller and workers in every test: the directory of directories with incompleteness as a result, cluster placement by reachability with the worker left to the cluster, shadow mode keyed by ref, the read model from heartbeats and the console, the live gateway that follows a failover, the signer with lifetimes and root rotation, identity that never reaches a worker, cluster-local grants carried by the domain agent, enrollment with a voucher or an approval, entitlement with a grace period, the cloud arithmetic; four Nomad jobs and 41 tests

## М13 — Observability

**It does not introduce observability — it collects it.** Every module below already emits signals, defined where the failure that needed them was introduced: М9's health-check ladder and spool age, М9's `camera_silent_seconds`, М11's failover time, М12's replica lag. Six signals for a whole VMS, and the module opens by taking inventory rather than installing anything.

What makes it hard is one sentence: **in a datacentre, no news is bad news; at the edge, no news is *no news*.** A site that stops answering might be broken, or its uplink might be down, or the building might have lost power — four different problems for four different people, and an identical signal in every case. A monitoring system that cannot tell *broken* from *unreachable* either pages somebody whenever a router reboots or stays quiet through a real outage, and in practice does both.

That collides with the rule everybody knows — **monitoring must not share a failure domain with the thing it monitors** — because Prometheus pulls, and you cannot pull across the link you stopped trusting. The resolution is two observers with different jobs: a local one that sees everything and dies with the site, and a remote one whose only job is to tell silence from health. Which is **detail is local, summary is domain** for the fourth time — the rule that predicted a fourth data type would arrive.

It also carries the course's second licensing finding, and a sharper one than Nomad's: **Grafana, Loki, Tempo and Mimir are AGPLv3**, and §6 triggers on shipping at all, modified or not. Prometheus, VictoriaMetrics, Thanos, Cortex, the OTel Collector and Grafana Alloy are Apache 2.0. The course's position is to teach Prometheus and **ship no dashboard** — which is a better product decision anyway, since an operator should not need two consoles.

- [Module design](./М13_Observability/module-design.md) — the observer's paradox, what silence means, alert design, and the AGPL problem

---

## М14 — VendorVMS

Five lessons, and a different kind of module: it builds no scope of the product, because the domain is the top of it. What sits above a domain is not a layer — **it is the vendor**, a separate organisation on the far side of a boundary the product is designed to work across in one direction only.

The thesis is the property enterprise security buyers ask for by name: **the product must work with the vendor unreachable, or gone.** The demo is built backwards from it — a year-old domain, and on a date the student picks, the vendor disappears entirely. For thirty days nothing that was working stops; what *cannot* happen is enumerated rather than implied; on day thirty-one the licence runs out of grace and degrades exactly as М12 wrote down.

The module is the mirror image of the course so far. М9–М12 asked what the product must do; this one asks **what the vendor may do, and what it must never be able to do** — vouch for its hardware but never join a box to a domain on its own; issue an entitlement but never stop recording by withholding one; publish a bundle but never push it onto an appliance; rent a cluster but never hold the customer's root. The right-hand column is a list of things earlier drafts of this course would have let the vendor do.

What genuinely belongs to the vendor: **the MASA** and the ten-year commitment running one implies; **the licence system** — a database, one signing key, and a signed document the domain verifies offline, bound to the domain id rather than to any hardware, counted at admission and never against a camera already recording; signing and publishing bundles, and rolling them out across customers with a canary that halts itself; support inventory at the customer's discretion; and the hosting business as a commercial option the module frames without taking. **OpenBao's real scope finally appears here** — dynamic credentials for a vendor holding many customers' secrets — after everything else once assigned to a vault was removed by giving machines identities.

- [Module design](./М14_VendorVMS/module-design.md) — the one-way boundary, MASA, the licence system, publishing and rollout, the hosting business

## How these lessons are written

Two conventions run through every lesson, and they are the reason the material is the length it is.

**Every step produces a result you can see.** A process you can signal, a container you can inspect, a pipeline whose output you can play, a page you can look at — not code taken on trust. Where a dependency could not be installed, the lesson still proves its logic some other way rather than asserting it.

**Each lesson replaces a stand-in from the one before.** `camera_sim.py` becomes the real pipeline, `filesink` becomes `kvssink`, fake clients become boto3, fixtures become real fragments. Nothing is ever more than one layer away from something already verified.

Decisions are recorded with their costs attached. Where a choice is weak — a missed acceptance criterion, a capability given up, a licence worth a lawyer's eye — the documents say so instead of quietly moving on.
