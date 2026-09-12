# Course Plan — the whole picture

A shipped edge VMS is seven layers deep. The course builds them in dependency order, one module per layer, each ending with something that runs.

---

## The seven layers

| # | Layer | The question it answers | Module | State |
|---|---|---|---|---|
| 1 | **RAUC** | What OS is this box running, and can I change it safely? | М9 | Designed |
| 2 | **Nomad + Podman** | What workload is running, and where? | М9 (one box) · М11 (a cluster) | Written |
| 3 | **Postgres** | What does this system know about itself? | М9 | Written |
| 4 | **The directory** | Which worker, which cluster — and is that answer complete? | М11 (a cluster's) · М12 (across clusters) | Written (М11) · Written (М12) |
| 5 | **The domain signer** | Who is allowed to know what, and how do they prove it? | М12 — the domain is its own root; *OpenBao only for a multi-tenant vendor, М13* | Written |
| 6 | **Prometheus + logs** | Is it working, and how would I know? | М13 — domain-level; the remote observer is a domain service | Designed |
| 7 | **The vendor boundary** | What may the vendor do, and what must it never be able to? | М14 | Designed |

Layers 1–2 are the two update planes М9 is built around: the OS underneath, the workload on top — both visible on a single box, which is all М9 needs. М9 also owns the **third** thing on that box, which is neither: the data. A spool of recorded-but-not-yet-uploaded segments outlives both planes, and the segments it writes are what М9 turns into the archive. Layers 3–5 are the product, and **the domain is its top** — one customer is one domain. Layer 7 is not a layer of the product at all: it is the far side of a boundary the product must work across in one direction only.

**Layers 5 and 7 turned out to be one layer.** They are both in М12. The plan had identity in layer 5 and device management in layer 7, three modules apart, and each asked the same question — *how does a machine prove who it is in order to get its first secret?* Enrollment is where identity and device management meet, and separating them meant neither owned it.

**Layer 4 is split across two modules,** which is a change from this plan's first version. М9 builds the reconciliation loop on a single recorder, where both ends of it are visible at once; М11 handles what happens when ownership is contested. A database with nothing acting on it is not a working system, so М9 could not stop at Postgres.

**Layer 2 moved out of М9 and into М11.** М9's own progression promises one box — *a box is whatever was flashed onto it* — and it cannot promise that while building a three-server cluster in its second half. It spent one revision in М9, on the grounds that scheduling is desired-state work; that is true, but it put the two-level idea in two modules and taught it twice. A cluster and the controller above it are one arc. Nomad's cross-site federation went further still, to М13, where many networks actually begin.

---

## Two structural warnings

### 1. The licensing concentration is worse than it looks

The stack this course *would* have reached for — Nomad, Consul, Vault — is **three components under one vendor's source-available licence.** Read from the licence files, not marketing, and kept here because the design's shape is partly a response to it. **Only the first row survived.**

| Component | Licensor | Licence | Change Date |
|---|---|---|---|
| Nomad 1.7.0+ | International Business Machines Corporation | BUSL | 4 years per version → MPL 2.0 |
| Consul 1.17.0+ | International Business Machines Corporation | BUSL | 4 years per version → MPL 2.0 |
| Vault | HashiCorp / IBM | BUSL | → MPL 2.0 |

Three of seven layers under IBM's BUSL, in a product that is *shipped to customers on hardware* — which is exactly the "embedded" word the Additional Use Grant uses. Two mitigations, both real:

- **Vault → nothing.** The product has no vault. Most of the secrets a vault would have held were removed by giving machines identities (the domain signer, М12), and the one that remains — camera credentials — must work with everything above the cluster unreachable, so a central vault is the wrong answer by construction. **OpenBao**, the MPL-2.0 Linux Foundation fork, appears only in М14, and only if the vendor is a multi-tenant host holding many customers' secrets.
- **Consul → drop it.** Nomad has **native service discovery** that needs no Consul, and HashiCorp's own documentation says it "suits edge computing… and minimal single-cluster setups prioritizing simplicity." It gives templated service addresses but *not* dynamic DNS, *not* HTTP/TCP/gRPC health checks with healthy-instance filtering, and *not* service mesh. For a handful of services per site, that is likely enough.

That leaves **Nomad as the only unavoidable BUSL dependency**, and no fork of it exists — unlike Terraform (OpenTofu) and Vault (OpenBao). If that single dependency is unacceptable, the decision is to teach Kubernetes instead, and it should be taken now rather than at М13.

#### Resolved: BUSL does not prohibit shipping this product

Checked against the licence itself rather than against the general alarm around it. **The Additional Use Grant permits commercial use, including embedding**, and prohibits something much narrower:

> "You may make production use of the Licensed Work, provided Your use does not include offering the Licensed Work to third parties on a hosted or embedded basis" in competition with the licensor's paid offerings.

Two conditions must hold **together**. HashiCorp's own FAQ defines both, and the second is what decides it:

> A "competitive offering" is a product sold to third parties "that significantly overlaps the capabilities of a HashiCorp commercial product."
>
> "Embedded" means including code from a HashiCorp product **"in a competitive product."**

*Embedded* is defined relative to a competitive product — embedding alone is not the trigger. Their worked example: a company building a Terraform competitor may still use Vault to secure it. **A VMS does not significantly overlap Nomad Enterprise**, so shipping Nomad inside a VMS appliance is permitted as written. The licensor is now **IBM**, not HashiCorp.

**The risk to watch is not the appliance — it is the plugin roadmap.** The moment the product lets a customer run *their own* containers on it (a third-party analytics platform, a detector marketplace, bring-your-own-model), it starts offering orchestration as a customer-facing capability, and "significantly overlaps" becomes arguable. For a VMS that is not a hypothetical drift: third-party analytics is where every VMS eventually goes. **М13 is the second exposure**, because renting cloud capacity and running customers' recorders makes *hosted* and *embedded* both true, leaving only the competitive test.

Not legal advice. The specific question for counsel is narrower than "can we use Nomad": *does our analytics-plugin roadmap turn the appliance into something that significantly overlaps Nomad Enterprise?*

#### The Change Date is not an escape route

Each BUSL release converts to **MPL 2.0 four years after it is published** — 1.8.0 (28 May 2024) becomes MPL on **28 May 2028**; 2.0.0 (21 Apr 2026) in **April 2030**. Two things make this useless as a plan:

- **The Change Date and the support window move in opposite directions.** By the time a version is MPL it has been out of support for roughly two years. 1.7.x is the worked example: EOL since April 2024, with an allocation-directory-escape CVE (CVE-2024-7625, affecting `>= 1.7.0, < 1.7.11`) fixed **only in Enterprise**. Shipping that to an appliance nobody visits is not a licence saving, it is a defect.
- **The only genuinely MPL Nomad is ≤ 1.6.5** (13 Dec 2023), and the 1.6 branch is a trap: 1.6.6 onward ship BUSL, whose text retroactively claims coverage from 1.6.4 even though the shipped 1.6.4 and 1.6.5 artifacts carry MPL. Anyone relying on "1.6.x is MPL" must pin **≤ 1.6.5** exactly — and then has no Variable Locks, no `disconnect` block, and three years of unpatched CVEs.

**So the version decision is an engineering decision, not a licensing one:** ship a supported release, and treat BUSL as settled by the competitive test above.

**Both are out, and the comparison record between them was retired** once neither survived. The reasoning worth keeping: Consul and a vault overlapped on exactly one thing — mTLS between services — and the product runs a PKI regardless, because no service mesh issues an identity to a device that has never been on the network. Once the domain became its own CA (М12 Lesson 7), a second certificate hierarchy bought nothing. **The accepted cost is health-check-filtered service discovery**, which Nomad's native discovery does not provide and a handful of services per cluster does not need.

#### A vault is not what removes most of these secrets

Worth recording because it inverts the layer table above. Auditing the course's five stand-in secrets against what actually resolves each one: object-store access becomes **workload identity**; the local database password becomes **certificate auth**; the operator account is an **IdP** question; the per-recorder credential and the self-signed CA are already replaced by **mTLS and a delegated intermediate** in М12 Lesson 7.

> **Most secrets exist because something was not given an identity.** Give the machine an identity and the secret it stood in for disappears.

That leaves a genuinely narrow scope for OpenBao — **dynamic credentials, and the secrets a multi-tenant operator holds for many customers** — and makes layer 5 a smaller layer than the table implies. What a vault does that Postgres cannot is *issue and revoke*: the encryption key must not sit beside the data, and a stored `valid_until` cannot revoke anything by itself. If the orchestration layer stays single-tenant, the honest answer may be that the product does not need one, and М14 Lesson 4 is written to reach that conclusion rather than avoid it.

**The exception, and it is the course's only real secrets problem:** camera credentials. An RTSP URL carries `user:pass@` inline, so М9's `rtsp_url` column silently held every customer's camera password in plaintext until М9 Lesson 5 was corrected. Those must work with everything above the cluster unreachable, so a central vault is the wrong answer by construction — they stay at the site under a key the database backup does not contain.

### 2. Secrets arrive three modules before the module that resolves them

М9 Lesson 4 provisions AWS credentials by hand at commissioning. М9 Lessons 5–9 add a database password, an operator account and the camera credentials. М12 adds a per-server credential and a self-signed CA — and then **resolves all five itself**, four by giving things identities and one by promotion: the self-signed CA turns out to be the customer's permanent root. OpenBao appears only in М13, and only for a multi-tenant vendor.

This is deliberate and follows the course's existing discipline — `camera_sim.py` before the real pipeline, `filesink` before `kvssink`, fixtures before real fragments. Hand-provisioned secrets are the stand-in; М12 replaces them, and the replacement is the lesson. What must not happen is the resolution arriving as a surprise: every earlier module should mark its secret handling as temporary at the point it introduces it.

---

## The modules

### М9 — EdgeVMS, Lessons 5–9: Postgres and the AppHost · [written](./М9_EdgeVMS/README.md)

*Folded into М9 on 12 September 2026: the recorder's five lessons follow the appliance's four, so one module carries the box from an A/B root to a database that owns what the box should be. The design brief is [`recorder-design.md`](./М9_EdgeVMS/recorder-design.md).*

The cloud VMS spec forbade a database outright. The appliance needs one, and understanding *why the answer flipped* is half the module: in the cloud, KVS held the configuration; on-prem, the box holds it. The other half is that a row saying a camera should be recording is a wish until something makes it true.

- Schema for cameras, sites and retention; migrations as a shipped artifact that runs at boot on a box nobody visits
- **Operator-owned columns versus controller-owned columns** — the distinction that keeps recorder placement out of the operator's hands
- **The critical one:** `PGDATA` lives on the data partition, so it survives A/B OS updates untouched. This is М9's three-way boundary with real consequences
- The reconcile loop, built against a fake actuator first: desired persisted, actual derived, `observed_revision >= revision` as the only test of applied
- Fifty GStreamer pipelines in one Python process — the GIL boundary demonstrated, `watchdog` for stall detection, and where Python stops being the right answer

### М10 — ServerVMS: the platform's shape on one server · 5 lessons · [written](./М10_ServerVMS/README.md) · [design](./М10_ServerVMS/module-design.md) · [code](./М10_ServerVMS/vmsserver/README.md)

The module that rebuilds М9's recorder on the decision М11 arrived at last — and retires the word, because what is on a server needs none — — **workers, resources, one controller** — and does it on a single box first, so the shape can be prototyped without a scheduler, without KVS and without a database. Three things are built from the GStreamer end: `driverpacksrc`, a source element that plays files whose names stand in for RTSP addresses; `archivesink`, a local archive on the spool's discipline; and the two processes every subsystem will give the platform — a **controller** that is the only writer of configuration in the cluster and a **worker** that runs pipelines and nothing else.

- **`vmscontroller`** — the sole writer of the camera list and of camera-to-worker assignment; stateless, correct by CAS against the platform's config store; never on the recovery path
- **`vmsworker`** — DriverPack as the worker: one process, N pipelines, its own reconcile loop over its assignment; Nomad (М11) runs as many as the workload needs
- **The subsystem contract** — controller + worker + a config prefix + a heartbeat object, the same for the VMS, for detectors, and for the gateway; the platform knows the shape and nothing about video
- **No sharding on one server**, and the open question of where configuration lives answered: the controller writes it, the platform stores it, the worker reads its share
- **KVS retired; the archive is ours**: segments on the spool, closed segments promoted to the archive resource, the epoch in the path, a manifest instead of an index

### М11 — ClusterVMS: workers that outlive their server · 5 lessons · [written](./М11_ClusterVMS/README.md) · [design](./М11_ClusterVMS/module-design.md) · [code](./М11_ClusterVMS/clustervms/README.md)

The only module where getting it wrong corrupts customer data rather than merely stopping a service. М10's shape works on one box; this is one cluster — servers on one network you would bet recording on — and a worker that survives any of them dying.

- **Workers move, resources stay, the controller is not consulted.** A worker is an allocation that claims its name by CAS from `NOMAD_ALLOC_INDEX`; its cameras are assigned to that name in the cluster's raft, so failover rewrites nothing. The archive is a `system` job pinned to its server's disks, unavailable while the server is down and never lost. Configuration never left raft, so an edit made during the failover is simply there
- **Nomad decides how many workers and where.** The worker job carries a `scaling` policy on the workers' own load; the Nomad Autoscaler (MPL-2.0) moves `count`; the controller has no Nomad client. Scale-in releases a slot and the controller redistributes; a crash releases nothing and the controller waits
- **Leases, epochs and the zombie writer.** Dead, partitioned and paused are indistinguishable, and the design must be correct without resolving that. Fencing happens **at the resource, not the controller** — the epoch per camera is in every path — and one layer earlier at the slot, which is what makes Nomad's duplicate-index bug harmless
- **The cluster directory is the assignment.** One scan of `vms/workers/*` answers *where is camera 7*, in one raft, strongly consistent. Placement under label constraints by the workers' own capacity, the stability rule, the server in the reason, and why consistent hashing is the reflexive wrong answer
- **`clustervms/` is built on М10's `vmsserver/`**, importing the contract, the controller, the worker and the archive unchanged; `vmsserver-go/` and `clustervms-go/` are both modules in Go with the same relationship (a `replace` directive instead of `sys.path`), 70 tests, and the measurement

### М12 — DomainVMS: several clusters, and the top of the product · 8 lessons · [written](./М12_DomainVMS/README.md) · [design](./М12_DomainVMS/module-design.md) · [code](./М12_DomainVMS/domainvms/README.md)

What is left once a cluster works alone: **everything that stops being knowable with more than one cluster** — and, since nothing above the domain belongs to the product, everything a domain must do for itself.

- **A directory of directories, and it cannot be consistent.** Inside a cluster there is one raft; across clusters there is none, so the domain aggregates — partial, bounded-stale, and honest about incompleteness. The CAP boundary drawn by a network you stopped trusting
- **Three-level placement, split by what each level knows:** Nomad picks the server, the cluster picks the recorder on capacity, the domain picks the cluster on **reachability**
- **No domain controller.** One signer job, a stateless placement, a read view, an update server — hosted by one designated cluster — the **domain cluster**, Nomad choosing the server. The signer's key is the only state, a software key in raft on purpose, backed up beyond the cluster and rotated on a drill
- **The domain is its own root.** A vendor-held root that signs the customer's CA can impersonate their whole trust domain; so the root is self-signed, permanent, and the customer's. Enrollment (registrar, LDevID, approval, TPM) is the domain's; only the MASA voucher is the vendor's
- **Lifetimes against offline tolerance:** *tolerable outage = certificate lifetime − renewal margin*. Revocation at the edge is a lifetime problem, not a list problem
- **Human identity:** recorders hold the signer's public key, never a password hash; the signer federates to the customer's own IdP. One domain, one Alice
- **A cluster the domain rents for itself**, from the customer's cloud account — and proof the recorder cannot tell where it runs. The bandwidth arithmetic (fifty cameras at 4 Mbps is 200 Mbps up) makes *mixed* the default shape, and closes the arc with М8's rented cloud
- **Its own update server and entitlement cache**, which is what lets it run with the vendor gone
- **Who serves browsers: never a recorder.** A console and a live gateway as two cluster-level jobs — the recorder's only clients — with the failure arithmetic that keeps a web problem away from a recorder. Lesson 3 grows a section for it when the lessons are written

### М13 — Observability: Prometheus and logs · 4 lessons · [designed](./М13_Observability/module-design.md)

**This module does not introduce observability — it collects it.** Every module below already emits signals, defined where the failure that needs them was introduced, because a metric chosen at the moment you watch something break has a reason, and one chosen in an observability chapter has only a name:

| Emitted in | Signal | The point |
|---|---|---|
| М9 L3 | the health check's four-row ladder | it decides **rollback**, on the box, offline |
| М9 L4 | `spool_oldest_seconds`, `spool_bytes_used` | alarm on age, not count — one threshold works at any camera count |
| М9 L9 | `camera_lag` (a distribution), `camera_silent_seconds` | the second: the only one describing the product |
| М11 L4 | `vms_failover_seconds{kind="worst"}` (RTO, from the workers' heartbeats), `vms_epoch_conflicts` | a counter that should be zero forever |
| М12 L1 | `domain_snapshot_age_seconds` | the worst cluster, never the mean |

What is left for this module is what is genuinely *cross-cutting*:

- **Scrape topology, bounded by the domain.** Prometheus pulls, and you cannot pull across the link you stopped trusting — so the scrape boundary **is** the domain boundary, for exactly the reason certificate issuance is
- **The placement rule, which is the module's spine:** *monitoring must not share a failure domain with the thing monitored.* A Prometheus running as a Nomad job inside the domain it watches dies with that domain and cannot tell you it died. And its mirror image from М9 L3: **a health check must not depend on monitoring**, or an unreachable metrics server rolls back a good update across the fleet at once
- **Metrics are the fourth data type**, and [`where-the-database-lives.md`](./М12_DomainVMS/where-the-database-lives.md) predicted it — *"the fourth one will arrive eventually."* Detail is local, summary is domain: full resolution at the site with pull-on-demand, alarms and aggregates to the centre. The same rule as footage, index and events
- **Cardinality, which is how monitoring becomes more expensive than the product it watches.** Per-camera series at a thousand cameras is a thousand time series per metric. Export distributions; leave the per-object number in the database the console already queries. **A metric is not a database**
- **Alarm on the product, not the process** — М9 L3's rule, stated once for everything above it. Fragment write rate and camera-offline, not CPU graphs
- **The thin uplink:** remote-write with downsampling, or local retention with pull-on-demand — and what an operator is shown for a site whose uplink is down, which is *not* "healthy"
- **Logs:** journald, retention, and never letting a secret reach them — sharpened by М9 L5's finding that an RTSP URL carries the password inline, so the leak is a **log-formatting** bug rather than a storage one
- **A second licensing finding, sharper than the Nomad one.** Grafana, Loki, Tempo and **Mimir** are all **AGPLv3**; Prometheus, VictoriaMetrics, Thanos, Cortex, the OTel Collector and Grafana Alloy are Apache 2.0. BUSL *permitted* this product; AGPL §6 triggers on **conveying at all**, modified or not, and Grafana's free Enterprise binary is explicitly not redistributable. The course's position: teach Prometheus and **ship no dashboard** — the customer installs Grafana and points it at an Apache-2.0 endpoint. Full reasoning in [`М13_Observability/module-design.md`](./М13_Observability/module-design.md)

### М14 — VendorVMS: the far side of the boundary · 5 lessons · [designed](./М14_VendorVMS/module-design.md)

**Not a scope of the product.** Formerly FederatedVMS, then OrchestratedVMS — a layer above domains holding a root CA, an identity provider, a vault, a fleet inventory and rented capacity. Item by item, each turned out to be something the domain does for itself or something the vendor does across customers. What remained is the vendor, and the thesis is the property enterprise buyers ask for by name: **the product must work with the vendor unreachable, or gone.**

- **What the vendor may do, and must never be able to:** vouch for its hardware but never join a box to a domain alone; issue an entitlement but never stop recording by withholding one; publish a bundle but never push it to an appliance; rent a cluster but never hold the customer's root
- **The MASA**, and the ten-year commitment running one implies; device → domain routing as the only reason enrollment touches the vendor
- **The licence system** — the vendor's system of record and one signing key; a licence as a signed document bound to the domain id, pulled like a bundle, verified offline by every recorder, counted at admission and never at runtime; lifetimes instead of revocation, and the perpetual licence as the honest answer to *what if you are gone*
- **Publishing and rollout across customers** — a canary that halts itself, and version skew across the fleet as the normal state
- **The hosting business** as a commercial option framed and not taken; **OpenBao's real scope** finally appearing — a multi-tenant vendor's secrets — after everything else once assigned to a vault was removed by giving machines identities
---

## Sequencing

The order is dependency-driven, not layer-numbered:

- **М9 before М9** — an appliance has to exist before it can be scheduled onto
- **М9 before М11** — the loop has to work on one box before a scheduler above it, or contested ownership, means anything
- **М11 before М12** — a recorder has to survive its server, inside one cluster, before a layer across several clusters means anything
- **М12 before М13** — observability collects what М9–М12 emit, and its remote observer is a domain service; there has to be a domain to host it
- **М13 before М14** — the vendor module's demo is *the vendor disappears for thirty days*, which can only be demonstrated with instrumentation in place, and its canary halt condition is an alert rule

**Resolved.** Observability sat after the vendor module for three restructures, each time with a note that the vendor module leaned on instrumentation it had not taught. Recognising the remote observer as a *domain service* settled it: observability is domain-level, so it follows the domain directly, and the vendor module inherits alerting rather than presupposing it.
---

## Scale

| Module | Lessons | Numbered |
|---|---|---|
| М8 — Cloud VMS | 8 | 1–8 |
| М9 — EdgeVMS | 9 | 1–9 |
| М10 — ServerVMS | 5 | 1–5 |
| М11 — ClusterVMS | 5 | 1–5 |
| М12 — DomainVMS | 8 | 1–8 |
| М13 — Observability | 4 | 1–4 |
| М14 — VendorVMS | 5 | 1–5 |

**39 lessons**, or a full semester. The count moved three times: down from 49 when collapsing the layer above the domain removed four lessons of redundancy; up one when the licence system, which had been a bullet, turned out to be a lesson; and down by seven when М8's fifteen short lessons were merged into eight — its first twelve became five multi-part lessons, one per original sub-module, and its last three stayed as they were. **Each module numbers its lessons from 1**; a reference into another module always carries the module: *М9 Lesson 6*, never a bare number. М9–М14 are each a genuine module rather than an appendix.

**М12 is now the largest at eight lessons**, with a visible seam between the domain's *structure* (1–5) and the domain *looking after itself* (6–8). If it needs splitting, that is where.

---

## Deliberately out of scope

- **Analytics and inference at depth.** М11 attaches detectors; it does not teach computer vision
- **High availability of a single-box site.** One box, replaced not clustered — a second server is sold for capacity or for failover, never bolted on to make one box redundant. Failover *between* servers in a cluster is very much in scope: М11 Lesson 4 reschedules a worker off a dead server, its cameras go with it because they are assigned to its name, and the module says plainly what does not fail over — the resource, and the footage on that server's disks
- **Multi-tenancy.** One operator organisation per deployment
- **The cloud side.** М8 covers KVS; nothing here builds a SaaS control plane

---

## Open questions

1. ~~**The BUSL decision, taken once.**~~ **Resolved** — the Additional Use Grant permits this product; the risk is the analytics-plugin roadmap, not the appliance. See the licensing section above. What remains open is a counsel review of that one question
2. **Does the vendor run a MASA?** BRSKI is unimplementable without one, and it is a permanent operational commitment — a signing service that must outlive every appliance shipped
3. ~~**Observability's position**~~ — resolved: domain-level, directly after М12. See the sequencing section
4. **Does the product ship a database HA option?** [`where-the-database-lives.md`](./М12_DomainVMS/where-the-database-lives.md) settles the architecture — each recorder owns its configuration, the domain keeps a directory — but whether HA is offered for the directory, and priced, is commercial

**Resolved since the first version of this plan:**

- ~~Where inference runs~~ — a deployment question, not a schema one. Opaque worker config means the controller is unchanged whether inference runs on the appliance, at the camera or in the cloud (М11)
- ~~Where the write API belongs~~ — built on every recorder in М12 Lesson 3, unauthenticated and marked as such; authentication arrives one lesson later from the domain signer, and enrollment replaces the hand-provisioned credential in М12 Lesson 6
- ~~Lesson numbering~~ — **superseded four times by restructuring, and settled.** Each module numbers from 1: М8 is 1–8 (its first twelve original lessons merged into five multi-part ones), М9 1–4, М9 1–5, М11 1–5, М12 1–8, М13 1–4, М14 1–5. **39 in total.** Cross-module references carry the module name; a bare *Lesson N* always means this module's
- ~~Consul in or out~~ — out, and for a better reason than licensing alone: the product runs a PKI regardless, so a mesh CA is a second hierarchy that buys nothing. The comparison record was retired when OpenBao left the product too
- ~~Identity split across М12 and М14~~ — they were one layer; merged into М12
- ~~Where the domain database lives, and whether hosts replicate it~~ — **there is no domain database.** Each **recorder** owns its configuration in its own Postgres and publishes one way upward; the domain's directory is a Nomad Variable per recorder plus an object per recorder; a **cluster** is the largest set of servers on a reliable network and a **domain** is the clusters under one directory ([`where-the-database-lives.md`](./М12_DomainVMS/where-the-database-lives.md))

---

*Layer model from the architecture discussion; licence terms read from the projects' own LICENSE files, 4 September 2026.*
