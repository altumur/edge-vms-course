# domainvms — М12, whole

The smallest layer that can sit above a set of clusters, be switched off, and be the top of the product — as code. Built **on** М11's `clustervms/` and М10's `vmsserver/` (imported, not copied): the domain reads each cluster's snapshot and heartbeats through М11's object-store adapters, forwards writes to each cluster's console, and never holds a database. In the tests every cluster is the real thing — М11's `ClusterController` placing cameras on `ClusterWorker`s over linked in-memory stores — so what the domain reads is what a cluster actually writes.

```
domainvms/
  domain/
    federation.py     Lesson 1  N clusters, one directory of directories; Cluster.snapshot()/heartbeats(); an Answer, by ref, that says what it could not reach
    placement.py      Lesson 1  which CLUSTER gets a camera, by reachability; stored with a reason, by CAS; a dead cluster is not a trigger; the worker is the cluster's choice
    shadow.py         Lesson 2  the divergence report from WorkerReports keyed by ref: lagging / stalled / orphaned / unmanaged / conflict / stale_epoch; the exit criterion
    readview.py       Lesson 3  the camera list from the workers' heartbeats and the controller's snapshot; age on every row; one cause per dead server
    api.py            Lesson 3  the write façade: idempotency keys; forwards to the owning cluster's console; refuses cluster/worker/server/placement/epoch; 503 not 404 when a cluster is unreachable
    gateway.py        Lesson 3  the live tee with a leaky queue; the gateway that subscribes once and fans out; WorkerLiveEndpoint follows a failover; the worker's viewer count stays 1
    console.py        Lesson 3  the domain console over HTTP, standard library; `python3 -m domain.console`
    tokens.py         Lesson 4  Ed25519 tokens naming a subject and nothing else; a key SET for overlap; a self-pruning revocation list
    identity.py       Lesson 4  users in identity/* (the signer the only writer); IdP subjects with no secret; publish-then-point; prefs as objects; break-glass
    grants.py         Lesson 4  cluster-local grants with expiry (ClusterGrants); ClusterAuthoriser: signature, then its own table, never a network call
    agent.py          Lesson 4  the domain agent: keys, revocations and the cluster's grants into its domain/* and nothing else; `python3 -m domain.agent`
    signer.py         Lessons 4, 7  the domain signer: root, service and LDevID lifetimes, renewal with overlap, root rotation with a trust bundle and a cross-cert, clock skew named
    entitlement.py    Lesson 5  the licence cached in the domain cluster, verified against the product's vendor key, graceful for a stated period; recording never stops
    enroll.py         Lesson 6  pledge, registrar, a simulated manufacturer CA and MASA; the voucher path and the approval queue with audit and expiry
    cloud.py          Lesson 8  the bandwidth and cost arithmetic; М11's worker job rendered three ways and diffed
    pending.py        Lesson 9  an edit kept for a cluster that is off: per field, beside the grants, applied by the member's console, matched by rev
    device.py         Lesson 10 a camera as a cluster of one: its row, a pinned unit, its own epoch, heartbeats in RAM, the door after the first publish
    scale.py          Lesson 11 the Meter: calls counted and link time charged per member; what a pass, an edit and a silence cost
    shared.py         Lesson 12 shared settings: one signed object behind a pointer, carried by agents, ordered by (term, rev), defaults resolved at read
    crossing.py       Lesson 13 a stream from another cluster: one recording cluster per camera, the source book, backfill planned from it and fetched from the card
    alarms.py         Lesson 14 one list of alarms from every member; closed alarm buckets mirrored on a neighbour, pulled; a stable mirror plan
    term.py           Lesson 15 the domain on a camera: a term, a signed backup beyond the host, re-hosting, never a smaller term, what an old host alone held
    runtime.py, signer_service.py   wiring for the real processes (NomadVariables, the object store, HTTP)
  deploy/
    signer.nomad.hcl  console.nomad.hcl  gateway.nomad.hcl  agent.nomad.hcl   the four jobs; constraints, never hostnames
    signer-policy.hcl  agent-policy.hcl                                        one writer per prefix; the agent may not touch vms/*
    federation.hcl                                                             two regions, one gossip pool
    verify-bench.sh                                                            what needs a real bench, scripted
  tests/              96 tests, no Nomad, no Postgres, no browser — about two seconds
```

```bash
python3 tests/run.py                 # 96 tests; finds ../../М11_ClusterVMS/clustervms (or CLUSTERVMS_PATH) and М10 through it
python3 -m domain.console            # CLUSTERS=north=http://nomad:4646|variables://objects,...
```

## What each lesson's deliverable became

| Lesson | Deliverable in the design record | Where it runs |
|---|---|---|
| 1 | three clusters, one directory; find a camera in each by ref; make one unreachable and show the console saying **what it does not know** | `test_lesson1_directory_and_placement.py`: `Answer.sentence()` says *not found in the 1 cluster(s) I could reach; south unreachable — not 'not anywhere'*; the answer carries cluster, worker and server from the snapshot and the heartbeat |
| 1 | placement by reachability, stored with a reason; two placers are safe by CAS; a dead cluster is not a trigger; **the domain never names a worker** | same file: two threads placing forty cameras with opposite preferences agree on every one; `test_the_cluster_then_places_on_a_worker_and_the_domain_never_named_one` has south's real controller put the forwarded camera on a worker whose server sees its VLAN |
| 2 | a divergence report and a written exit criterion | `test_lesson2_shadow.py`: the six kinds from `WorkerReport`s keyed by ref, slow versus stuck by distance and time, `exit_criterion()` |
| 3 | two hundred cameras across four workers on two servers; kill a server; **one cause displayed**; a browser watching live with the worker's viewer count still zero | `test_lesson3_readview_api_gateway.py`: 200 rows from four heartbeat objects, no worker called; `srv-1` dies and `causes()` returns exactly one *server silent* covering both its workers and their hundred cameras; the list from a real М11 cluster is exactly its heartbeats; fifty viewers, one subscription on the tee; the gateway follows a failover to the new worker's `url` |
| 4 | grant an operator rights, revoke while the cluster is unreachable, **state in advance and then measure** when access ends | `test_lesson4_identity_grants_agent.py`: `access_ends()` states the number, the clock proves it; both directions (token outlives grant, grant outlives token) |
| 5 | what degrades when the licence server is unreachable for a month, and what does not | `test_lesson5_entitlement.py`: valid → grace → degraded; `recording_allowed()` has no code path that returns False |
| 6 | a box enrolls from cold with nobody typing a secret, receives an LDevID, the hand-provisioned credential is deleted, nothing stops | `test_lesson6_enrollment.py`: the voucher path and the approval path, a stranger's IDevID and a wrong-domain voucher refused, unapproved requests expiring |
| 7 | a thirty-day outage; rotate the root under load; revoke a device on a stated schedule | `test_lesson7_lifetimes.py`: service certs dark after two days, LDevIDs fine; both leaves valid across the overlap, the old root retired on its date, a peer with only the old root served by the cross-cert; skew named |
| 8 | two clusters, one rented; a written bandwidth-and-cost estimate | `test_lesson8_cloud.py`: 50 × 4 Mbit/s = 200 Mbit/s and 2.16 TB/day → *mixed*; `vmsworker.nomad.hcl` rendered for a rack, a rented instance and a split site differs in datacenter and object store and is byte-identical from the worker's `group` down |
| 9 | an edit for a cluster that is off is kept, not refused, and lands when it is back — as the operator, grant checked then | `test_lesson9_pending.py`: per field against the value last seen; a field changed on site is a conflict shown, not an overwrite; a revoked grant refuses at application; carried twice applies once; an edit made before the last was confirmed is not a conflict (`via`), and an old outcome clears nothing (`rev`) |
| 10 | a camera is a member like a server room | `test_lesson10_cluster_of_one.py`: found by serial on a worker and a server that are the camera; epoch grows every boot; a day of heartbeats costs flash nothing; no placer's keys; the door shut until the first publish, or an edit is a false 404 |
| 11 | the stated limit, measured | `test_lesson11_hundreds.py`: 4 calls and ~575 bytes per member per pass; 50 edits through the directory are 28 550 calls and most of an hour, from memory none; 30 silent members cost 82 s in turn, 5 s in 16 lanes, 1.4 s backing off; back on the list within the ceiling |
| 12 | settings shared without a database | `test_lesson12_shared.py`: delivery named per member; a stranger's signature refused with a matching checksum; never backwards; defaults resolved and no row touched; two editors told; a tree over 64 KiB; the domain off and a camera rebooted |
| 13 | a server room records a camera of another cluster | `test_lesson13_crossing.py`: resolved from the carried book with nothing written into the camera; one recording cluster per camera; six hours with the domain off; a camera that moved meanwhile, found again; backfill fetches what the card still holds and drops the rest |
| 14 | one list of alarms, and a camera that is off answered from a copy | `test_lesson14_alarms.py`: merged newest first; from the neighbour's copy up to when it knows, "none known since"; closed alarm buckets only, twice is once; one pair of 300 moves when a camera is added; a storm is truncated, not the page |
| 15 | the domain re-hosted from one camera to another | `test_lesson15_domain_of_one.py`: the kept edit survives the host; the host record never goes backwards; the old host steps down and lists what it alone held; a forged backup ignored; the wrong key followed by nobody; the second re-host takes term 3; a planned handover strands nothing, is called off cleanly when the target cannot take the backup, and reports a write that slipped past its freeze |

## What the design record says, as code

**No database.** `grep -r "postgres\|sqlite\|CREATE TABLE" domain/` finds nothing. Users are Variables under `identity/*`; the read model is memory rebuilt from objects; placement is Variables; the signer's keys are one Variable. `IdentityStore.restore()` and `Signer.restore()` are the re-hosting: the backed-up key, then the identity object the pointer names.

**The domain reads two objects and never the rows.** `Cluster.snapshot()` is the controller's `vms/snapshot` (with its `ts`); `Cluster.heartbeats()` is every `vms/<w>/heartbeat`. `ReadView.refresh()` reads those and nothing else; `DomainDirectory.where(ref)` answers from them. Nothing in this package reads `vms/cameras/*`.

**Identity across clusters is `ref`.** Every cluster's controller numbers its cameras from 1, so the domain sets `ref` on the row when it forwards the create (an operator field in М10's schema), and the worker's status and the controller's snapshot carry it back. The shadow, the directory and the read model key on it; a cluster-local id never leaves its cluster.

**A worker never learns a user exists.** `test_nothing_about_a_user_reaches_a_worker_only_trust_does` lists the south cluster's Variables after the agent has synced: `["domain/keys"]`. Then it tries to make the agent write `vms/cameras/7`, `vms/epoch/7` and `vms/slots/w-0` and gets `Forbidden` on each — not the controller's rows, not a worker's slot.

**The token names the subject and nothing else.** `verify()` returns `{"sub": "alice", ...}` and the test asserts `"roles" not in payload`. What alice may do is `ClusterGrants` in each cluster's `domain/grants`, with `valid_until`, carried by the agent and dropped when the domain drops them; the cluster's console and gateway enforce (`ClusterAuthoriser`), never a worker.

**Correctness from CAS, never from instance count.** `ClusterPlacer._store` re-reads on `Conflict` and returns whatever the other placer wrote; the race test runs two placers with opposite preferences.

**Nothing was written downward for this module.** Two operator fields went into М10's camera schema during the same review (`ref`, `events_retention_days`); М11's controller and workers are imported unchanged, and `render_three_ways` reads М11's `vmsworker.nomad.hcl` as it is.

## Verified where

Everything in `tests/` ran in the authoring sandbox and on the author's machine (Python 3.10/3.11, `cryptography` for Ed25519 and X.509) against М10's `vmsserver/` and М11's `clustervms/`. The stdlib HTTP console is exercised by `test_console_over_http`. What needs a bench is in `deploy/verify-bench.sh`: federated regions, the forwarded write, the agent's ACL against real Nomad, the signer's placement, and the console with a whole cluster's uplink pulled. WebRTC, fMP4 and TURN are the transport under `gateway.py`'s contract and are not here. A TPM is read about, not run.
