# Glossary

Terms the course uses precisely, and acronyms it would otherwise leave unexplained. Where a term has an everyday meaning and a narrower one here, the narrower one is what the lessons mean.

## The three words the course keeps apart

| Term | Meaning here |
|---|---|
| ~~**recorder**~~ | *Retired (12 September 2026).* М9 Lessons 5–9's recorder: a process with its own database, cameras and archive index, distinct from the box it ran on. Under М10's shape what is on a server is the platform's stores, a *resource* on its disks and the *workers* placed there — and their sum is the *server*, so the fifth word went. Appears in М9 (history) and in the preserved earlier revisions of М12's [`where-the-database-lives.md`](./М12_DomainVMS/where-the-database-lives.md). |
| **Event** | An observation — a detection, a silence, an operator's mark — written by the worker holding a unit's epoch into that unit's bucket on its server's resource: `<subsystem>/<unit>/e<epoch>/<start>Z.events.jsonl`, recording or not. A platform piece any subsystem uses; the VMS's buckets sit beside its footage. Closed into the manifest, fenced by the path, retained by their own days. No controller writes events; each resource process keeps an `EventDatabase` over its own — a cache — and the console merges them (М10 L3 and L9, М11 L3). |
| **Worker** | A DriverPack shard with N cameras assigned — holds the pipeline and routes it; stable identity, movable between servers, stateless but for a spool. What М9's recorder becomes under М11's decision *2c*. 1+ per cluster, by workload. |
| **Resource** (platform job) | One per server, pinned: the platform's `resource` job (`psimplatform/resource.py`) heartbeats what is on the server's disks under `platform/resources/<server>`, serves every subsystem's buckets, mirrors closed ones to the next resource when `platform/mirror` is on, restores them after a replaced disk, retains by each subsystem's `<sub>/retention` rows, and runs the passes subsystems register on it (the VMS: manifests and media). |
| **Resource** | Something bound to a server: the archive on its disks, GPU compute for detectors, a NIC on the camera VLAN. A Nomad `system` job pinned by what the server has; N per cluster; when it dies nothing moves because nothing can. |
| **Controller** (cluster) | The one job per cluster that places cameras onto workers and rebalances on request. Stateless — computation over Variables and objects — correct by CAS, safe at two, and never on the recovery path: when it is down, nothing already running stops. Not to be confused with the retired *domain controller*, which held state. Since М10 Lesson 5 it is the platform's `SpecController` run from the subsystem's `<sub>.subsystem.yaml` — one class for every subsystem; `count = 1` is economy, CAS is the correctness. |
| **Server** | A box with CPUs and disks — and, under М10's shape, everything on it: a replica of the cluster's stores, the platform's *resource* job on its disks, and whichever *workers* the scheduler places there. A Nomad *client*. М10 is ServerVMS: the platform's shape on one server. |
| **Domain cluster** | The one cluster, named by the operator, that runs the domain services — the signer, placement, the read view, the update server, the remote observer. Nomad picks the server inside it. Its raft holds the signer's key and the local user records; its object store holds their backup and the identity object. If it dies the domain services die with it — bounded, and recording does not. |
| **Live gateway** | A worker of the `live` subsystem (М10 Lesson 7): its unit is a camera's fan-out, its capacity is viewers. It subscribes to a worker's RTP tee once per camera and fans out to N browsers over WebRTC (WHEP signalling through the console); the fan-out unit is created by the first viewer and deleted by the gateway after a grace period. `count = N` by viewer headroom; placed by constraint (a public address, a GPU for transcoding), never by hostname. A worker's only viewer, together with the console; a worker never serves a browser. М12 adds TURN, the token check and placement across clusters (*Who serves browsers*). |
| **Console** (cluster) | The cluster-level job that serves the UI: static files, the API façade, the read model built from the workers' heartbeats and the controller's snapshot, TLS, token verification. Stateless; at the domain, proxies edits to the owning cluster's console, whose grants decide. The domain cluster runs the same console pointed at every cluster. Distinct from М9's recorder console (М9 Lesson 9), which serves only these two jobs. Since М10 Lesson 6 it is its own job — one on every server, nothing in front — with its own token — the operator's rows, never placement — and, like the controller, it is the platform's `SpecConsole` run from the subsystem's YAML; a subsystem adds only the routes that are its own (the VMS: the timeline and the bytes). |
| **Domain signer** | The one Nomad job at the domain that holds keys and signs — certificates for machines, tokens for people. The only domain service that cannot be re-provisioned from nothing, and the reason М13's delegated root matters. Hosted by one designated cluster. |
| ~~**Domain controller**~~ | **Retired.** Named a component the design dissolved: its database became cluster Variables and object stores, its scheduler became Nomad, its API moved onto every cluster's console, its restore point dissolved into the cluster's raft. What remains at the domain is a signer, a stateless placement function and a read view — none of them authoritative, which is why the old name misleads. |
| **Site** | Where cameras physically are — a building, a store. The only one of these an operator names. **Sites and clusters are many-to-many on purpose:** a campus is one domain with three clusters and three sites; a cloud deployment is one domain with one cluster serving fifty sites. |

Two more that are easy to blur:

| Term | Meaning here |
|---|---|
| **Cluster** | Servers close enough to share **a network you would bet recording on** — one LAN, usually one server room. What М11 builds, and the boundary set by **physics**. A worker fails over *within* a cluster and **never across one**, because its footage is on that cluster's disks. |
| **Domain** | The **clusters** under one directory, one CA and one set of operators. What М12 builds, and the boundary set by **administration** rather than physics — one customer installation, which may be one cluster or several. A cluster answers *where can this run*; a domain answers *what is supposed to be running, and in which cluster*. **Losing a cluster stops that cluster's cameras; losing the domain stops nothing already recording.** Past the domain you federate rather than build a bigger one. |
| **Federation** | Now means one thing: Nomad regions joined by gossip, sharing no state — which is how М12 builds a domain from several clusters. The *other* federation — independent domains under a shared trust root — was retired with the layer above the domain: there is one domain per customer, and no shared root by design. |
| **Vendor** | The far side of the product's one boundary that is not a network. One customer is one domain, and a domain is the top of the product — so what sits above it is not a layer but a separate organisation. The vendor may vouch for hardware, issue entitlement, publish updates and rent capacity; it must never hold a customer's root, join a box to a domain alone, or stop recording by withholding anything. М14. |
| **Licence** (entitlement) | A signed document from the vendor naming a **domain id** — never a server or a box — with a camera count, features, a validity window and a grace period. Pulled through the domain's update server like a bundle, held in the domain cluster's Variables, verified offline by the domain against a key shipped in the product. Enforced **at admission** (placement refuses a new camera) and never at runtime: a lapsed licence removes the right to grow, not the recording. No revocation — lifetimes only, and a perpetual licence has none. М14 Lesson 3; the consuming side is М12 Lesson 5. |
| ~~**Orchestration** (the layer)~~ | **Retired.** Earlier drafts had a layer above domains that supplied capacity and trust. The domain provisions its own clusters and is its own root; what remained was the vendor. Nomad still *orchestrates* — within a cluster, placing allocations on servers — and that sense of the word survives. |
| **Edge / cloud / mixed** | Where a domain's servers physically are — at the site, rented in a cloud region, or both in one system. A placement decision per site, not three products: a worker cannot tell which it is running on. |

---

## State and correctness

| | |
|---|---|
| **Desired state** | What an operator asked for. Persisted. |
| **Actual state** | What is running right now. Observed, never persisted — anything a system remembers about actual state across a restart is a bug. |
| **Reconciliation** | The loop that closes the gap between the two. A Nomad jobspec plus its scheduler is one; М9's worker is another. |
| ~~**worker**~~ | *Retired (14 September 2026).* М9's name for its worker process — the `Worker` class in `recorder/worker/`: one process, the reconcile loop and up to fifty pipelines. Renamed so that М9's word and М10's (`vmsworker`) are the same word for the same thing. |
| **Revision** | A monotonic, controller-assigned integer per object. *Applied* means `observed_revision >= revision`. |
| **Epoch** | A fencing token: increments on every ownership change, never decreases, issued by exactly one authority. Part of the archive path, so a stale writer cannot name the files it would otherwise corrupt. |
| **Fencing** | Making a stale writer's writes harmless rather than trying to stop them. You cannot stop a zombie from writing. |
| **CAS** — check-and-set | An update that succeeds only if the value has not changed since you read it. Nomad Variables compare against `ModifyIndex` and return 409 on conflict; it is how the course issues epochs. A **lock** without a fencing token is not a substitute. |
| **Split-brain** | Two writers both believing they own the same object. For a key-value store, survivable. For video, corruption — footage has no merge function. |
| **Quorum** | The majority a consensus group needs to agree. Why an odd number of servers, and why a witness node exists. |

---

## Availability and recovery

| | |
|---|---|
| **HA** — high availability | Keeping a service running through the failure of one of its parts, normally with a standby ready to take over. In М11 the question is whether the *directory* needs one. |
| **Failover** | Moving work off something that failed. In this course it happens at two levels: the scheduler moves a **worker** to a healthy server; the domain reassigns **cameras** only when an operator rebalances. |
| **RPO** — recovery point objective | How much recently written data an outage may lose, measured in time. Every boundary in this course is crossed the same way — **a one-way publication with a stated RPO** — whether what crosses is footage (М9's spool), configuration (М11) or status. A number the product states, not a surprise it discovers. |
| **RTO** — recovery time objective | How long recovery is allowed to take. In М11's deliverable it is the seconds between pulling the power and recording resuming. |
| **TTL** — time to live | How long a lease is valid before it expires and the object may be granted to someone else. |
| **Witness / monitor node** | A member of a database cluster holding no data, present only to break ties and prevent split-brain. |

---

## Storage and process

| | |
|---|---|
| **Spool** | Segments recorded locally and waiting to be uploaded, deleted only on acknowledgement. Introduced in М9 so an uplink outage costs visibility rather than footage; М9 puts an index over the same files and they become the archive. |
| **RSS** — resident set size | Memory a process has in RAM. Counts a shared library page once **per process**, so summing it across processes double-counts and inflates the answer. |
| **PSS** — proportional set size | The same, but each shared page is divided by the number of processes mapping it. **The only honest way** to compare one process holding N pipelines against N processes holding one each. |
| **GIL** — global interpreter lock | CPython's lock ensuring one thread runs Python bytecode at a time. Released around C calls, which is why fifty GStreamer pipelines in one Python process work — and re-acquired in every callback, which is why a per-buffer callback does not. |
| **CSI** — Container Storage Interface | The standard by which schedulers attach network storage to workloads. Nomad supports it; М11 Lesson 3 explains why it does not give an appliance unattended failover. |
| **SAN / NAS** | Storage area network / network-attached storage — shared storage reachable by several servers. Expensive, and a shared failure domain. |

---

## Security and identity

| | |
|---|---|
| **Authentication** | Proving *who* a subject is. Federated in М12, so there is one Alice across every cluster and domain. |
| **Authorization** | Deciding what that subject may *do*. Deliberately **not** centralised: grants live in each cluster so they can be checked with the domain unreachable, and carry an expiry that bounds the revocation window. |
| **PKI** — public key infrastructure | The certificate authorities, certificates and revocation machinery that let parties prove who they are. |
| **CA** — certificate authority | What signs certificates. Each domain runs one — the *domain signer* — and **its root is self-signed and the customer's, permanently.** No vendor root sits above it, because a vendor who can sign your CA can impersonate your domain. Recoverability is by backup and rotation, not delegation. |
| **Trust anchor** | The certificate a party has decided to trust without further proof — the top of any chain it will accept. In this design there is exactly one per domain, the domain's own root, and nothing above it. |
| **Cross-signing** | Issuing a second certificate for the same key from a different CA, so a chain validates under both the old and the new anchor at once. How a domain rotates its own root without a flag day — М12 Lesson 7 makes it a drill. |
| **mTLS** — mutual TLS | Both ends of a connection present certificates, so the server authenticates the client as well as the reverse. |
| **CRL / OCSP** | Certificate revocation list / Online Certificate Status Protocol — the two ways to ask whether a certificate has been revoked. Both assume you can reach something, which is why М12 prefers short lifetimes at the edge. |
| **TPM** — Trusted Platform Module | A chip holding a key that cannot be exported, and able to attest what software booted. The strongest answer to *how does a box with no secret get one*. |
| **BRSKI** | Bootstrapping Remote Secure Key Infrastructure ([RFC 8995](https://datatracker.ietf.org/doc/html/rfc8995)) — the standard for secure zero-touch onboarding. |
| **IDevID / LDevID** | Initial / Local Device Identifier — the certificate a manufacturer installs at the factory, and the one a domain issues once the device has joined. |
| **MASA** | Manufacturer Authorized Signing Authority — the vendor-run service that vouches for a device to a domain. Running one is a permanent commitment, which М12 states plainly. |
| **EST** | Enrollment over Secure Transport — how a device asks a CA for its certificate once it has decided whom to trust. |

---

## Licensing

| | |
|---|---|
| **BUSL** — Business Source License | A source-available licence: the source is published, production use is granted subject to conditions, and each version converts to an open-source licence after a **Change Date**. **Nomad is the only BUSL component in the design** — Consul and Vault were designed out — with IBM as Licensor. Permitted for this product by the competitive test; see [`COURSE-PLAN.md`](./COURSE-PLAN.md). |
| **Change Date** | When a given version's BUSL grant converts — four years after that version is published, to MPL 2.0 (Nomad 1.8.0 → 28 May 2028). Not an escape route: a version reaches its Change Date about two years *after* its support ends, so the MPL build is always the unpatched one. |
| **Additional Use Grant** | The clause defining what production use is permitted. Nomad's forbids offering the software to third parties *hosted or embedded* **in competition with the licensor's paid products** — and "embedded" is itself defined relative to *a competitive product*, so embedding alone is not the trigger. A VMS does not overlap Nomad Enterprise, so shipping an appliance with Nomad inside is permitted; see [`COURSE-PLAN.md`](./COURSE-PLAN.md). |
| **MPL 2.0** | Mozilla Public License 2.0 — a weak-copyleft open-source licence; what BUSL components convert to. |

*Summaries of licence terms, not legal advice.*
