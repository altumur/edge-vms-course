# М12_DomainVMS — Module Design

**The smallest layer that can sit above a set of clusters, be switched off — and be the top of the product.**

[М11](../М11_ClusterVMS/module-design.md) made a *worker* survive its server. It did that without any coordinating layer at all — a cluster's controller owns the camera rows in one raft, a worker claims its identity by CAS and moves wherever Nomad puts it, and a camera's epoch is fenced per camera — so failover rewrites nothing and needs nobody's permission. That is the unusual starting position this module has to justify itself from: **almost everything already works, so what is a domain layer actually for?**

Exactly three things. This module builds them, and spends as much effort on what it refuses to do as on what it does.

> **There is nothing above the domain in the product.** Earlier drafts had a layer above it — a root CA, a federated identity, a fleet inventory, a vault. Every one of those turned out to be either a thing the domain can do for itself, or a thing the *vendor* does across customers. One customer is one domain, because a domain can be as large as their whole estate. What sits above it is [М13 — the vendor's side](../М14_VendorVMS/module-design.md), and the property this module must deliver is the one that makes that module honest: **the product works with the vendor unreachable, or gone.**

> **Domain is not cluster, and a domain is bigger.** М11 built a **cluster** — servers close enough to share a network you would bet recording on, a boundary set by physics. This module builds a **domain**: the clusters under one directory, one CA and one set of operators, a boundary set by **administration**. A campus is one domain with three clusters and three server rooms; a cloud deployment is one domain with one cluster serving fifty sites. Sites and clusters are many-to-many on purpose.
>
> **And М11's rule holds here: a worker never crosses a cluster.** So this module is not about moving work between clusters — it is about *knowing where the work is*, and about being honest when a whole cluster is unreachable.

> **Vocabulary.** *Server*, *cluster*, *domain* and *site* are the four words the course keeps apart ([ARCHITECTURE §1.1](../ARCHITECTURE.md)). *Node* was retired when М10 became ServerVMS: under the 2c shape a server holds the platform's stores, a resource and whichever workers are placed there, and nothing in it is a unit of ownership. Where this record says *worker* it means a М10 `VmsWorker` allocation; where it says *cluster* it means the thing with one controller and one raft.

---

## The thesis

A cluster owning its own configuration answers almost everything, and М11 showed a cluster failing over with nothing above it at all — **a lapsed worker slot is inherited, a camera's epoch moves with it, and nobody's permission is asked.** That raises the fair question of what is left for a domain layer, and the answer is: everything that stops being knowable once there is **more than one cluster.** Exactly three things:

| | Why a **cluster** cannot answer it |
|---|---|
| **Lookup across clusters** — where is camera 7? | A cluster answers for its own workers, in one raft, correctly. It cannot see the other two, and **cannot tell *not mine* from *not anywhere*** |
| **Which cluster** gets a new camera? | The criterion is **reachability** — which clusters can see this site's network — and no cluster knows what the others can reach |
| **Is this answer complete?** | Only something that knows how many clusters exist can say a result is partial. A cluster asked about a camera it does not have says *no*, which is the wrong word |

**Note what is no longer in that table.** Lookup within a cluster, placing a camera on a worker, and redistributing a released worker's cameras are **М11's**, built in its Lesson 5 out of the Variables it already had. They needed no domain then and need none now — a single-cluster customer gets all three with nothing above the cluster at all.

> **The domain is a directory *of directories*** — and unlike the one inside a cluster, **it cannot be strongly consistent**, because no raft spans clusters. That single fact is what makes this a different module rather than the same one with bigger nouns, and every decision below follows from it.

Which is also why this is the first layer in the course **allowed to be unavailable**, and more so than it looks. Recording continues without it, playback continues, an operator still edits a camera at its own cluster's console, and **a dead server still fails over** — М11 put both of failover's dependencies inside the cluster. What stops is cross-cluster lookup, deciding which cluster gets a new camera, and issuing certificates and tokens. None of it is recording, and none of it is recovery.

---

## Decisions taken

| Decision | Choice | Why |
|---|---|---|
| Domain services | **One signer job (CA + token issuer), placement, a read view, an update server, and the remote observer — hosted by one designated cluster — the **domain cluster**; Nomad picks the server** | No controller. Two things cannot be re-provisioned from nothing — the signer's key and, without an IdP, the local user records — and both are backed up beyond the domain cluster for exactly that reason (*The two pieces of state*). |
| The signer's key | **A Nomad Variable in the domain cluster's raft — a software key, on purpose** | A TPM-sealed key pins the signer to one server and defeats failover. A software key can be stolen — and the answer is **short leaf lifetimes**, so a compromise is bounded by hours, plus a key-rotation procedure the module makes students run. Not delegation: there is nobody above to delegate from, and that is the point. |
| Domain correctness | **From CAS writes, never from instance count** | `count = 1` is not exactly-one during a reschedule. Placement is safe against two instances because it writes with check-and-set, not because Nomad promises one placer. |
| Clusters per domain | **One or many. Nomad regions, federated** | A campus is three server rooms and one customer. Regions share no state and gossip-couple, which is exactly what a domain needs: each cluster schedules on with the others unreachable. |
| A dead cluster | **Reported, never healed** | Its cameras are on its network and its footage on its disks. Rebalancing them elsewhere produces workers failing to reach a dead network and hides the real fault. |
| Domain layer | **A directory of directories, and not consistent** | Cross-cluster lookup, choosing a cluster, and saying when an answer is partial. Within-cluster lookup, placement and redistribution are М11's. |
| Directory storage | **No database at all.** A federated read across each cluster's stores | The clusters already hold the answer; the domain aggregates rather than copies. A cluster's archive stays on its resources and the domain never reads it. |
| Camera identity across clusters | **`ref` — an operator field on the camera row, set by the domain when it forwards the create** | Every cluster's controller numbers its cameras from 1, so two clusters both have a camera 7. The domain never asks by that number: it asks by the name it gave, and the worker's status and the cluster's snapshot carry it back. |
| What the domain reads from a cluster | **Two objects, never the rows: the controller's snapshot (`vms/snapshot`, with `ts`) and the workers' heartbeats (`vms/<w>/heartbeat`)** | The rows are the controller's, with one writer; the domain is a reader that is allowed to be stale and must say by how much. The snapshot is *desired* (which worker, why); the heartbeat is *actual* (epoch, phase, `server`). Both are objects because they are frequent and never looked up by key. |
| The camera list the UI sees | **A read model built from the workers' heartbeats and the snapshot — never a fan-out to consoles, never Variables** | Asking N consoles per page waits for the slowest and breaks on the first dead one; putting status in Variables is the three-stores rule broken (frequent, large, replicated to every server). The heartbeat already carries status per camera and the server it runs on, so the list is assembled from what М10 built, held in memory, shown with its age, rebuilt in one pass. Writes never go through it. See *The camera list* below. |
| Who serves browsers | **Never a worker. Two cluster-level jobs — a *console* (UI, API façade, the read model, TLS, token verification) and a *live gateway* (WebRTC/WHEP live, fMP4 playback, TURN, transcoding) — stateless, `count ≥ 2`, placed by constraint where they need a GPU or a public address** | A worker's memory is budgeted for cameras; a browser is numerous, untrusted, on a bad network and opens six tabs. A worker serves *few, trusted, internal* clients — the gateway subscribes to its live tee once per camera, the console reads its heartbeat — and never a viewer. Enforcement stays in the cluster: the gateway forwards the user's token and the cluster's grants decide. Both jobs run in every cluster, so a single-cluster customer has them with no domain; the domain's console is the same code pointed at every cluster. See *Who serves browsers* below. |
| Convergence token | **Monotonic revision**, not token equality | Ordering expresses *distance*; equality only *difference*. See below. |
| Transport | **mTLS, from the domain's own self-signed root** | The cluster↔domain streams carry configuration, grants and status. A credential says who is calling; it says nothing about the channel. **The root is the customer's and stays self-signed on purpose** — a vendor-held root above it would be a vendor that can impersonate the customer's whole trust domain. |
| What a certificate names | **The server** — never a worker, never a hostname it happens to have | A worker is an allocation named by a slot and moved by Nomad; its tokens come from Nomad's workload identity. The physical box is the thing with an identity and a place, and it is what the domain enrols. |
| Authentication | **A hand-provisioned credential per server, marked temporary — until Lesson 6** | The course's existing discipline: the stand-in is named where it appears. Lesson 6's enrollment replaces it with an LDevID issued by the domain's own signer. |
| Human identity | **A token signed by the domain signer; clusters hold the public key, never a password hash** | М9's per-box `operators` table becomes N Alices and N stealable hashes. Verifying a signature needs no network, so this survives the domain being down. **The signer federates to the customer's own IdP** where one exists — one domain, one Alice, and nothing above either. |
| Distribution of identity | **Nothing about a user is ever sent to a worker. What travels is trust: the signer's public key, the revocation list and the cluster's grants, written into each cluster's Variables under `domain/*` by a domain agent — one Nomad job per cluster, allowed to write `domain/*` and nothing else** | A user is the customer's, not a cluster's: replicating Alice into thirty stores makes thirty writers of one key and asks for cross-cluster consistency that does not exist. A public key, a short list of revoked token ids and a cluster's grants are small, rare and consistent — raft's shape — but no raft spans clusters, so something inside each cluster has to write them, and that something is not a worker, not the controller and not the UI. The agent is the same one-writer-per-prefix pattern the cluster's ACLs already enforce for `vms/*`. A role change is a new token; an old one runs to its expiry. |
| Where users live | **Local user records in the domain cluster's Variables under the signer's prefix (`identity/*`), the signer the only writer; an OIDC subject and no secret where the customer has an IdP; per-user UI configuration as objects (`users/<id>/prefs`), no consistency claimed** | Three shapes, three places, no database. A user record is small, rare and must be consistent — raft, beside the signer's key, under the same one-writer-per-prefix ACL. Layouts and bookmarks are larger, frequent, read by one key and harmless if the last write wins — an object. What a cluster needs is neither: only the public key, the revocation list and its own grants. The identity set is published as one object with a pointer so that losing the domain cluster loses users only back to the last publication. |
| Authorization | **Cluster-local grants carrying an expiry, enforced by the cluster's console and gateway** | Enforcement must survive the domain being down, so it cannot be a lookup. Expiry is what bounds the revocation window. Workers never see a grant, a token or a user. |
| Status model | **Positions and reasons kept apart** | Kubernetes shipped a phase enum and then documented why it was a mistake. |

The decisions about the servers underneath — camera ownership, worker identity by claim, fencing and the epoch, capacity as the worker's word, who decides how many workers there are — are [М10's](../М10A_Platform/module-design.md) and [М11's](../М11_ClusterVMS/module-design.md), and nothing here is allowed to contradict them.

---

## Prerequisites

- **М11 entire.** Workers that move between servers, the slot a worker claims, and the epoch that keeps two holders of one camera from corrupting an archive. This module adds a layer above that and must not weaken it.
- **М10 Lesson 4 and М11 Lesson 4** — the worker's heartbeat as an object (`{worker, server, ts, epoch, capacity, headroom, cameras}`), and why it left raft. This module reads that object and adds nothing to it.
- **М11 Lesson 5** — the controller's snapshot, the cluster directory in one scan, and the table of what the controller does not decide; this module aggregates several of those.
- **М9 Lesson 5** — `revision` as a monotonic integer. The convergence token here is that same idea, one scope up.
- **М9 Lesson 9** — positions versus reasons. The console in Lesson 3 is that model at fleet scale.

---

## Why ordering beats equality

Configuration is owned by each cluster's controller and read upward, and the domain must be able to say how far behind it is. The token could be an opaque value compared for equality, or an ordered revision. Ordering wins three ways:

1. **It expresses distance, not just difference.** "Diverged" is an alert you learn to ignore; "diverged by four revisions for forty minutes" is an incident
2. **It permits skip-ahead.** A subscriber offline across revisions 7, 8 and 9 converges straight to 9 without replaying. Edge links go down constantly, so this is not an optimisation
3. **It survives replay and reordering.** A late report carrying a lower revision is ignored rather than ambiguous

**The cost:** you lose proof that one *precise* configuration was applied at one moment. If that must be auditable it belongs in an audit log, not in the convergence token.

---
## Several clusters, one domain

A domain with one cluster is the common case and the boring one. The interesting shape is a campus: three server rooms, three LANs, one customer, one directory — and that is what makes this a module rather than a chapter.

**Nomad calls a cluster a *region*, and joining regions is Nomad federation.** So the mechanism was always here rather than three modules up:

- Regions are **fully independent** — they share no jobs, clients or state, and nothing replicates between them
- They are loosely coupled by a **gossip protocol**, so a job can be submitted to any region, or any region's state queried, transparently, with requests forwarded to the right regional servers
- Which is exactly the property a domain needs: **each cluster keeps scheduling with the others unreachable**, and the domain reads across them without owning them

### What that does to the directory

Inside a cluster the directory is one scan of `vms/workers/*` — the controller's assignments in one raft (М11 Lesson 5). Variables belong to a region, so with several clusters the domain's directory is **a federated read**, not one store: it reads each cluster's snapshot and heartbeats through Nomad's forwarding rather than holding a copy, and answers by `ref` rather than by a cluster-local id. Single-writer per key is unchanged, and so is the limit: tens of workers per cluster, low hundreds of clusters' worth before the scan stops being adequate.

The **archive** is unaffected — it is on each cluster's resources, mirrored to a peer inside the same cluster (М11 Lesson 3), and only that cluster ever reads it.

### And it settles the epoch

**Raft is per-cluster.** Federated regions share no state, so there is no domain-wide raft to issue from — which would be a serious problem if a camera could move between clusters, and is a non-problem because it cannot. **The epoch only ever needs to be monotonic for one camera, and that camera's row lives in exactly one cluster for its whole life.** Nomad's per-region raft is not a compromise here; it is precisely the right scope.

> Worth saying out loud, because it is the kind of thing that looks like luck: the failover rule was chosen for **archive locality** — footage is on the cluster's disks — and it happens to make the fencing token's scope correct too. When two independent arguments land on the same boundary, the boundary is usually real.

### When a whole cluster dies

Not a failover, and the module must not pretend otherwise. Those cameras are on that cluster's network; if the servers are gone, so is the ability to reach the cameras and the disks holding their footage. Nothing above can heal it.

So the domain's job is **honesty, not recovery**:

- Report the cluster as **unreachable**, distinct from its workers being unhealthy — you do not know which
- Show what is **unavailable rather than lost**: footage on those disks still exists and will return
- **Refuse to rebalance its cameras elsewhere.** They cannot be reached from another cluster, so a placement decision would produce workers trying to record cameras on a dead network — busy, failing, and hiding the real fault

---

## Placement, at the level above the one М11 built

М11 Lesson 5 placed cameras on **workers**, by the workers' own measured capacity under label constraints, with the stability rule and its property tests. That work is done and this module does not repeat it. What is added is the level above, and the division is about *what each level knows*:

| Level | Decides | On | Because only it knows |
|---|---|---|---|
| Nomad | which **server** runs a worker, and how many workers there are | resources, constraints, `vms_worker_load` | the servers |
| **Cluster** (М11 L5) | which **worker** gets a camera | the workers' own capacity, under labels | its own workers' headroom and which server sees which VLAN |
| **Domain** (here) | which **cluster** gets a camera | **reachability** | which clusters exist, and what each can see |

**Reachability, not capacity, is the domain's criterion**, and that is the whole reason the level exists. A camera on a warehouse VLAN can be reached from the warehouse cluster and from nowhere else; no amount of spare capacity elsewhere makes another cluster a candidate. Capacity — the sum of the workers' headroom in each cluster's snapshot — only breaks ties among clusters that can actually see the camera.

> **Only place a camera when you must.** Two triggers: the camera is new, or an operator asked for a rebalance. A dead server is *not* a trigger — Nomad brings the worker back, or another worker inherits its slot and the controller redistributes; the camera stays in its cluster. **And a dead cluster is not a trigger either**, for the opposite reason: its cameras cannot be reached from anywhere else, so re-placing them produces workers failing against a dead network and hides the real fault.

**Store the placement; do not derive it** — at both levels. At 3am, *"why is camera 812 in the north cluster"* should be a row (`domain/placement/<ref>`) with a reason and a timestamp; *"why is it on w-1"* is the cluster's row, with the server in its reason. The domain never names a worker: it forwards the create, with the `ref` and the labels, to the cluster's console, and that cluster's controller places it (`test_the_cluster_then_places_on_a_worker_and_the_domain_never_named_one`).

---

## The domain services: no controller, four processes, one key

The word *domain controller* was retired from this course on purpose, because it names something that no longer exists and implies an authority the layer deliberately does not have. What runs at the domain is small enough to list:

| Service | Kind | When it is down |
|---|---|---|
| **The CA** | holds a key, signs certificates | renewal stops — bounded by certificate lifetime minus margin |
| **The token issuer** | holds a key, signs identity tokens | nobody *new* logs in; existing tokens run to expiry; break-glass (Lesson 4) |
| **Cluster-level placement** | stateless computation | new cameras get no cluster |
| **The aggregating read view** | stateless; an in-memory read model rebuilt from the snapshot and heartbeats each cluster publishes | the console sees only its own cluster — from its own object store, by the same code |
| **The remote observer** | stateless, scrapes the other clusters | nobody is told a cluster went silent — [М13](../М13_Observability/module-design.md) |
| **The console** *(every cluster; built in М10/М11 as its own job — one on every server, nothing in front — with a token for the operator's rows and never placement)* | stateless: the UI, the API façade, the read model, TLS, token verification, the cluster's grants | nobody logs in or sees the list; recording and established live sessions continue |
| **The live gateway** *(every cluster)* | stateless fan-out: one subscription per camera to the owning worker's tee, N browser sessions out; transcoding and TURN where needed | live view and playback stop; recording continues |

Every outage in that column is bounded, and none of it is recording or recovery. That is the thesis, made into a table.

The last two rows are not domain services: they run in **every cluster**, because a single-cluster customer needs a screen and a picture with no domain at all, and the domain cluster runs the same console pointed at every cluster's stores. They are in this table because the question they answer — *who serves browsers* — is the one the server-centred modules never had to ask.

**The CA and the token issuer are one service.** They are the same operational thing — a process that holds keys and signs — with one availability story bounded by the same arithmetic and one thing to protect. Run them as one Nomad job with two keys, not two jobs. Calling them *the domain signer* keeps the point visible.

**Creating a user touches no cluster.** The UI talks to the signer's issuer; the issuer writes one record under `identity/*` in the domain cluster's Variables (or, where the customer has an IdP, nothing — the subject is the IdP's) and from then on signs tokens **naming that subject and nothing else**. What the subject may do is not in the token: it is in the *grants* of each cluster, published by the signer under `domain/grants/<cluster>` and carried into that cluster's own Variables by its agent, because enforcement has to survive the domain being down and a token that carried rights would be a lookup that expired with it (the *Authorization* row). So a cluster never needs to know who exists — its console needs to know whether *this* token is genuine, which is a signature check against a public key it already holds, and whether *this* subject has a grant on *this* camera, which is its own table in memory. The three things that do have to reach every cluster — the signer's current public key, the revocation list and the cluster's grants — are carried by **the domain agent**: one small Nomad job per cluster whose only right is to write `domain/*` in that cluster's Variables, the way a worker's only right is to write `vms/<w>/*` and the controller's is `vms/*`. When the domain is down the agent stops updating, the console keeps verifying with the key it has, issued tokens run to expiry, and nobody new logs in — the bounded outage the services table already promises, with the mechanism named. A worker is nowhere in this paragraph, and that is the design.

### One cluster hosts the domain, and that is a decision

A Nomad job runs in one region. Regions share no state, and Nomad does not reschedule across them. So the domain services are hosted by **one designated cluster — the domain cluster** — and if that cluster dies they die with it — there is nothing to fail over *to*, and the thesis says that is acceptable.

Acceptable is not the same as accidental. **Which cluster hosts the domain is a stated deployment decision**, recorded where the directory can report it, and not wherever an installer happened to run the job first. The default is the cluster with the most reliable power and uplink, which is usually the one with the operators in it.

### The two pieces of state, and why they are recoverable

Placement and the read view can be re-provisioned in another cluster from nothing. **The signer cannot: it holds the key every certificate in the domain chains to**, and losing the domain cluster loses it. And where the customer has no identity provider, the signer holds a second thing nothing else can regenerate: **the local user records** — who exists, how they authenticate, which roles they have (see *Where users live* below). Two answers for the key, and they are the delegation principle again:

- **The domain's root is self-signed and it is the top.** There is no authority above it to re-issue from, and that is deliberate: a vendor-held root that signs the customer's CA is a vendor that can impersonate the customer's entire trust domain, and no serious security buyer accepts it. So recoverability comes from **backup**, not delegation: the key is kept somewhere the domain cluster's death cannot reach — another cluster's object store, or offline — and Lesson 7 makes students **rotate** it while the domain runs, because a backup nobody has restored from is a hope
- **Lose it anyway and every server re-enrolls.** That is the honest cost of the customer owning their own trust, and the module says the number — how long a full re-enrollment takes at N servers — rather than leaving it as a feeling

**The user records get the same treatment, by the mechanism М11 already built.** The signer publishes the whole identity set as one object into the domain cluster's object store and then moves a pointer Variable — object first, then the pointer, exactly М11 Lesson 3 — on every change and on a floor. That object is backed up wherever the key is. Re-hosting the domain in another cluster is then a restore with different nouns: the backed-up key, the identity object named by the pointer, then the domain agents pick up the new public key. The RPO for users is the publication interval, and it is stated, not discovered.

### Who decides which server, and what that forces

The operator names the **domain cluster**. **Nomad names the server**, continuously, and nobody types a hostname — the same rule the course has for workers, one layer up. Three things follow, and one is a real decision.

**The domain services fail over within the domain cluster like any allocation.** A server dies; Nomad reschedules the signer job elsewhere in the same cluster, exactly as it would a worker. So the domain's availability is *as good as its domain cluster's* — no better, and no worse. Only a whole-cluster death has nothing to fail over to.

**Which decides where the key lives.** A signer that can land on any server cannot keep its key on a server's disk — that disk just died. It lives in a **Nomad Variable in the domain cluster's raft**: encrypted, ACL'd, delivered to the task, the same mechanism М11 uses for every job's secrets. That is a *software* key, and the alternative should be named to be refused: sealing it in a TPM pins the signer to one server and **defeats the failover it just gained.** The tradeoff — hardware-bound keys cannot move, software keys can be stolen — is settled by lifetimes rather than by preference: **leaf certificates live hours to days**, so a stolen signing key is worth exactly as long as it takes to rotate it, and Lesson 7 makes rotation a drill rather than an emergency. A software key is acceptable *because* everything it signs is short-lived.

**And the two-instances problem is here too.** `count = 1` does not mean exactly one during a reschedule — a partitioned server may still run the old instance, which is М11 Lesson 4's entire subject. Sort the services by what that does:

| Service | Two instances briefly | Why |
|---|---|---|
| Signer | harmless | same key, same signatures |
| Read view | harmless | read-only |
| **Placement** | **a writer** — two placers could give one camera two clusters | **safe anyway**, because a placement is a **check-and-set write** into the domain cluster's raft: the second gets a 409, reads the winner's row and agrees |

> **At the domain, correctness comes from how a write is made, never from how many instances Nomad promises.** Placement is safe because it writes with CAS, not because there is one of it.

**What the operator may still say:** a **constraint**, never a server. *The signer runs on a server with a TPM* or *not on a server carrying fifty cameras* — a requirement Nomad satisfies, which is М9's *physics leaks* table again: the operator names what must be true, the scheduler decides where.

### Cold start, which the rehydration lesson never had to face

М11 Lesson 3 walks a cluster's restart step by step. A *domain's* first start has a step that sequence does not: **before the signer runs, no server in the domain can present a certificate.** The order is Nomad up on its own install-time TLS → the signer scheduled → certificates issued → the agents write `domain/*` → the domain reads snapshots and heartbeats. In that window a cluster records — that is the whole design — but cannot yet be seen by anything above it. Lesson 1 walks this sequence, because a student who has not seen it will build a signer that depends on a cluster that depends on the signer.

---

## The camera list, and where the console gets it

The first screen any UI wants is the one the architecture so far cannot draw: *every camera, with its name, its site, whether it is recording, and when it was last seen* — across workers, and across clusters. The domain's directory does not have it: it answers **where** camera 7 is — which cluster, by `ref` — and nothing an operator would recognise as a camera. Inside a cluster, the rows are the controller's (name, source, labels, `ref`, revision) and the *actual* state — phase, epoch, `server`, `last_seen` — is in each worker's heartbeat, because М10 put it there. So the list is not stored anywhere. It has to be **assembled**, and the question is by whom and from what.

Three ways to assemble it, and the shape rule from М11 Lesson 2 — *small, rare and consistent is raft; large, frequent and never queried by key is an object* — decides between them before any of them is built.

| | What it is | Why not |
|---|---|---|
| **Fan-out** | the console discovers every cluster's console through Nomad's service catalogue and calls N of them per page | Every page waits for the slowest; the first dead cluster either hangs the list or forces partial-response logic into every screen; and each refresh is N network calls. Works at three clusters, fails at thirty. |
| **Status in Variables** | every worker adds its camera phases to its own Variable | Two hundred cameras from fifty workers every ten seconds is a hundred raft commits a second replicated to every server, for data nobody looks up by key. This is precisely why the heartbeat was moved out of Variables (М11 Lesson 4). |
| **The heartbeats** | every worker's heartbeat object already carries its status per camera and the server it runs on; the read view lists `vms/*/heartbeat` per cluster, reads N small objects and holds them in memory | Frequent, medium, never queried by key: **an object**. No worker is called. No raft is written. A dead worker costs nothing but a stale heartbeat. |

The third is the decision, and it is not a new mechanism: **М10 Lesson 4 built it.** A worker's heartbeat is `{worker, server, ts, epoch, capacity, headroom, cameras: [{id, ref, phase, epoch, revision, …}]}`, at `HEARTBEAT_INTERVAL`; this module changed nothing downward. The arithmetic is the reason it is cheap: fifty cameras at roughly two hundred bytes each is a 10 kB object per worker every ten seconds; fifty workers are 50 kB/s into an object store that was sized for the events mirror. Beside the heartbeats the read view takes the controller's `vms/snapshot` — one object per cluster, with its own `ts` — for the *desired* side: which worker each row is placed on and why.

**What the read view is, and is not.** It is a process that lists `vms/*/heartbeat` and reads `vms/snapshot` in each cluster's object store, keeps the result in memory, and serves the list, search and pagination from there — no call to any worker or controller on any request. It is **not a database** (the *No database at all* decision stands): it holds nothing it cannot rebuild from the objects in one pass, and a restart of it is exactly that pass. It is a cache that admits to being one, which is М9's *desired is persisted, actual is derived* one layer up — the heartbeats are actual state, and a copy of actual state is only ever a cache.

**Staleness is shown, never hidden.** Every row carries the age of the heartbeat it came from, and the UI prints it: *as of 8 s ago*. A worker whose heartbeat is older than `lost_after` is shown as *unreachable — last known state*, with its cameras still listed, greyed, from the last object. The console never blocks on a worker, never times out on a page, and never presents a worker's silence as its cameras' absence — which is the *not mine* versus *not anywhere* distinction from the thesis, applied to a screen.

**Grouped by server, because that is how things fail.** The heartbeat carries `server`, so when a server dies its workers go silent *together*, and the read view collapses them into **one cause** — *srv-b: 3 workers silent, 61 cameras* — instead of greying sixty-one rows. A single silent worker on a live server is a different cause, and reads as one.

**Writes never go through it.** When the operator edits a camera from that list, the UI asks the directory *where is this ref*, gets the cluster, and forwards the edit to **that cluster's** console, which writes the row by CAS as its controller's client. The owner does not change; one-writer-per-key is not touched; and the edit shows up in the list when the next heartbeat carries the new revision (`observed_revision`), which is still the only place the UI learns that the edit reached a worker. A read model that also accepted writes would be a second owner of configuration, and the whole of М10 is about there being one.

**It is the same code at both levels.** A single-cluster customer runs the read view against their own object store and gets the whole list with no domain at all — that is the *the console sees only its own cluster* row in the services table, and it is not a degraded mode but the same process pointed at one store. The domain's read view is that process pointed at every cluster's store, which is the first concrete thing in this module that is *a directory of directories*: it merges N lists that were each consistent inside their cluster, marks which cluster each row came from, and says when one of the clusters has gone silent — the third thing a cluster cannot know, made visible on the first screen.

**What this costs the worker:** nothing — the heartbeat it already publishes. **What it costs the module:** the read view stops being *federated reads of Variables plus whatever workers report* and becomes reads of objects, which is simpler, and Lesson 3's deliverable — two hundred cameras across four workers on two servers, kill a server, one cause displayed — is now specified down to where the two hundred rows come from and how old they are allowed to be.

---

## Who serves browsers

Everything up to here has been about workers talking to stores. Nothing has said who talks to *people*: the operator's browser, the sixteen-up wall in the lobby, the investigator scrubbing yesterday's footage. М10 Lesson 7 put a console beside the controller, and it was the right console for the right client — the cluster's own rows and heartbeats, for the cluster's own operator, one read. It never said who is allowed to be that console's client, and the answer matters, because the wrong one turns every viewer into a subtraction from the camera count.

**A worker serves few, trusted, internal clients. Something else serves many, untrusted, external ones.** A worker's memory is `B + n·I` (М9 Lesson 7), budgeted for cameras — that is the `capacity` it reports; a browser is everything a camera is not — numerous, on a bad network, behind NAT, inclined to open six tabs and leave them. The moment a worker serves browsers directly, a slow viewer on a Saturday night competes with recording for the same process, and the autoscaler scales on a number the viewer polluted. So the worker's clients are exactly one: the live gateway, which subscribes to its live tee once per camera. The console never calls a worker — it reads the worker's heartbeat from the object store. The worker never sees a viewer.

That leaves two processes, and they are separate because they fail differently.

**The console** is the web UI's static files, the API the browser talks to, the read model (*The camera list* above), TLS termination, token verification against the signer's public key, and the cluster's grants held in memory (`ClusterGrants`). It is stateless — nothing in it cannot be rebuilt from Variables and objects — so it is an ordinary Nomad job — a `system` job in М11, one on every server, so any server's address is the console and nothing sits in front of it; a retry is answered the same by any instance because the idempotency key is a Variable. Its writes are the controller's rows, written by CAS as any client of the cluster may; at the domain, an edit goes to the directory, then to the owning cluster's console, and *that* cluster's grants decide whether the caller may (the *Authorization* row). The console needs a public key and nothing that fails when the domain is down.

**The live gateway** turns a worker's one live stream into fifty browser sessions: WebRTC (WHEP) for live, fMP4 over HTTP with range requests for playback, a TURN relay when browsers sit behind NAT, transcoding where a browser cannot decode what the camera sends. It is the middle tier М9's process-model note predicted — *demand-driven, not camera-driven; sized by concurrent viewers; hardware-bound* — and it is the one legitimate place a specific server comes back into the design: a gateway that transcodes wants the GPU, a gateway that faces the internet wants the public address, and both are **constraints**, so Nomad places it on that server because of them and nobody types its name. The same rule the domain signer has for a TPM. Playback goes the same way: the resource serves segment files over HTTP (М10's `/segment`) to the gateway, the gateway serves the browser with the token check in front.

**Where the picture comes from.** Two sources, and the cameras decide. Most IP cameras serve several RTSP sessions, so the gateway may open the camera's *sub-stream* directly while the worker records the main profile — one more session on the camera, nothing on the worker. Where the camera cannot (session caps, a saturated uplink, a DriverPack source with no second session), the worker's pipeline carries a `tee` after the parser: one branch into `splitmuxsink` as always, one into a local live endpoint the gateway subscribes to. The live branch is **fire-and-forget** — a leaky queue, never a blocking one — so a stalled gateway loses frames rather than stalling the worker behind it. Either way the gateway finds the worker through the cluster directory (*where is camera 7* → `w-1`) and the worker's heartbeat (*where is w-1's endpoint* → `url`), so a failover moves the endpoint and the gateway reconnects (`WorkerLiveEndpoint`); nobody configures an address.

**The failure arithmetic, which is the point of the split.** Console down: nobody logs in or sees the list; recording continues, and live sessions already established continue. Gateway down: live view and playback stop; recording continues. Worker down: its cameras go dark on the wall until Nomad brings it back or another worker inherits its slot, and the console shows them from the last heartbeat with their age; every other camera is untouched. Server down: every worker on it, as one cause. In no case does a web problem reach a worker, and in no case does a worker's process host a viewer.

**Two lines to hold.** The gateway relays and does not authorise: it forwards the user's token when it subscribes, and the cluster's authoriser (`ClusterAuthoriser` — the signer's public key plus `ClusterGrants`) decides who may see a camera, or enforcement has moved into a process that cannot survive the domain being down. And both jobs run in **every cluster**: a single-cluster customer gets a screen and a picture with no domain at all, and the domain cluster's console is the same code with every cluster's stores behind it — which is the read model's *directory of directories* again, now with a picture under each row.

**What this does to the lessons:** Lesson 3 is the console's lesson already and grows a section for the gateway; its deliverable gains *and a browser watching one of them live through the gateway, with the worker's viewer count still zero*.

**What the course actually ships as a screen** is the platform's `console.html` — one page for every subsystem, built from `/spec` — served by М10's and М11's `SpecConsole`: the camera list from the read model, the timeline from the manifest (merged across resources on a cluster, each span naming its server), and playback of promoted segments one at a time through `/segment/<path>` — the console fetching the bytes from the resource job that has them. That is the single-cluster console of the table above with no gateway: no live view, no fMP4, no TURN, and no viewer ever reaching a worker. The gateway is what turns that page's *play this segment* into *watch this camera*, and it is described here rather than built.

---

## Lessons

*Nine lessons, written — [the index](README.md); Lesson 9 is below with Part two, whose design it was written in, though it belongs to any domain with members that are often away. The three things a cluster cannot know, and the discipline of a layer that may be down. What follows is the plan each lesson was written from.*

### Lesson 1 — What a cluster cannot know

М11 built a directory and did not call it one: scanning the controller's assignments answers *where is camera 7*, in one raft, strongly consistent. **This lesson is what happens to that answer when there are three clusters**, and the change is not one of scale.

- **The consistency boundary, which is the module's real subject.** Inside a cluster there is one raft, so the directory has one current answer. Across clusters there is **no raft at all** — regions share no state by design — so the domain's directory is an *aggregation* over N cluster snapshots: partial, stale by a bounded amount, and sometimes incomplete. **This is the CAP boundary, drawn for you by a network you stopped trusting rather than chosen**
- **Whose id is it.** Every cluster numbers its cameras from 1. The domain asks by **`ref`** — the name it gave the cluster on create, an operator field on the row — and the answer carries the cluster, the worker and the server from the snapshot and the heartbeat
- **Incompleteness as a first-class result.** When one cluster is unreachable, *where is camera 7* may be **unanswerable**, and the honest response is "not found in the three clusters I could reach" — never a short list rendered as though it were complete. A short list read as complete is how an access review, or a missing-camera investigation, goes wrong
- **Two-level placement, and each level decides only what it knows.** The **domain** picks the *cluster*, on **reachability** — which clusters can see this site's network at all. The **cluster** picks the *worker*, on **the workers' own capacity** under labels, which only it measures accurately. Neither level can do the other's job, and that is why this is not the same lesson as М11's
- **The third level, named once.** Nomad places workers on servers and decides how many there are; the cluster places cameras on workers; the domain places cameras on clusters. Students who have now written two of the three stop finding schedulers mysterious
- **What the domain holds and does not.** A federated read across regions — snapshot and heartbeats — plus a placement row per ref in its own raft; each cluster's archive and events, which the domain never touches, because recovery is cluster-local
- **The domain's first start, step by step.** Nomad up → the signer scheduled in the domain cluster → certificates issued → agents write `domain/*` → the domain reads. Name the window in which a cluster is recording and invisible, and say why nothing in the sequence is allowed to depend on a cluster
- **Why ordering beats equality**, from the section above: replica lag across clusters is a *distance*, and "four revisions behind for forty minutes" is an incident where "diverged" is an alert you learn to ignore
- **Opaque config** — the domain stores and forwards what it does not parse, which is what lets a new subsystem ship without touching it
- **Where the domain ends:** at the first network link you would not bet *administration* on. Not recording — recording stopped being the test when the cluster boundary took that job

**Deliverable:** three clusters, one directory; find a camera in each by ref; then make one cluster unreachable and show the console saying **what it does not know**, rather than a shorter list.

---

### Lesson 2 — Shadow mode: the domain that writes nothing

The course's own convention — the stand-in before the real thing — at the top layer.

- The domain computes what the directory *would* say, observes what the workers' heartbeats report (`WorkerReport`, keyed by ref), emits a divergence report, and changes nothing

| Kind | Meaning | Fault? |
|---|---|---|
| **Lagging** | behind, within grace | No — normal |
| **Stalled** | behind past grace, progress static | **Yes** — the real "it didn't take effect" |
| **Orphaned** | a camera in the directory no worker claims | Yes |
| **Unmanaged** | a worker recording something the directory does not know about | In shadow mode, **a measurement, not a fault** |
| **Conflict** | two workers claim one camera under the same epoch | Always — a fencing or placement failure |
| **Stale epoch** | a report under a superseded epoch | The fencing rule catching a writer that should have stopped |

- **The one number: `unmanaged == 0`** — anything running the model does not describe is a gap in the model, and driving it to zero *is* the design work
- **Slow versus stuck:** how long diverged, and whether progress moved

**Deliverable:** a divergence report against the student's own cluster, and a written exit criterion for switching the domain into write mode.

---

### Lesson 3 — The API, and what it refuses

- The read view: the directory list merged with the heartbeats the workers publish and the controller's snapshot (*The camera list* above), **grouped by failure domain**, so a dead server reads as one cause. There is no join to write — it is a merge in the API process, which is what a directory of tens of entries permits
- **Positions and reasons.** `phase` says where an object is; conditions say why it cannot get further. Kubernetes shipped the phase enum and then documented why it was wrong
- Write API: camera CRUD with **idempotency keys**, forwarded to the owning cluster's console, and **what it refuses** — a client may not set `cluster`, `worker`, `server`, `placement`, `epoch`, `observed_revision`, `phase` or `revision`
- **Detectors.** Another subsystem with its own opaque config and its own controller; the domain does not change. So **where inference runs is a deployment question**, not a schema one
- **Who serves browsers** (the section above): the console and the live gateway as two cluster-level jobs; the worker's only client is the gateway; the gateway relays the token and the cluster's authoriser decides; WHEP for live, fMP4 for playback; the `tee` with a leaky queue, or the camera's sub-stream
- The API is unauthenticated, and the lesson says so

**Deliverable:** the console showing two hundred cameras across four workers on two servers; kill a server; one cause displayed. And a browser watching one of them live through the gateway, with the worker's own viewer count still zero.

---

### Lesson 4 — Who may call it

Lesson 3 forwarded writes to every cluster's console. That is **N endpoints where there used to be one**, and the module has to say what protects them before it moves on.

- **The surface, counted honestly.** Cluster-owned configuration is why an operator can edit a camera while the domain is unreachable — and it is also why the thing to protect is now per-cluster. This is a real cost of the design, and it belongs next to the benefit rather than three modules later
- **The channel, before the caller.** Every stream in this module — configuration upward, grants downward, status both ways — runs **mTLS**, from the **domain's own self-signed root**. It is hand-provisioned here in the sense that a student runs `openssl` to make it, and **it is not a stand-in** — this is the customer's root, permanently, and Lesson 7 gives it lifetimes and rotation. The certificate names the **server** — the physical box — never a worker: a worker is an allocation named by a slot and moved by Nomad, and its tokens are Nomad's workload identity. Certificates are short-lived and renewed against this root, so renewal never reaches outside the domain — which is what lets the channel keep working with everything above it gone
- **Authentication, hand-provisioned and marked temporary.** A credential per server, exactly as М8 hand-provisions AWS keys and М9 a database password. **Lesson 6 replaces it** with an LDevID the box earns by enrolling, and the replacement is that lesson
- **Grants are cluster-local, carried by the agent.** Each cluster holds *subject X may do Y on camera Z until T* under `domain/grants` in its own Variables, and its console and gateway hold them in memory. Enforcement is a local read — no lookup, no token exchange — which is the only way authorization survives the domain being down. It also **partitions privilege**: a compromised cluster's grants are that cluster's, where a central store compromised is total. Workers never see a grant

#### The asymmetry that makes rights different from configuration

| If the write does not reach the cluster | Result | Visible? |
|---|---|---|
| A camera edit | records the old way | **Yes** — you can see it |
| A **grant** | the operator cannot get in | Yes — they complain |
| A **revoke** | **the removed administrator keeps the site** | **No** — and they have every incentive not to mention it |

Configuration staleness is benign and self-announcing. Revocation staleness is silent and adversarial, and worse, its window is **unbounded** — it lasts until someone successfully reaches that cluster, which may be weeks.

#### Expiry is the revocation mechanism

- A grant carries `valid_until`, renewed on the same stream the agent already carries the public key on. A cluster whose agent cannot renew lets its grants lapse
- That converts an unbounded window into **a number the product states**, exactly like the certificate lifetime this lesson already picked, and the snapshot age from Lesson 3
- **The tension, and it has no clean answer:** short renewal revokes fast and locks an operator out of their own site during a long outage; long renewal is the reverse. The lesson makes students pick a number and defend it
- Rights are therefore not special — they are one more thing *cached from above with an expiry*, governed by the rule this module already applies to entitlement and placement

#### The other credential: М9's `operators` table, times N

М9 Lesson 9 put a login on the console, against a local `operators` table holding a password hash. On one box that was right. **On N clusters it is a defect**, and naming it is this lesson's second half.

Four clusters means four accounts for one person, four passwords she will make identical, and four hashes an attacker can take. Worse, it breaks the rule this lesson just established: **a grant expires and the account does not.** Revoke Alice's grants and her credential still authenticates at every console; you have bounded the authorization window and left the authentication window unbounded.

The fix is the move this course keeps making, and it is the same one the CA made two bullets ago:

> **Delegate an authority; do not distribute a secret.** A cluster holds the **issuer's public key**, not Alice's password hash. Its console verifies a signature — which needs no network — and then checks its own local grants for the subject that signature names.

So the cluster stores *no human credential at all*:

```
Alice ──▶ domain identity service ──▶ short-lived signed token (subject: alice)
                                                │
                                                ▼
                          cluster south's console: verify signature (public key, offline)
                                                  check expiry
                                                  look up domain/grants for "alice"
```

- **N clusters holding password hashes is N places to steal them from. N clusters holding a public key is zero.** That is a security improvement, not just a tidiness one
- **The issuer is the domain signer**, the same job that runs the CA, and it is permanent too. Where the customer already has an identity provider — and enterprises do — the signer **federates to it** over OIDC: Alice authenticates against her employer's IdP, the signer issues a domain token naming her, and the clusters never learn the IdP exists. One domain, one Alice, and nothing above either of them
- **М9's `operators` table is superseded, not extended.** Say so explicitly — a student who keeps it and adds a `cluster` column has built the N-Alices problem on purpose

#### Two lifetimes, and they are not independent

The grant now has `valid_until` and the token has its own expiry, and picking them separately produces nonsense:

| | Too short | Too long |
|---|---|---|
| **Token lifetime** | Alice is logged out mid-incident, and cannot re-authenticate if the domain is unreachable | A revoked employee keeps working until it expires |
| **Grant lifetime** | A site in a long outage locks out its own operator | A revoked administrator keeps the site |

**A token outliving its grant is harmless** — the cluster finds no grants and refuses. **A grant outliving every token is also harmless** — nobody can present a subject. The failure is assuming one covers the other. The lesson makes students state both numbers and say which one bounds the revocation window. *(It is the shorter of the two, and most people answer the token.)*

**The honest residue: break-glass.** Alice is on site, the uplink is down, and her token expired an hour ago. No amount of design removes that case — a local emergency account is what real products ship, and it reintroduces exactly the password hash this section removed. The defensible version is that it is **one account per cluster, audited on every use, alarmed on, and rotated after** — and that the module says this out loud rather than pretending the clean design has no edge.

#### What the domain can and cannot tell you

The directory aggregates grants for review, never for enforcement. And when a cluster is unreachable, the answer to *"what can Alice access?"* is **incomplete** — the console must say so rather than render a short list, because a short list read as complete is how an access review misses something.

**Identity itself is not cluster-local.** Alice is an employee of the customer and exists whether or not any cluster does; storing her *in* cluster south would create N Alices whose records can disagree about who she is. Clusters store grants against a subject; **the domain signer supplies the subject**, from the customer's own IdP where there is one.

**Deliverable:** grant an operator rights in a cluster, then revoke them while that cluster is unreachable — and state, in advance and then by measurement, exactly when their access ends.

---

### Lesson 5 — Packaging, updates, and the licence

- **Nomad Pack**: templating, variables and registries; per-cluster differences without per-cluster forks
- **The honest GitOps gap.** Fleet is pull-based — a site catches up by itself. Nomad Pack driven from CI is push-based; your pipeline must reach each cluster. For flaky links that is materially worse, and the module says so rather than glossing it
- **The domain runs its own update server.** Eclipse hawkBit, pull-based, hosted like every other domain service. Servers poll it; **the vendor publishes to it** and never reaches a server directly. That restores pull on the OS plane, and it is what makes an air-gapped domain updatable at all — somebody carries a bundle to the update server, and the boxes fetch it as if nothing were unusual
- **Entitlement, from the domain's side.** The vendor issues it; the domain **caches** it and degrades on a grace period, exactly like placement and identity. What degrades is decided here — record-but-don't-add-cameras is the usual answer — and the number is stated. Nothing that is already recording stops because a licence server is unreachable. The licence arrives through the same hawkBit as bundles, lives in the domain cluster's Variables, and is verified by every cluster's controller against a key shipped in the product; the issuing side — what the document contains, what it binds to, why there is no revocation — is М14 Lesson 3
- **Reading the licence you just built on.** Nomad Community Edition is under the **Business Source License (BUSL)**: Licensor IBM; production use granted unless the work is offered to third parties hosted or *embedded* to compete with IBM's paid versions; Change Date four years per version, to MPL 2.0. The competitive test is what decides it for a VMS, and the analysis is in [`COURSE-PLAN.md`](../COURSE-PLAN.md) and [`kubernetes-vs-nomad.md`](../М11_ClusterVMS/kubernetes-vs-nomad.md). The Nomad Autoscaler М11 scales workers with is MPL-2.0 and carries none of this
- Acceptance criteria for the module's structural half

**Deliverable:** one pack, three clusters, per-cluster differences; an OS update delivered through the domain's own hawkBit with the vendor unreachable; and a written analysis of what degrades when the licence server is unreachable for a month — and what does not.

---

### Lesson 6 — Secure introduction: a box joins the domain

The hardest problem in the course, and it belongs here because **a box joins a domain** — and the signer that issues its certificate is already running two lessons back.

A device with no secret must obtain one, over a network it does not yet trust, from a service it cannot yet authenticate. Every option is a trade, and М9 removed the easiest: **the appliance image is byte-identical across every unit**, so nothing device-specific can be inside it.

| Approach | How it fails |
|---|---|
| **Shared secret in the image** | One extracted image is every device's identity. This is how vendors get breached; not a trade-off, a defect |
| **Per-device token written at manufacture** | Works, but it is a factory process, a secret database, and a secret in transit — the problem moved to logistics |
| **Hardware root** (TPM 2.0, or a manufacturer-installed certificate) | Strongest. A key that cannot be exported, and with attestation, evidence of *what software is running* — at the cost of a hardware requirement |
| **Registration with human approval** | The device presents itself; an administrator approves it in **the domain's console**. Pragmatic, widely deployed, judgement lives in the approval — but it trusts the network at first contact |

- **The registrar is the domain's.** RFC 8995 says so and the design agrees: it is the door a box knocks on to join *this* domain, it decides yes or no, and it hands the box to the signer for an **LDevID** — a certificate from *this* domain, replacing Lesson 4's hand-provisioned credential. Enrollment is a domain service, hosted like the others
- **BRSKI's vocabulary, because it names the parts precisely:** the *pledge* carries a factory **IDevID**; the *registrar* decides; the manufacturer's **MASA** issues a *voucher* telling the pledge which registrar to trust; the pledge enrolls over **EST** and receives its LDevID. **The only part of that which is not the domain's is the MASA** — the vendor vouching for its own hardware, which is М13's — and the voucher is the single cryptographic thing a customer ever needs from the vendor
- **What the LDevID names: the serial** — the physical box, before it has a role. The server certificate names the server as the domain knows it; the LDevID names the hardware; a box may be re-imaged, re-clustered or re-purposed and its manufacturer's identity should not have to be reissued for any of those reasons. Nothing about a worker is ever in a certificate
- **TPM 2.0:** sealing, attestation, and precisely what attestation does and does not prove
- **Registration-with-approval, built properly** as the shipped fallback: a queue, an audit trail, and an expiry on unapproved requests
- **The ladder:** never a shared secret in an image; approval as the honest start; hardware identity where the box has a TPM; BRSKI when the customer wants zero-touch and the vendor runs a MASA

**Deliverable:** a box enrolls from cold with nobody typing a secret, receives an LDevID from the domain signer, and the enrollment is auditable afterwards. Then the hand-provisioned per-server credential is deleted, and nothing stops.

---

### Lesson 7 — Lifetimes, rotation, and revocation that works offline

The domain's root is self-signed and it is the top. That removes a layer and adds a duty: **nobody above will re-issue anything**, so this lesson is where the domain learns to look after its own trust.

- **The tension, with an arithmetic answer.** Short certificates revoke by expiring but a cluster offline longer than the lifetime goes dark; long ones survive outages and keep a stolen device trusted for months. There is no lifetime good at both — **split the certificates by job**:

| Certificate | Lifetime | Renewed by | Needs anything outside the cluster? |
|---|---|---|---|
| The domain root | years | a rotation drill | — |
| Service-to-service | hours to days | the signer | **never** |
| Device identity (LDevID) | long | the signer, on enrollment and renewal | never |

- **The number to state:** `maximum tolerable outage = certificate lifetime − renewal margin`. Pick lifetimes from the outage you must survive; a product promising thirty days of autonomy cannot issue seven-day certificates
- **Renewal without downtime:** overlapping validity, and reloading without dropping connections
- **Root rotation as a drill, not a disaster.** Cross-signing or an overlap window; the domain keeps running throughout; the old root is retired on a date. Students rotate a live domain's root, because a backup nobody has restored from is a hope and a rotation nobody has run is a plan
- **Revocation is a lifetime problem, not a list problem.** CRL and OCSP both assume you can reach something; let short certificates expire, and treat the device certificate as the one slow case, compensated by entitlement
- **Clock skew**, which breaks certificate validation in ways that look like everything else

**Deliverable:** simulate a thirty-day cluster outage; everything keeps working. Rotate the root under load. Then revoke a device and show it losing access on a schedule stated in advance.

---

### Lesson 8 — A cluster you rent, and a worker that does not know where it is

М11 built clusters from servers in a room. This lesson changes one thing: **where the servers come from** — and proves the software cannot tell.

- **A cloud region is just a cluster.** Rented instances on one provider network satisfy М11's definition exactly as a rack does, and Nomad cannot tell the difference. The domain cluster **provisions** it, using the customer's own cloud account — which is why this is a domain feature and not something above it
- **Deploy М11's worker job three ways** — local servers, rented instances, and split so a site's workers are local while the domain services are not — and diff the artifacts. **They are identical**: the same `vmsworker.nomad.hcl`, the same `count` the autoscaler moves, the same claim of a slot. If they are not, this lesson found a bug in М10 or М11
- **The bandwidth arithmetic, done before the demo:** fifty cameras at 4 Mbit/s is 200 Mbit/s sustained upstream and ~2 TB a day. Most sites cannot buy that, so **recording stays at the edge and operation moves to the cloud** — *mixed* is the shape a real deployment takes, and a cloud-only site is for a handful of cameras with no hardware to install
- **What differs by placement**, and it is a short list: storage class and its cost curve, how the camera's stream reaches the worker, who is paged when hardware dies. **What must never differ:** configuration ownership, the epoch, the certificate chain, the update mechanism
- **A cloud site has no spool.** Its cameras stream over the internet to a worker that writes to its server's resource; an uplink outage is not buffered, it is lost — so **the camera becomes the buffer**, edge recording backfilled over ONVIF when the link returns. Say this to the customer before they choose it
- **Cost as a design input:** six cameras for thirty days is ~5.8 TB, about $130/month on hot object storage and one $150 disk on-prem. The cloud option is not cheaper; it is *operationally simpler*, and a datasheet that implies otherwise loses money per camera
- **Closing the arc with М8.** The course opened renting a cloud VMS from Kinesis. Rebuild that shape here — your workers, your resources, your cloud account — and М9's hand-provisioned AWS credentials are retired by no longer being needed

**Deliverable:** one domain, two clusters — one local, one rented from the customer's cloud account by the domain itself — both recording, both in one directory, and a written bandwidth-and-cost estimate for a fifty-camera site saying which it should be.

---

## Part two — cameras as members of the domain

The design brief that started it is [`КАМЕРЫ-НА-ПЛАТФОРМЕ.md`](../КАМЕРЫ-НА-ПЛАТФОРМЕ.md): a vendor that makes cameras and runs **the same platform inside them**, in two deployments — cameras with servers, and cameras alone, with no server, no NAS, footage on SD cards. The brief first proposed a module of its own, with its own mechanisms. Laid against this module, most of them turned out to be the domain under another name, and this part is what is left.

### Decisions taken

| Decision | Choice | Why |
|---|---|---|
| What a camera is | **A cluster of its own — a cluster of one** | A camera has none of what makes a cluster М11's: no raft, no orchestrator, no unit that can move to another camera. Treat *all* cameras as one cluster and they need one consistent store — raft among cameras, which the brief refuses. Treat each as its own and its store is trivially consistent (its flash, one writer), and the domain's *no raft spans clusters* is exactly the brief's *no consensus*. |
| What joins them | **The domain, unchanged in kind** | Joining is Lesson 6; the one console that edits any camera is Lesson 3's forwarded write; an unreachable camera is Lesson 1's incomplete answer; offline trust is Lesson 7; *the domain can vanish and no camera notices* is this module's thesis — and it is the brief's "primary camera is off, recording goes on". |
| Who writes a camera's row | **The camera, always** | The brief's *adopted* mode moved the row to the server cluster, which needed a routing store, a change stream, and the camera writing into the servers' store to report. With the owner never changing, none of it is needed, and neither is a per-unit ACL: a camera's token is good only in its own one-camera cluster, and a camera writes nothing into anyone else's. |
| The camera's number | **Its cluster-local id stays an integer; the serial is `ref`** | The domain already answers by `ref` (Lesson 1). Making the serial the id would ripple through every integer camera id below — the event index's `cam INTEGER` among them — for no gain. |
| An edit for a member that is off | **Kept, per field, and applied when it is back (Lesson 9)** | `503` is honest and, for a branch on a weak link or a camera that is off more than on, useless. Kept beside the grants, carried home by the agent, applied by the member's own console as the operator, grant checked then; *applied / already / conflict* per field; outcomes matched by version. The owner never changes. It belongs to Part one — nothing in it knows about cameras — and closes it. |
| Shared settings | **One signed object behind a pointer, defaults resolved at read (Lesson 12)** | The brief's immutable signed file is the identity set's publish-then-point, verified against the key set every member already holds. Over 64 KiB, so an object; ordered by `(term, rev)`; never written into rows. |
| A server recording a camera | **The camera's doors in a source book, carried to the recording cluster (Lesson 13)** | The recorder must not depend on the domain; it resolves `ref:` against its own cluster's copy. One recording cluster per camera, stored by the domain like a placement. |
| Alarms across cameras | **Merged by the domain from every door; closed alarm buckets mirrored on a neighbour (Lesson 14)** | The brief's `MergedIndex` over cameras and its mirrors, which had no lesson in the first plan. Closed buckets only, so copying needs no coordination; pulled by the keeper; the plan chosen by the domain, stably. |
| The "primary camera" | **The domain cluster, hosted on a camera, with a term (Lesson 15)** | This module already has one designated cluster that hosts the domain services and fails with nothing to fail over to. On a camera, re-hosting becomes routine; a term makes a returning host step down; the kept edits of Lesson 9 are published beyond the host; what the old host alone held is listed, not lost. |

### Lessons

All written, each with its module and its tests:

| # | Lesson | Code | Tests |
|---|---|---|---|
| 9 | An Edit for a Cluster That Is Off | `domain/pending.py`; `api.py` keeps, `agent.py` carries and applies | `test_lesson9_pending.py` — 9 |
| 10 | A Cluster of One | `domain/device.py` — `DeviceCluster`, `Flash`, `Ram`, the door | `test_lesson10_cluster_of_one.py` — 7 |
| 11 | Hundreds of Small Members | `domain/scale.py` — `Meter`; `readview.py` — one snapshot read, `where`, lanes, back-off; `api.py` — a forward that meets silence | `test_lesson11_hundreds.py` — 7 |
| 12 | Shared Settings Without a Database | `domain/shared.py` — `SharedSettings`, `carry`, `SharedView` | `test_lesson12_shared.py` — 7 |
| 13 | A Stream From Another Cluster | `domain/crossing.py` — `Crossings`, `resolve`, `plan_backfill`; `readview.py` keeps the doors | `test_lesson13_crossing.py` — 6 |
| 14 | Alarms From Every Member | `domain/alarms.py` — `Card`, `MirrorPlan`, `mirror_once`, `DomainAlarms` | `test_lesson14_alarms.py` — 6 |
| 15 | A Domain Cluster of One Node | `domain/term.py` — `DomainHost`, `carry_host`, `find_host`, `rehost`, `handover`, `stranded` | `test_lesson15_domain_of_one.py` — 9 |

What went back into Part one's code for them, all of it small: the read view reads each member's snapshot once (it read it twice), can read in lanes and back off, answers `where`, and keeps each worker's published doors; the API keeps an edit when the owner goes silent between the directory and the forward; the agent carries per-cluster rows (`domain/sources`, `domain/mirrors`), the shared settings, the backup, and the host record — never to a smaller term.

---
## The code, whole

[`domainvms/`](./domainvms/README.md) is the fifteen lessons as one runnable package, built **on** М11's `clustervms/` and М10's `vmsserver/` — the domain reads each cluster's snapshot and heartbeats through М11's object-store adapters, forwards writes to each cluster's console, and holds no database. In its tests every cluster is the real thing: М11's `ClusterController` placing on М10-shaped `ClusterWorker`s over linked in-memory stores, so what the domain reads is what the cluster actually wrote. Every decision above has a file: `federation.py` (`Cluster.snapshot()/heartbeats()`, and an `Answer` that says what it could not reach, by ref), `placement.py` (reachability, CAS, a dead cluster is not a trigger), `shadow.py` (`WorkerReport`, keyed by ref), `readview.py` and `console.py` (the camera list from heartbeats, one cause per dead server), `api.py` (`ClusterConsole`, the forwarded write, `FORBIDDEN_FIELDS`), `gateway.py` (`WorkerLiveEndpoint`, the tee with a leaky queue, one subscription per camera), `tokens.py` / `signer.py` / `identity.py` / `grants.py` / `agent.py` (the signer, users that never reach a cluster, `ClusterGrants` and `ClusterAuthoriser` with expiry, the agent that carries only trust into `domain/*`), `enroll.py` (voucher and approval), `entitlement.py`, `cloud.py` (`render_three_ways`, the worker stanza that is the same everywhere); and Part two's `pending.py`, `device.py`, `scale.py`, `shared.py`, `crossing.py`, `alarms.py` and `term.py`, in the table above. Four Nomad jobs and two ACL policies under `deploy/`. Its tests — 96, 45 of them for Lessons 1–8 — run with no Nomad, no Postgres and no browser, and each lesson's deliverable is a named test.

Two things went back down for it, both operator fields rather than mechanism: `ref` on the camera row (М10 `config.py`, carried in the worker's status and the controller's snapshot), and `events_retention_days` from the same review. Nothing in М11's controller or worker was written for the domain.

---

## Verification plan

**Track 1 — verified in the authoring sandbox.** Nearly all of it, because a directory is logic and a file:

- **Placement is a pure function** — property tests are the natural fit: adding a cluster moves nothing; every camera lands in exactly one cluster and on exactly one worker; no constraint is violated; two placers with opposite preferences agree
- The divergence taxonomy, one-way replication and revision handling, resume tokens, idempotency
- **Grant expiry and the revocation window** — pure logic; Lesson 4's measurement needs a clock and a cluster's grants, no cameras at all
- Streaming behaviour against a fake worker endpoint, in the style of М8 Lessons 5–8
- The mTLS chain, exactly as М9 Lesson 2 built the RAUC chain — and now the whole of Lesson 7: lifetimes, expiry, root rotation with an overlap window, and clock-skew failures, all by issuing short certificates and moving time rather than waiting
- **Enrollment end to end with a simulated MASA** — the registrar, the voucher, EST, the LDevID — and the approval fallback with its queue and audit trail. A student without a TPM reads Lesson 6's attestation section rather than running it, and the lesson says at which paragraph that starts
- The bandwidth and cost arithmetic of Lesson 8, which is a spreadsheet

**Done, in `domainvms/tests/`:** all of Track 1 above except the mTLS chain with real `openssl` (the chain, lifetimes, rotation, cross-signing and skew are done with `cryptography`'s X.509 instead, which is the same chain the servers will verify) — 45 tests for Lessons 1–8 and 51 for Lessons 9–15, run on the author's machine against the real М11 controller and workers. `deploy/verify-bench.sh` scripts Track 2's federation, forwarded-write, ACL and pulled-uplink checks.

**Track 2 — needs the real bench.** Anything that needs several real workers on several real servers: shadow mode against live traffic, the rebalance budget under load, the packaging exercise, and a rented cluster provisioned from a real cloud account. **TPM 2.0** cannot be faked in any way worth teaching.

---

## Open questions

1. **Rebalance trigger.** Operator-initiated only, or scheduled during a maintenance window? The module assumes the former.
2. **How much retention policy is domain design rather than infrastructure?** Schedules, per-camera overrides and legal hold may deserve their own lessons; `retention_days` and `events_retention_days` are cluster rows today, set through the forwarded write.
3. **How short should a grant's lifetime be?** Lesson 4 makes students pick a number and defend it; the product must pick one too, trading an operator locked out during an outage against a revoked administrator retaining access.
4. **What does an air-gapped domain use for object storage?** М11 answered it for a cluster — Variables for the small objects, the peer mirror for events, S3 only when rented or outgrown — and the domain's own objects (the identity set, per-user prefs) fit the same answer. What remains is where the signer's key backup goes when there is no second cluster.
5. ~~**Where does the domain CA run?**~~ **Answered in *The domain services* above** — as one Nomad job with the token issuer, in a designated domain cluster, its key backed up beyond that cluster and rotated on a drill. What remains open is narrower: **should the domain cluster be chosen automatically** when the designated one dies, or is that a human decision on purpose?

**Resolved while designing the module:**

- ~~Does the directory need high availability (HA)?~~ — **answered by removing the thing that would have needed it.** The rows are in each cluster's raft, replicated across servers already there for scheduling; the snapshot and heartbeats are objects, whose durability is their whole product. The repmgr-versus-Patroni discussion this module was heading for does not happen
- ~~Is the domain its database?~~ — no. It has none. Five revisions of [`where-the-database-lives.md`](where-the-database-lives.md) moved in one direction throughout, and the last one removed it; the 2c rewrite then removed the last per-box database underneath it
- ~~Which id does the domain use?~~ — **`ref`**, the operator field it sets on create. Cluster-local ids never leave their cluster, and the shadow, the directory and the read model all key on ref

---

## Sources

- [RFC 8995 — BRSKI](https://datatracker.ietf.org/doc/html/rfc8995) — pledge, registrar, MASA, voucher, IDevID and LDevID; the registrar belongs to the domain
- [Eclipse hawkBit](https://eclipse.dev/hawkbit/) — pull-based update delivery, run as a domain service
- [Nomad Variables HTTP API](https://developer.hashicorp.com/nomad/api-docs/variables/variables) — the `cas` parameter compared against `ModifyIndex`, 409 on conflict, and the 64 KiB item limit
- [Configurable max entry size for Nomad Variables](https://github.com/hashicorp/nomad/issues/14763) — why a limit exists at all: the impact of Variables on a memory-resident raft store
- [Nomad Pack](https://developer.hashicorp.com/nomad/tools/nomad-pack) · [Nomad LICENSE](https://raw.githubusercontent.com/hashicorp/nomad/main/LICENSE)
- [Eliminate Phase and simplify Conditions](https://github.com/kubernetes/kubernetes/issues/7856) — why phase enums were a mistake
- [`where-the-database-lives.md`](where-the-database-lives.md) — five revisions ending with one database in the whole design, and a sixth note on what 2c did to that one
- [`kubernetes-vs-nomad.md`](../М11_ClusterVMS/kubernetes-vs-nomad.md) · `apphost-and-process-model.md`

*Written 5 September 2026. Split from the combined DomainVMS module on 7 September 2026. Rewritten to the 2c shape (workers, resources, one controller per cluster; Node retired) on 12 September 2026.*
