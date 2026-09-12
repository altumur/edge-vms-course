# Lesson 1 — What a Cluster Cannot Know

**Module:** DomainVMS — the smallest layer above a set of clusters (Module 12)
**You will build:** a directory of directories over three clusters, an answer that says what it could not reach, and cluster-level placement by reachability — stored, by check-and-set, with a reason.
**Time:** ~150 minutes.

## Why this lesson exists

М11 ended in an unusual place: everything works. A cluster's controller is the only writer of its configuration, its workers fail over by claiming their names back, its resources fence their own zombies, and the cluster knows where every camera is — in one raft, strongly consistent, with nothing above it. So the first thing this module has to do is justify its own existence, and the answer is short: exactly three things stop being knowable the moment there is a second cluster. Where is camera 7, when the cluster you are asking has never heard of it? Which cluster should a new camera go to? And — the one people forget — is the answer you just got *complete*?

The lesson is built around the property that makes those three questions a different module rather than the same module with bigger nouns: **no raft spans clusters.** Inside a cluster the directory has one current answer. Across clusters there is an aggregation over N directories, partial, stale by a bounded amount, and sometimes incomplete. That is the CAP boundary, and it was drawn for you by a network you stopped trusting rather than chosen.

> **What you can verify without hardware.** Everything in this lesson runs against fakes in [`domainvms/`](domainvms/README.md): three clusters as three `FakeVariables` rafts with a switch to make one unreachable. `tests/test_lesson1_directory_and_placement.py` is the lesson's deliverable, and every line of output below came out of it. Nomad federation itself — two regions, one gossip pool, a forwarded read — is `deploy/federation.hcl` and `deploy/verify-bench.sh`, and needs the bench.

## Prerequisites

- **М11 Lesson 5** — the cluster directory: one scan of `vms/workers/*`, and why it is current inside one raft; and the one object a cluster publishes for a layer above — `vms/snapshot`. This lesson aggregates several of those snapshots.
- **М11 Lesson 2** — Variables belong to a region, and the three-stores rule.
- **М11 Lesson 4** — the epoch is per camera, issued by check-and-set from the cluster's raft. This lesson shows why per-cluster raft is precisely the right scope for it.
- **М9 Lesson 5** — `revision` as a monotonic integer. The convergence token here is that idea, one scope up.

## Learning objectives

1. Name the three things a cluster cannot answer, and why each needs the level above.
2. Return *incomplete* as a first-class result, and say why a short list read as complete is worse than no list.
3. Explain what Nomad federation is and is not — what regions share (nothing) and how a read crosses them.
4. Place a camera on a **cluster** by reachability, store the decision with a reason, and prove two placers cannot disagree.
5. Say why a dead cluster is not a placement trigger, and why the epoch needs no domain-wide issuer.
6. Walk the domain's cold start and name the window in which a cluster is recording and invisible.

---

## Step 1 — The three things

Put М11's cluster directory in front of a second cluster and ask it the question it answers perfectly for its own workers:

| | Why a **cluster** cannot answer it |
|---|---|
| **Where is camera 7?** | A cluster answers for its own workers, correctly. Asked about a camera it does not have, it says *no* — and *no* is the wrong word, because it cannot tell **not mine** from **not anywhere** |
| **Which cluster gets a new camera?** | The criterion is **reachability**: which clusters can see this site's network at all. No cluster knows what the others can reach |
| **Is this answer complete?** | Only something that knows how many clusters exist can say a result is partial |

Notice what is *not* in the table. Lookup within a cluster, placing a camera on a worker, rebalancing between workers, deciding how many workers there are — those are М11's and Nomad's, and a single-cluster customer gets all three with nothing above the cluster. The domain adds the level above; it does not repeat the level below. Keep that boundary in your head for the whole module, because every temptation to "just do it at the domain" is a temptation to build М11 again with worse consistency.

## Step 2 — Federation, and what it does not do

Nomad calls a cluster a *region*, and joining regions is *federation*. Read the two properties that matter before running anything:

- Regions are **fully independent**. They share no jobs, no clients, no state. Nothing replicates between them — not a Variable, not an allocation, not an ACL token's raft entry.
- They are coupled by **gossip**, so a request submitted to any region's servers is **forwarded** to the right region and answered from there. `nomad var list -region south vms/workers/` run against a north server works, because north forwards it — though the domain does not do that: it reads one object per cluster (Step 3).

That is exactly the shape a domain needs and nothing more: each cluster keeps scheduling with the others unreachable, and the domain can *read across* them without *owning* them. `deploy/federation.hcl` is the south servers' configuration: a `region`, `authoritative_region = "north"` (for ACL policy replication, the one thing federation does replicate), and `retry_join` pointing at the other region's servers on the WAN gossip port.

What federation does not give you is the thing people assume: a domain-wide raft. There is none. Which is why, in code, a domain is nothing more than a list:

```python
@dataclass
class Cluster:
    name: str
    vars: Variables          # this region's Variables, reached through forwarding
    objects: ObjectStore     # this region's own object store; the domain never reads restore points from it
    reaches: frozenset       # networks this cluster can see: {"vlan:cctv-a", ...}
    is_domain_cluster: bool  # the one that hosts the domain services — a stated decision
```

`Federation.domain_cluster` raises if zero or two clusters are designated. Which cluster hosts the domain is a deployment decision someone wrote down, not wherever an installer happened to run a job first.

## Step 3 — The directory of directories

`DomainDirectory` reads one object per cluster — `vms/snapshot`, the controller's copy of every camera row with the worker and server it is placed on and a timestamp (М11 Lesson 5) — and merges. The rows themselves stay in each cluster's raft with one writer; what leaves is a copy with an age, and `ages()` shows it. The merge is ten lines; the part that matters is the return type:

```python
@dataclass
class Answer:
    camera: str                # the DOMAIN's name for it: the `ref` it gave the cluster (Step 4)
    worker: str | None
    server: str | None
    cluster: str | None
    searched: list[str]        # the clusters that answered
    unreachable: list[str]     # the clusters that did not

    @property
    def complete(self) -> bool:
        return not self.unreachable
```

Run it over three clusters, then pull one:

```
camera 1 is on w-0 (srv-1) in north
camera 7 is on w-0 (srv-9) in south
camera 50 is on w-0 (srv-20) in cloud
camera 99 is in no cluster of the domain (3 clusters searched)
--- south unreachable ---
camera 7 was not found in the 2 cluster(s) I could reach; south unreachable — not 'not anywhere'
camera 1 is on w-0 (srv-1) in north (and south could not be asked)
```

Read the fourth line and the fifth line together. Both are "not found". The fourth is a fact about the domain; the fifth is a fact about the network, and the sentence says so, because a short list rendered as complete is how a missing-camera investigation closes on the wrong answer and how an access review misses the administrator who kept the site. Even the sixth line — a hit — carries the caveat, because "camera 1 is on w-0 in north" and "camera 1 is on w-0 in north *as far as I can see*" are different claims and the console must never upgrade one to the other.

The one condition the directory refuses to merge is two clusters claiming one camera. That is not a tie for the domain to break; it is a placement failure, and `where()` raises rather than guessing.

**Whose id is it.** Every cluster's controller numbers its cameras from 1 (`vms/next_id`), so two clusters both have a camera 7 and the domain cannot ask by that number. It asks by **`ref`** — the name it gave the cluster when it forwarded the create, an operator field on the camera row (М10) that the snapshot and the workers' heartbeats carry back up. The cluster's id is the cluster's; the domain's ref is the domain's; the tests name three clusters that each have an id 1 and find the right one by ref every time.

## Step 4 — Placement, one level up

М11 Lesson 5 placed cameras on workers by the workers' own capacity, under label constraints. This lesson adds the level above, and the division is about what each level *knows*:

| Level | Decides | On | Because only it knows |
|---|---|---|---|
| Nomad | which **server** runs a worker — and how many workers there are | resources, constraints, the autoscaler's metric | the servers |
| Cluster (М11 Lesson 5) | which **worker** gets a camera | the workers' reported capacity, the server's labels | its own workers' headroom, accurately |
| **Domain** (here) | which **cluster** gets a camera | **reachability** | which clusters exist, and what each can see |

Reachability is the whole reason the level exists. A camera on the warehouse VLAN can be reached from the warehouse cluster and from nowhere else; spare capacity in the cloud cluster is irrelevant. Capacity only breaks ties among clusters that can actually see the camera, and even then the domain does not measure it — it reads each cluster's `vms_headroom` from its console's `/metrics` and believes the answer.

```
101 north | only cluster reaching vlan:a | rev 1
102 south | most headroom (40.0) among 2 reaching vlan:b | rev 2
refused: camera 103: no cluster in the domain reaches vlan:z
```

Three things to notice. The refusal says *the domain cannot reach it*, never *cluster X is full* — the same rule as М11's *the system is full*. The reason is stored with the decision: `domain/placement/102` in the domain cluster's Variables reads

```
{'cluster': 'south', 'reason': 'most headroom (40.0) among 2 reaching vlan:b', 'at': '1757500000.0', 'rev': '2'}
```

so that at three in the morning *why is camera 102 in the south cluster* is a row with a reason and a time, not an inference. And placing camera 102 again changes nothing: placement is stored, not derived.

## Step 5 — Two placers, and why that is fine

Nomad's `count = 1` is not exactly-one during a reschedule: a partitioned server may still be running the old placement service while the new one starts. Two placers could give one camera two clusters. The domain's answer is not "make sure there is one" — it cannot — but the same answer М11 gave for the epoch: **correctness comes from how the write is made, never from how many instances Nomad promises.**

```python
def _store(self, camera, cluster, reason, retries=5):
    for _ in range(retries):
        items, idx = self.vars.get(self.path(camera))
        if items and items.get("cluster"):          # somebody placed it while we thought
            return ClusterPlacement(...)             # agree with them
        try:
            self.vars.put(self.path(camera), {...}, cas=idx)
            return pl
        except Conflict:
            continue                                 # the other placer won; re-read
```

The test runs two placers with *opposite* preferences — one thinks north has more headroom, the other south — placing forty cameras concurrently, and asserts every camera ended in exactly one cluster. Whoever won each CAS, both placers agree afterwards, because the loser reads the winner's row instead of insisting.

## Step 6 — What is not a trigger

Only place a camera when you must: it is new, or an operator asked. Two events look like triggers and are not.

**A dead server** is not one — Nomad brings the worker back under the same name and its cameras follow it; that was М11's whole point. **A dead cluster** is not one either, for the opposite reason: those cameras are on that cluster's network and their footage on its resources. Nothing above can heal that, and re-placing them elsewhere produces workers in another cluster trying to reach a dead VLAN — busy, failing, and hiding the real fault behind thirty camera alarms.

```python
try:
    p.place(CameraSite(8, "vlan:b"), unreachable={"south"})
except Refused as e:
    # camera 8: the only cluster(s) reaching vlan:b (south) are unreachable; not placing elsewhere — nothing else can see it
```

`ClusterPlacer.rebalance_across_clusters` exists only to raise `NotImplementedError` with the sentence from М11: a worker never crosses a cluster. The domain's job when a cluster dies is **honesty, not recovery**: report it as unreachable (distinct from its workers being unhealthy — you do not know which), show what is *unavailable rather than lost*, and refuse to move anything.

And the level below does its own part without being asked: `test_the_cluster_then_places_on_a_worker_and_the_domain_never_named_one` has the domain pick south by reachability, forward the create with a `ref` and a label, and south's controller put the camera on a worker whose server sees that VLAN — with the server in its reason. The domain stored `domain/placement/7 {cluster: south}` and nothing about workers, because it knows nothing about them.

## Step 7 — The epoch needs no domain

A worry surfaces at this point: the fencing token from М11 Lesson 4 is issued from a per-cluster raft, and now there are several rafts. Does the domain need an issuer?

No, and the reason is worth saying out loud because it looks like luck. The epoch only ever needs to be monotonic *for one camera*, and a camera lives in exactly one cluster for its whole life — its workers never cross one. So per-region raft is not a compromise; it is precisely the right scope. The failover rule was chosen in М11 for archive locality; it happens to make the fencing token's scope correct as well. When two independent arguments land on the same boundary, the boundary is usually real.

## Step 8 — Cold start

М11 Lesson 4 walked a worker's failover. A domain's *first* start has a step that sequence never had: before the signer runs, no server in the domain can present a certificate. The order is

```
Nomad up (its own install-time TLS) → the signer scheduled in the domain cluster
→ certificates issued → the clusters' consoles and agents come up; snapshots and heartbeats become visible
```

Name the window: between "Nomad up" and "certificates issued", every cluster is **recording** — that is the whole design — but cannot yet be seen by anything above it. Nothing in that sequence is allowed to depend on a cluster, because a student who has not seen it draws a signer that reads its configuration from a cluster whose console needs a certificate from the signer. `Signer.__init__` is the concrete form: it loads its keys from `domain/signer` in the domain cluster's raft or creates them on first start, and reads nothing else.

**Deliverable:** three clusters, one directory; `where()` finds a camera in each; make one cluster unreachable and show the console saying **what it does not know** rather than a shorter list. Then place a camera on each network, read the reason back from the Variable, race two placers, and show a dead cluster refusing to become a trigger.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `where()` says *in no cluster of the domain* for a camera you know exists | The cluster holding it answered — with a snapshot that does not list it yet: its controller publishes every five seconds, and `ages()` says how old the copy is. Or you asked by the cluster's id instead of the domain's `ref`. Not a domain fault. |
| `where()` says *unreachable* for a cluster that is up | The forwarding path: the region name in the request does not match the server's `region`, or WAN gossip is not established (`nomad server members` shows one region). |
| Placement always picks the same cluster | Every candidate reports the same headroom and the tiebreak is by name. Fine — or the headroom callback is returning a constant, which it does by default. |
| `place()` raises `Conflict` after five retries | Something other than a placer is writing `domain/placement/*`. Find it; the placement service is the only writer of that prefix (`deploy/signer-policy.hcl`). |
| `domain_cluster` raises | Zero or two clusters have `is_domain_cluster=True`. It is a decision; make it once. |

## Recap

- A cluster cannot tell *not mine* from *not anywhere*, cannot know what other clusters reach, and cannot know whether an answer is complete. Everything else is М11's.
- **No raft spans clusters.** The domain's directory is an aggregation: partial, stale by a bounded amount, sometimes incomplete — and `Answer` carries the incompleteness as a field, never as an absence.
- Federation shares nothing and forwards reads. That is enough.
- Placement at the domain is by **reachability**, stored with a reason, written by CAS — so two placers agree and nobody counts instances.
- A dead server brings the worker back under Nomad; a dead cluster moves nothing, and the domain says so.
- The domain asks by `ref`, its own name for a camera; a cluster's ids are the cluster's.
- The epoch's scope is the cluster, and that is correct, not lucky.
- Cold start: the signer first, and nothing in the sequence depends on a cluster.

## Exercises

1. Add a fourth cluster that reaches *both* `vlan:a` and `vlan:b`, then place twenty cameras on each network and count where they went. Then make the fourth cluster report headroom 0 and repeat. Say in one sentence what the domain measured and what it believed.
2. Write the one-line console message for a camera that is *found* while another cluster is unreachable, and defend keeping the caveat on a hit.
3. Make `where()` return the first claimant when two clusters claim a camera, run the conflict test, and explain what an operator would have seen a week later.
4. Take the cold-start sequence and reorder any two steps. Say what breaks, and whether it breaks loudly.
5. The design record's open question: should the domain cluster be chosen automatically when the designated one dies? Argue both sides in a paragraph each; then say which one the deliverable of Lesson 7 makes easier.

## Where this is going

You have a directory that knows what it does not know, and a placement service that writes safely and refuses honestly. Neither of them writes anything a cluster can see yet — and that is on purpose. [**Lesson 2**](02-shadow-mode-the-domain-that-writes-nothing.md) runs the domain in shadow: it computes what the directory *would* say, watches what the workers report, and produces a divergence report against your own cluster before it is allowed to change anything.
