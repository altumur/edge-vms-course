# Where the Databases Live

**A decision record for М9_EdgeVMS and М11_ClusterVMS.** Companion to [`worker-and-process-model.md`](../М9_EdgeVMS/worker-and-process-model.md), written in answer to *"a domain exists when its Postgres exists — so is Postgres installed on every host, and how do they sync?"*, and then revised in answer to a second question that corrected it: *if the host needs an event store and an index anyway, why not Postgres locally too?*

It was revised a third time, and that revision **inverted the second verdict below.** The question that did it: *the Node is a Nomad allocation, not a server — its configuration does not change when Nomad moves it from one server to another, so why does anything need to write ownership at all?* That is right, and the design changed because of it.

A fourth revision followed the PKI split — М11 now runs a certificate authority in every domain, which poses a *where does the key live* question this record is the right place to answer — and while making it, **found this record wrong in five places about the epoch.** It said the domain database issues it. It does not: the epoch is a Nomad Variable, and everything downstream of that error is corrected below.

A fifth revision removed the domain database entirely. The objection that did it was aimed at this record's own first verdict — *"a node database, one per Node, and a domain directory, one per domain; both Postgres"* — and consisted of pointing at the last three words. It is wrong, and the reason it took five revisions to see is instructive: the directory does two jobs with nothing in common, and a database is the obvious answer to either one taken alone.

A sixth revision came from redefining the words above it. **Cluster and domain used to span the same machines**, which made the split between М11 and М12 feel arbitrary — so a cluster became *servers on one network you would bet recording on* and a domain became *the clusters under one directory*, different scales rather than different framings. That reclassified the restore point, and with it this record's last remaining claim about what the domain is *needed* for. Corrected below.

Each revision is left visible rather than quietly edited out, because the sequence is the lesson — and this one is the sixth in a row that took state **out** of the domain.

> **Seventh revision — the record is now historical below this line.** The 2c rewrite (М10 ServerVMS, М11 ClusterVMS, and this module) removed the last database this record kept, and retired the word it was written in. Where the text below says *Node* — a process with its own Postgres, its own disk and its own identity that Nomad moved between servers — read the unit that no longer exists. Under 2c a **server** holds the platform's stores, a **resource** (its disks, as a platform job) and whichever **workers** Nomad placed there; a cluster's **controller** owns every camera row in the cluster's raft; a worker holds an **epoch per camera** and claims its own identity by CAS. The verdicts that survive, and what happened to the others, are in [*The seventh revision*](#the-seventh-revision-the-last-database-goes) at the end. The body is left as written because the sequence is still the lesson — six revisions took state out of the domain, and the seventh took it out of the box.

---

## Verdict

Five things. The first was wrong in every earlier draft, in a different way each time.

1. **One database, and it belongs to a Node.** Postgres per Node, holding its configuration, its archive index and its events. **The domain has no database at all** — its directory is a Nomad Variable per Node plus an object per Node in an object store, because those two halves are a small consistent thing and a large opaque thing and neither is a query workload.
2. **The Node owns its configuration and replicates one way upward.** It does not cache someone else's. A Node is a Nomad allocation with stable identity, so when a server dies the Node moves and **its cameras go with it** — nothing rewrites ownership, because ownership never changed.
3. **Destructive operations must never run from state whose authority is unreachable.** Recording continues; deletion does not.
4. **A cluster is the largest set of servers sharing a reliable network; a domain is the clusters under one directory.** The first boundary is physics, the second administration — and **a Node never crosses the first**, which is what makes the restore point cluster-scoped and puts the fencing epoch at exactly the scope Nomad's per-region raft provides.
5. **Storage is chosen by shape, not by habit.** Small and consistent goes in the scheduler's store; large and queryable goes in the database; large and opaque goes in an object store. A record about where databases live turns out to be mostly about what they are *not* for.
6. **The restore point is cluster-scoped, so the domain is not needed to recover either.** Earlier drafts made much of *"needed to recover, not to run."* That was true while the restore point lived at the domain. It does not: a Node restores from **its own cluster's** object store and its own cluster's Nomad Variables, so a dead server fails over with the domain unreachable. **What actually stops without the domain is creation, cross-cluster lookup, rebalance and issuing new certificates** — and none of that is recording *or* recovery.

---

## A domain is not its database

The original question assumed it was, and two drafts of this record agreed. The answer is narrower: **a domain is a set of Nodes on a reliable network.** Configuration lives in the Nodes; the domain holds a directory of them.

Putting Postgres on every Node and synchronising *bidirectionally* would still be wrong, and the reasons are worth keeping because they are what constrains the design:

- **Multi-master** — for a desired-state store, conflicting writes are precisely what must not happen. "Camera 7 belongs to Node A" and "camera 7 belongs to Node B", merged, is the split-brain М11 Lesson 5 exists to prevent
- **A consensus system** — which is what you would end up building, and Postgres is not one

What makes the design work is that **neither is needed, because every row has exactly one writer by construction.** A Node writes its own configuration and nothing else writes it. The synchronisation is one-way and therefore not synchronisation at all — it is publication.

---

## The Node owns; the domain observes

Two earlier drafts of this record had it the other way round — a domain database as the authority, hosts holding read-only caches of their slice. That works, and it is what most control planes do. It is also **strictly less available than it needs to be**, and the reason is worth stating precisely.

**A Node is a Nomad allocation, not a server.** Its identity is stable; Nomad decides which hardware runs it. So camera 7 belongs to Node 3 *permanently*, and a dead server is not an ownership change — it is a relocation. Nothing has to write "Node 3 now lives on Server B" into a configuration store, because no camera moved.

Once that is true, the case for a central configuration authority mostly evaporates:

| | Domain-authoritative (earlier drafts) | **Node-authoritative** (this design) |
|---|---|---|
| Who writes camera 7's config | the controller | **Node 3, the only writer** |
| Editing while the domain is unreachable | impossible | **works — reach the Node** |
| Ownership on failover | rewritten by the controller | **never rewritten** |
| What the domain holds | everything | a directory and a durable copy |

Both designs have exactly one writer per row, so neither has a merge problem. The difference is *where that writer sits*, and putting it at the Node keeps configuration next to the software that uses it.

### What a Node actually holds

- **Its configuration** — its cameras, their settings, its retention policy. **Owned, not cached.** Replicated one way upward for durability and lookup
- **The archive index** — which segment covers which camera over which range. Written constantly; rebuildable by scanning
- **Events** — motion, camera offline, analytics hits, operator actions. High volume, mostly never read
- **Its own certificate and key** — how it proves it is Node 3 on every stream to the directory. Short-lived, renewed from the domain's CA, and the key never leaves the Node

> This does not contradict М9's rule that *desired state is persisted and actual state is derived*. The Node persists desired state because it *is* the authority for it; what stays derived is everything about what is actually running.

### The domain has no database

Earlier drafts of this record put a Postgres in every domain and then spent a page arguing about how to make it highly available. Both the database and the argument were avoidable, and seeing why requires noticing that **the directory is two jobs wearing one name.**

| | **The list** | **The restore point** |
|---|---|---|
| Holds | which Nodes exist, which cameras belong to which, how far behind each is | a durable copy of each Node's configuration |
| Size | kilobytes | megabytes, growing with the fleet |
| Written | on create, rebalance, and each status report | on every publication |
| **Read** | **constantly** — every console page | **once, during a failover that may never happen** |
| Queried? | yes — *where is camera 7* | **never.** Nothing but the Node that wrote it ever parses it |
| Needs | consistency | durability |

A database is a defensible answer to either column alone. It is a poor answer to both at once, and the instinct to have one store rather than two is what produced the Postgres. So:

- **The list is a Nomad Variable per Node.** Already replicated across the Nomad servers, already how a Node learns its identity and its epoch, already ACL'd. Hundreds of bytes per Node: camera ids, and the revision its configuration object is at
- **The restore point is an object per Node.** Write-rarely, read-almost-never, opaque — the definition of an object store's workload, and М9's appliance already speaks to one

**What this buys, beyond one fewer component:**

- **One writer per key becomes a platform property rather than a convention.** A Nomad ACL policy can say Node 3 writes only `nodes/node-3`. In Postgres that guarantee was discipline; here it is enforced
- **The HA argument disappears.** Nomad's raft is replicated because the scheduler needs it to be; object storage durability is what an object store sells. Neither is something a customer has to operate
- **A domain becomes a set of Nodes on a network, literally** — nothing to install, so no box whose loss is the domain's loss

**The cost, stated plainly:** two mechanisms instead of one, an object store that must exist on-premises for air-gapped sites (MinIO, and someone must back *that* up), and a lookup that scans rather than queries — fine at tens of Nodes per domain, and a stated limit somewhere in the low hundreds.

### The Node's own database

Still Postgres, and still one. An earlier draft said SQLite was plenty; that was answering a narrower question than a Node actually poses, because **the index and the events need a real database regardless** — so a Node is running Postgres either way, and putting configuration anywhere else buys nothing.

| | Node database |
|---|---|
| Instances | **one per Node** |
| Written by | **this Node only** |
| Holds | **its own configuration**, archive **index**, events |
| Size | large and always growing |
| Write rate | ~100 rows/sec at a thousand cameras, plus events |
| Backup | index and events reconstructible by rescanning segments; **configuration is not**, which is what the restore point is for |
| Losing it costs | **that Node's configuration**, unless its published object survives |

**Why Postgres locally rather than SQLite**, once index and events are in the picture:

- **Concurrency.** SQLite permits one writer at a time; WAL lets readers run alongside a writer but does not change that. Twenty media workers writing index rows, an event stream, and the worker reading is real contention
- **Partitioning is the decisive feature.** Index and events are both rolling time windows. `DROP PARTITION` against `DELETE FROM` on a table taking a hundred rows a second is not a close comparison, and it makes М9 Lesson 8's retention loop instant instead of a vacuum problem
- **Types that match the work.** `tstzrange` with a GiST index answers *what footage covers this window* directly — which is М8's timeline query — and JSONB carries event payloads that differ per detector
- **One engine, one skillset.** The same `psql`, `pg_dump`, monitoring and client library. Students learn one thing; whoever operates the appliance operates one thing

### What stays outside the databases, and why

**Only one database is left in the design**, so this list is longer than the thing it is an exception to. That is the point: naming the reasons matters more than the list, because the reasons are what tell you where the next one goes.

| | Where it lives | Out of the database because |
|---|---|---|
| **Last-known assignment** | a few hundred bytes on the Node's disk | Postgres is now in the boot path for recording. `After=postgresql.service` costs seconds and is fine; a data directory corrupted by power loss is not, and a Node that cannot record at all is a worse outcome than one recording from a stale hint |
| **The epoch** | a Nomad Variable, check-and-set | It is a **fencing token**, so it must never go backwards — and a database restored from last night's backup hands out numbers it has already issued. Nomad's raft store gives monotonicity and survives losing a server |
| **The directory list** | a Nomad Variable per Node | Small, consistent, and needed by the thing that does the rescheduling. Putting it in a database meant installing one per domain to hold a few kilobytes |
| **The restore point** | an object per Node | Large and **opaque** — no query ever touches it, and durability rather than consistency is the requirement. See above |
| **The domain CA's private key** | offline, or a token, never a table | A key in a database is a key in every backup, every replica, and every `pg_dump` a support engineer ever takes. Issuance is an online service; the key it signs with does not have to be, and the only thing that must be online is the intermediate's *signing capability*, not its storage |

A crash-recovery hint, a correctness primitive, a small consistent index, a large opaque blob, and a signing key. The last is the one most likely to be got wrong by convenience, because a certificates table is genuinely useful — **issued certificates are directory entries; the key that signed them is not.**

---

## The rule this makes general

The course had already made this decision twice without naming it as one principle:

| Layer                  | May be unavailable to | What the layer below does                         |
| ---------------------- | --------------------- | ------------------------------------------------- |
| Controller (М11)       | Workers               | keep recording from cached assignments            |
| Domain (М11)           | Nodes                 | keep editing their own configuration, and renew their own certificates |
| Federation (М12)       | Domains               | keep operating on cached identity and entitlement, and **issue certificates from an intermediate delegated to them** |

> **Every layer is allowed to be unavailable to the layer beneath it, and the layer beneath caches what it needs to carry on.**

Stated once, it becomes a design rule rather than three separate accidents — and it is the thing that makes an edge product different from a datacentre one.

### Caching is not the only way down

The PKI split exposed a second mechanism the rule had been hiding, and it is the better one where it is available.

- **Caching** hands down a *value*. It goes stale, and everything above about grace periods and destructive operations exists to manage that staleness.
- **Delegation** hands down an *authority*. A domain that is its own root does not hold a cached certificate that expires — it **mints fresh ones indefinitely** with everything above it gone, which is why routine issuance never leaves the site. *(Earlier drafts delegated an intermediate from a vendor root; that root was removed because a vendor who can sign your CA can impersonate your domain. The argument survives without it — the domain delegates to itself.)*

Delegation is strictly stronger, and it is available exactly when the thing handed down can be bounded and withdrawn on a schedule — which is what a certificate lifetime *is*. That also says precisely what cannot be delegated: **a secret has no such operation.** You cannot hand a domain a bounded, expiring piece of a password; you can only give it a copy, which is another place to steal it from. So the CA moves down into the domain and the vault does not, and that asymmetry — not a preference about topology — is why М12's appliance holds certificates and runs no vault.

---

## Stale caches, and the operation that must never run from one

Here is the finding that matters most, because getting it wrong destroys customer data silently.

A cached desired state has an age. What is safe to do with an old one **depends on the operation**:

| From a three-week-old cache | Safe? | Why |
|---|---|---|
| Keep recording what you were already recording | **Yes**, indefinitely | Recording is the safe default. A camera still running is never the wrong answer |
| Start recording something new | Questionable | The instruction may have been withdrawn; nothing is lost by waiting |
| **Delete footage under the cached retention policy** | **No** | Irreversible, and the policy may have changed |

**Node-owned configuration removes the worst version of this failure**, and it is worth seeing why. Under a domain-authoritative design an operator raises retention from 7 days to 30 on Monday, the Node loses contact on Tuesday, and on Wednesday it deletes everything older than a week — obeying a policy that was superseded, *silently*, because the edit succeeded at the domain and simply never arrived. Under Node-owned configuration that edit **fails loudly**: the operator cannot reach the Node, so nothing was promised.

The rule survives, relocated to the two places staleness can still occur:

- **Anything a Node caches from above** — entitlement, licence state, placement decisions
- **A Node restored from the domain's copy after failover**, where that copy may be several revisions behind the Node that died

> **Destructive operations stop at the grace period. Recording does not.**

A Node that cannot confirm its retention policy keeps footage and reports that it is doing so. Disks fill; that is a visible, recoverable problem. Deleted footage is neither.

---

## Where the boundaries fall — and there are two of them

Earlier drafts of this record had one boundary and drew it in the wrong place. There are two, they are set by different things, and separating them is what made М11 and М12 different modules rather than two halves of one.

> **A cluster is the largest set of servers that share a network you would bet recording on.** Physics draws this one.
>
> **A domain is the clusters under one directory, one CA and one set of operators.** Administration draws this one.

A campus with three server rooms is **one domain, three clusters**. Fifty stores across a country are fifty clusters, and whether they are one domain or fifty is a question about who administers them, not about the network. Rented servers in a cloud region are a cluster like any other.

### What actually crosses each

Node-owned configuration means a Node does not depend on the network for its *own* settings. What is left:

| Crosses | Scope | Needed for failover? |
|---|---|---|
| Reaching the epoch issuer | **cluster** — Nomad's own raft | **Yes** |
| Restoring a dead Node's configuration | **cluster** — its object store | **Yes** |
| Creating a camera, rebalancing, cross-cluster lookup | domain | No |
| Issuing a certificate to a new service | domain | No |

**Both of failover's dependencies are inside the cluster**, on the same servers it is failing over between. That is why a cluster is a complete product on its own, and why the domain can be unavailable without anything stopping — including recovery, which earlier drafts of this record got wrong.

**A Node never crosses a cluster boundary**, and two independent arguments put it there: its footage is on that cluster's disks, and the epoch comes from a per-cluster raft. When two unrelated reasons pick the same line, the line is usually real.

---

## The high-availability argument this design does not have

Earlier drafts of this record spent a page here choosing between repmgr, pg_auto_failover and Patroni for the domain database. **There is no domain database, so there is no choice to make** — but the page is kept, marked as superseded, because the reasoning is the reason the database went away.

**What losing the directory costs now:** creating cameras, cross-Node lookup, rebalancing, certificate issuance for new services, and — the one that matters — every Node's off-box restore point, so no failover can complete. Recording continues. So does editing a camera at its own Node, and renewing a certificate a Node already holds, up to its lifetime.

> ~~**The directory is not needed to run; it is needed to recover.**~~
> **Superseded.** It is not needed to recover either.

That sentence was true while the restore point lived at the domain, and the sixth revision moved it: a Node restores from **its own cluster's** object store and Nomad Variables, both on the servers it is already failing over between. So the domain is absent from the recovery path entirely.

What remains is weaker still: **Nomad's raft is replicated because the scheduler needs it to be, and durability is what an object store sells.** Both are highly available for reasons that have nothing to do with this product, which is the cheapest kind of availability there is — and neither of them is at the domain.

### Superseded, and kept for the trap in it

| Option | What it needs | Verdict *(when the directory was Postgres)* |
|---|---|---|
| **Single Postgres, backup and restore** | Nothing | The default. Honest for an appliance |
| **repmgr** | A witness node holding no data, as referee before a standby promotes | Reasonable when a customer asks for HA |
| **pg_auto_failover** | A monitor node that actively coordinates state changes | Same class, arguably simpler to reason about |
| **Patroni** | A distributed configuration store — etcd, Consul, ZooKeeper or Kubernetes | **The trap** |
| Domain database at the vendor | A working uplink | Never. The product must work with the vendor gone |

**The Patroni trap is why this section survives its own obsolescence**, because it is exactly the kind of dependency that arrives sideways. Patroni requires a DCS, and the obvious candidates are etcd or Consul. The course had already designed Consul out — Nomad's native discovery is enough for a handful of services per cluster, and mTLS comes from the domain's own CA — so choosing Patroni meant either bringing it back or adding etcd instead, and the database's availability would then depend on a consensus cluster the product otherwise has no use for.

Which is the general lesson, and it applies to more than Patroni: **when making a component highly available requires a second component that is already highly available, ask whether the state could simply live in the second one.** For the epoch, and then for the whole directory, the answer was yes — the Nomad servers were already a replicated consensus cluster, sitting there, doing scheduling. The database was the thing that did not need to exist.

**What is genuinely worth protecting is the CA key**, and it never was a Postgres problem. Losing the directory costs a restore point that every living Node republishes within one publication interval. Losing the domain's signing key means reissuing every certificate in the domain, and losing it *to someone else* means a domain whose entire mutual authentication is forged. Different failure, different mechanism, and none of the table above speaks to it.

---

## Detail is local, summary is domain

This answers М11's third open question, and then turns out to answer more than that.

Suppose the archive index lived at the domain rather than at the Node. During any domain outage recording would continue but **the footage would become unfindable** — the worst kind of failure, because it is silent and to a customer it is indistinguishable from data loss. That alone rules it out, before any argument about write rates.

So split it by who wrote it:

- **The Node that recorded the footage owns its index**, locally. Writing it never needs the network
- **The domain holds a rollup only** — *"Node 3 has camera 7"*, in that Node's Variable beside its camera ids. Coarse enough to stay inside a key-value entry, which is the test for whether something belongs at the domain at all
- **Playback asks the domain *where*, then the Node *what***

Which extends the rule М11 Lesson 2 already teaches — *do not put video bulk on replicated storage; replicate metadata and let footage be local* — one level up: **replicate the summary, not the index.**

### The same shape, three times

| Data | Local | Forwarded to the domain |
|---|---|---|
| Footage | every segment | nothing |
| Archive index | every segment's time range, and which server's disks hold it | which Node has which camera — a list, not a timeline |
| Events | every event | alarms needing acknowledgement, and counts |

> **Detail is local; summary is domain.** Three data types, one rule — worth naming once rather than rediscovering per data type, because the fourth one will arrive eventually.

### Events, which the design was missing

Events — motion, camera offline, analytics hits, operator actions — were not in the plan at all and belong here. They also need distinguishing sharply from М13's material, because they look alike:

> **Events are product data: an operator searches them. Metrics are operational data: an engineer alarms on them.** Different consumers, different retention, different modules.

Most events are never read. A filtered subset — alarms an operator must acknowledge — forwards to the domain so the console can show them without querying every Node. The rest ages out of a partitioned table locally.

---

## What this means

| Criterion | Answer |
|---|---|
| Postgres per Node? | **Yes — that is where configuration lives.** And it is the only Postgres in the design |
| Postgres per domain? | **No.** The directory is a Nomad Variable per Node plus an object per Node. Nothing to install, nothing to make highly available |
| Why not SQLite? | Index and events need a real database anyway, so a second engine is pure cost |
| How do Nodes sync? | One way, upward. Each Node is the only writer of its own rows |
| What survives a domain outage? | Recording, playback, **and configuration edits made at the Node** |
| What stops? | Creating a camera, rebalancing, cross-Node lookup, issuing certificates to new services, and **restoring a dead Node — so failover cannot complete** |
| Where does the directory live? | **The list in Nomad Variables, the restore point in an object store.** Chosen by shape: small and consistent, then large and opaque |
| Where does the epoch come from? | **A Nomad Variable.** A fencing token must never go backwards, and a restored database hands out numbers it already issued |
| Where does the CA key live? | **Not in any of the three stores.** Issued certificates are directory entries; the key that signed them is offline or in a token |
| What happens when a server dies? | Nomad moves the Node; its cameras go with it; **ownership is never rewritten** |
| Where does the domain end? | At the first network you would not bet recording on |
| HA? | **Not a question the design asks any more.** Nomad's raft is replicated for scheduling; object storage durability is its product. Nothing was made highly available *for this* |

**Course changes.** М9 Lesson 5 builds **one** database, owned by the Node, and what М9 builds *is a Node* — so М11 adds Nodes rather than restructuring anything. М11 Part A now carries what makes failover real: what must outlive a server, shared storage versus one-way replication, and fencing. Part B shrinks to the three things a Node cannot know about itself.

**Product recommendation: the Node is the authority for its own configuration and owns the only database; the domain is a Variable, an object, and a certificate authority.** Nothing in that list has to be available for the product to *record*; the directory has to be available for the product to *recover*. Five revisions of this record moved in one direction throughout — **every one of them took state out of a database** — and the last one removed the database.


---

## The seventh revision: the last database goes

*Added 12 September 2026, after the 2c rewrite. Everything above is the record as it stood; this section says what 2c did to each verdict.*

The sixth revision ended with one database in the whole design — Postgres per Node, holding that Node's configuration, its archive index and its events — and called it the only one. The 2c rewrite looked at the three things in it and found that none of them was a database's workload once the *Node* stopped existing as a unit:

| What the Node's Postgres held | Where it is under 2c | Why the shape changed |
|---|---|---|
| **Configuration** — camera rows, revision, placement | **The cluster's Variables**, written by one controller by CAS (`vms/cameras/<id>`, `vms/workers/<w>`) | Ownership was per Node because a Node was the thing that moved. A worker is `count = N` and interchangeable; its cameras are *placed* on it and can be redistributed, so the rows had to be somewhere every worker reads and one writer writes — raft's shape, already there |
| **Archive index** — which segment holds which time range | **A cache on the resource** (М10 `Manifest`, per camera per epoch) and a **SQLite cache on each resource over its own buckets** (`w2cplatform/eventdatabase.py`, inside the resource process; the console merges them) that can be dropped and rebuilt from the buckets | An index that is never the source of truth is a cache, and a cache that admits to being one needs no durability story. The manifest is beside the segments it names |
| **Events** — observations written by the worker | **Buckets on the resource** (`<resource>/<subsystem>/<unit>/e<epoch>/…events.jsonl`), written by the worker holding the unit's epoch, **copied to a peer resource** in the same cluster, restored by the owner | The write path had to survive the epoch moving between workers, and a bucket keyed by epoch does that by construction; a database on a server that just died does not |

So the verdict *one database, and it belongs to a Node* became **no database at all in the product, and one SQLite file that is allowed to be deleted**. Verdict 2 (*the Node owns its configuration and replicates upward*) inverted once more: **the cluster's controller owns configuration**, and the domain reads the controller's snapshot and the workers' heartbeats, never the rows. Verdicts 3, 4 and 5 stand unchanged — destructive operations still never run from unreachable authority (retention is a row in the cluster's raft, applied by the resource's own policy pass), a worker still never crosses a cluster, and every store is still chosen by shape.

**What the summary table looks like now:**

| Criterion | Answer |
|---|---|
| Postgres anywhere? | **No.** Configuration is Variables; the archive index is a rebuildable cache; events are files |
| Who owns camera 7's configuration? | **The cluster's controller** — one writer, CAS, in the cluster's raft. Not the worker recording it, and not the domain |
| What does the domain read? | **Two objects per cluster**: the controller's `vms/snapshot` and each worker's `vms/<w>/heartbeat`. It asks by `ref`, never by a cluster-local id |
| What survives a domain outage? | Recording, playback, edits at the cluster's console, **and failover** — a lapsed slot is inherited and the controller redistributes without anyone above |
| What stops? | Creating a camera across clusters, choosing a cluster, issuing certificates and tokens |
| Where does the epoch come from? | **A Nomad Variable per camera**, moved by CAS when a worker takes the camera. Unchanged since the fourth revision, and now the only fencing token there is |
| What happens when a server dies? | Nomad brings its workers back elsewhere, or other workers inherit their slots and the controller redistributes; the resource's events are restored from its peer's copy; **nothing rewrites a row's owner because the owner was never a server** |
| HA? | Still not a question the design asks. Raft for the rows, a peer copy for the events, and the archive itself stays on the disks that recorded it |

**Product recommendation, seventh form: the cluster's controller is the authority for configuration and there is no database; a server is disks, stores and whatever runs on it; the domain is a reader of two objects, a placement row per ref, and a signer.** Seven revisions of this record moved in one direction throughout — every one of them took state out of a database — and the seventh took out the last one.

---

## Sources

- [PostgreSQL HA: repmgr vs Patroni vs pg_auto_failover](https://tomasz-gintowt.medium.com/postgresql-high-availability-repmgr-vs-patroni-vs-pg-auto-failover-a16fd0bfbc1e) — external dependencies of each, witness versus monitor versus DCS, and the closing argument that a system the team understands beats a more advanced one it does not
- [`worker-and-process-model.md`](../М9_EdgeVMS/worker-and-process-model.md) — camera lifecycle must survive a control-plane outage, which is the rule this record generalises
- М11 Lesson 2 — replicate metadata, let footage be local
- [`module-design.md`](module-design.md) — the epoch issuer, the fencing argument it comes from, and М12 Lesson 4's mTLS on the Node↔directory streams
- [Nomad Variables](https://developer.hashicorp.com/nomad/api-docs/variables) — check-and-set against `ModifyIndex`, which is what makes the epoch monotonic without a second database

*Written 5 September 2026. Seventh revision added 12 September 2026.*
