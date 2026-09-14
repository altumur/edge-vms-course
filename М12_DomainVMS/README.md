# Module 12 — DomainVMS: The Smallest Layer Above a Set of Clusters

[Module 11](../М11_ClusterVMS/README.md) built a cluster: servers close enough to share a network you would bet recording on, and workers that survive any of them dying — with nothing above it. This module is for the deployment that has more than one: three server rooms on a campus, a cluster rented in the customer's cloud, one customer, one directory, one signer. It is the top of the product, and it is allowed to be switched off.

Eight lessons in which a layer is built that answers exactly three questions a cluster cannot — *where is camera 7* across clusters, *which cluster gets a new camera*, and *is that answer complete* — serves people without ever serving them from a worker, issues and rotates its own trust with nobody above to re-issue anything, lets a box join with nobody typing a secret, and can vanish for a month without a camera noticing.

The full design brief is in [`module-design.md`](module-design.md); the decision that removed the domain's database is in [`where-the-database-lives.md`](where-the-database-lives.md).

## The thesis

| | Cluster (М11) | Domain (here) |
|---|---|---|
| Boundary | physics — one LAN, one room | administration — the clusters under one directory |
| Consistency | one raft: strongly consistent | **no raft spans clusters**: an aggregation, partial by design |
| Owns a camera | its controller, the only writer of its rows | never — it knows *where*, and says when it does not know |
| Places | a camera on a worker, by the workers' capacity under labels | a camera on a cluster, by **reachability** |
| When it is down | Nomad brings its workers back on other servers | recording, playback, editing and failover all continue; new logins and new placements do not |

> **The domain is a directory of directories, and it cannot be strongly consistent.** That single fact is what makes this a different module rather than the same one with bigger nouns, and every decision in it follows from it — including the one that says the honest answer to *where is camera 7* is sometimes *not in the two clusters I could reach*.

**A worker never crosses a cluster.** So this module is not about moving work between clusters; it is about knowing where the work is, being honest when a whole cluster is unreachable, and looking after the trust that everything below it chains to.

## Lessons

| # | Lesson | You'll be able to... |
|---|---|---|
| 1 | [What a Cluster Cannot Know](01-what-a-cluster-cannot-know.md) | Name the three things; return *incomplete* as a result; say what federation shares (nothing); place a camera on a cluster by reachability, stored with a reason, safe against two placers by CAS; refuse a dead cluster as a trigger; walk the cold start. |
| 2 | [Shadow Mode: The Domain That Writes Nothing](02-shadow-mode-the-domain-that-writes-nothing.md) | Compute a divergence report; name the six kinds and which are faults; tell slow from stuck by distance and time; drive `unmanaged` to zero; write the exit criterion. |
| 3 | [The API, and What It Refuses](03-the-api-and-what-it-refuses.md) | Assemble the camera list from the heartbeat every worker already publishes; show every row's age and a dead server as **one cause**; forward writes to the owning cluster with idempotency keys and refuse placement at both levels; split serving browsers into a console and a live gateway so a worker never serves one. |
| 4 | [Who May Call It](04-who-may-call-it.md) | Order channel, caller, local check; issue a token naming a subject and nothing else; keep users where nothing about them reaches a worker; carry trust and grants with an agent that writes `domain/*` only; enforce with cluster-local grants that expire; **state and then measure** the revocation window; admit break-glass. |
| 5 | [Packaging, Updates, and the Licence](05-packaging-updates-and-the-licence.md) | One pack for three clusters; push against pull, honestly; the domain's own update server; entitlement cached and graceful with recording never refused; read Nomad's licence. |
| 6 | [Secure Introduction: A Box Joins the Domain](06-secure-introduction-a-box-joins-the-domain.md) | The ladder from defect to hardware; BRSKI's parts and whose each is; enroll zero-touch with a voucher, or with approval done properly; delete the hand-provisioned credential and show nothing stops; say what a TPM proves. |
| 7 | [Lifetimes, Rotation, and Revocation That Works Offline](07-lifetimes-rotation-and-revocation-that-works-offline.md) | Split certificates by job and state *tolerable outage = lifetime − margin*; renew with overlap; rotate a live domain's root with a cross-cert and a retirement date; revoke by expiry; name clock skew; back up and restore the two pieces of state. |
| 8 | [A Cluster You Rent, and a Worker That Does Not Know Where It Is](08-a-cluster-you-rent-and-a-worker-that-does-not-know-where-it-is.md) | Provision a cluster from the customer's cloud account; do the bandwidth and cost arithmetic first; name the three shapes; deploy a worker three ways and diff it identical; say what differs by placement and what never does. |

## The demo the module is built backwards from

Three clusters — two server rooms and one rented region — two hundred cameras, one console. Then:

```bash
# pull the uplink on the south server room
```

The console keeps listing south's cameras, greyed, with their age — *unreachable, last known state* — and `where camera 20` answers *not found in the 2 clusters I could reach; south unreachable*, never *not anywhere*. Nothing in south stops recording; an operator on site still edits a camera at its cluster's console and still logs in with the token they hold. Then pull the domain cluster itself: every other cluster keeps recording, failing over, and serving live view; nobody new logs in; and when it returns — or is re-hosted from the backed-up key and the identity object — the agents pick up the new public key and the console fills back in.

## What you can verify without hardware

Nearly all of it, because a directory is logic and a file. [`domainvms/`](domainvms/README.md) is the eight lessons as one runnable package built on М11's `clustervms/`, and its 41 tests need no Nomad, no Postgres and no browser: the directory of directories over the snapshots М11's real controllers publish, with an unreachable cluster; the domain choosing a cluster and the cluster's controller choosing the worker; placement raced by two threads; the six-kind divergence report on a clock; two hundred rows from four workers' heartbeats and one cause for a dead server; the console over real HTTP; fifty viewers on one tee subscription; tokens, users, grants, the agent's ACL and the revocation window measured; the licence's grace; both enrollment paths against a simulated manufacturer; a thirty-day outage, a root rotation and a cross-cert; the fifty-camera arithmetic and a worker diffed three ways. Every number in the lessons came out of those tests.

**Needs the bench** — two federated regions, a real cloud account, a browser: `deploy/verify-bench.sh` scripts federation, the forwarded read, the agent's ACL against real Nomad and the pulled-uplink console; WebRTC/fMP4/TURN under the gateway's contract; hawkBit; a TPM, which is read about, not run.
