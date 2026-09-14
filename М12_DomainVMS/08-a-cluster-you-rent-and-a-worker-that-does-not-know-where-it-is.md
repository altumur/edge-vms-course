# Lesson 8 — A Cluster You Rent, and a Worker That Does Not Know Where It Is

**Module:** DomainVMS — the smallest layer above a set of clusters (Module 12)
**You will build:** a second cluster the domain provisions from the customer's own cloud account; the bandwidth-and-cost arithmetic that decides what should live there; and a proof, by diffing, that a worker cannot tell.
**Time:** ~120 minutes.

## Why this lesson exists

М11 built clusters from servers in a room. This lesson changes exactly one thing — where the servers come from — and the point is that nothing else changes. A cloud region is a cluster: rented instances on one provider network satisfy М11's definition exactly as a rack does, and Nomad cannot tell the difference. If the software *can* tell, that is a bug in М9 or М11, and the lesson's method is to go looking for it with a diff.

It is also the lesson where the course closes the arc it opened. М8 rented a cloud VMS from Kinesis with hand-provisioned AWS keys. Here the shape is rebuilt the customer's way — their workers, their object storage, their cloud account, provisioned by their domain — and М9's hand-provisioned credentials are retired by no longer being needed. And before any of that is demonstrated, the arithmetic is done, because the demo is cheap and the decision it invites is not.

> **What you can verify without hardware.** The arithmetic and the diff: `tests/test_lesson8_cloud.py` computes the numbers below and renders the same worker job for a rack, a rented instance and a split site, then asserts the worker's part is byte-identical. Provisioning a real cluster from a real cloud account — and pulling the uplink on a cloud-recorded site — is the bench's, and the deliverable.

## Prerequisites

- **М11 Lesson 1** — what a cluster is: servers on one network you would bet recording on.
- **М11 Lesson 2** — the worker job, `deploy/vmsworker.nomad.hcl`.
- **М11 Lesson 3** — the restore point in the cluster's own object store, and `s3+https://` from the S3 adapter.
- **М9 Lesson 4** — the spool, and why an edge box survives an uplink outage.
- **М8** — the cloud VMS this lesson rebuilds the customer's way.

## Learning objectives

1. Say why a cloud region is a cluster and what the domain does to get one.
2. Do the bandwidth and cost arithmetic before recommending anything.
3. Name the three shapes — edge, cloud, mixed — and which a real fifty-camera site takes.
4. Deploy a worker three ways and prove the artifacts identical.
5. List what differs by placement and what must never differ.
6. Say what a cloud site has no spool for, and what becomes the buffer.

---

## Step 1 — A cloud region is just a cluster

Rented instances on one provider network: same LAN you would bet recording on, same Nomad servers, same Podman, same object store (theirs, this time, behind the `s3+https://` adapter from М11 Lesson 3). The domain cluster **provisions** it, using the customer's own cloud account — which is why this is a domain feature and not something above the domain: the account is the customer's, the root is the customer's, and the vendor is nowhere in the chain. Once the region joins the gossip pool it is one more entry in `Federation.clusters` with `reaches` naming whatever networks the provider's VPN gives it.

## Step 2 — The arithmetic, before the demo

Fifty cameras at 4 Mbit/s, the number `domain/cloud.py` prints:

```
50 cams × 4 Mbit/s: 200 Mbit/s sustained upstream; 2.16 TB/day; 30 days = 64.8 TB
    hot object storage ≈ $1,426/month        disks on-prem ≈ $27/month (amortised)
6 cams:  7.8 TB;  cloud ≈ $171/month         edge ≈ $3/month
```

The prices are parameters (`Prices`) and yours to replace; the *shape* of the result is not. Two hundred megabits sustained upstream is a link most sites cannot buy, and even where they can, hot object storage for footage costs more per month than the disk costs once. So `recommend()` returns three shapes, and the reasons are the sentences for the customer:

```
50 cams, 100 Mbit uplink   → mixed:  200 Mbit/s exceeds 70% of the uplink: recording stays at the edge, operation moves to the cloud
6 cams,  1 Gbit uplink     → cloud:  no hardware to install; operationally simpler, not cheaper; the camera is the buffer
50 cams, 10 Gbit uplink    → edge:   the uplink could carry it, but 64.8 TB hot costs 50× the disks; a datasheet implying otherwise loses money per camera
```

**Mixed is the shape a real deployment takes**: recording at the edge, where the bits are, and the domain services, the console, the read model in the cloud, where the operators are. A cloud-only site is for a handful of cameras with no hardware to install. The cloud option is not cheaper; it is *operationally simpler*, and the datasheet must say which.

## Step 3 — A cloud site has no spool

М9 gave an edge box a spool: an uplink outage is buffered locally and drained when the link returns. A camera streaming over the internet to a worker in a rented cluster has no such thing — the worker records into the resource on its rented server, *in the cloud*, and an uplink outage at the site is not buffered, it is lost. So **the camera becomes the buffer**: most IP cameras record to an SD card or an edge NVR, and ONVIF's replay profile lets the worker backfill the gap when the link returns. That is a real feature to build and a real sentence to say to the customer before they choose the shape: *for a cloud-recorded site, outage footage lives on the camera until the link returns, and cameras without local storage lose it.*

## Step 4 — Three ways, one artifact

Now the proof. `render_three_ways()` takes М11's `deploy/vmsworker.nomad.hcl` — the worker job as written — and produces three placements:

| | Datacenter | Object store |
|---|---|---|
| **local** — a rack in room A | `room-a` | `http://minio.room-a:9000/cluster-restore` |
| **rented** — the customer's cloud region | `cloud-eu-1` | `s3+https://s3.eu-1.example/cluster-restore?region=eu-1` |
| **split** — the site's workers local, the domain in the cloud | `room-a` | `variables://objects` — nothing rented needed |

and diffs them:

```
diff local/rented:
-  datacenters = ["room-a"]
+  datacenters = ["cloud-eu-1"]
-  OBJECT_STORE_URL=http://minio.room-a:9000/cluster-restore
+  OBJECT_STORE_URL=s3+https://s3.eu-1.example/cluster-restore?region=eu-1
identical from `group` down: True
```

Two lines differ, and neither is about the worker. Everything from `group` down — the constraint on the camera VLAN, the `disconnect` block with М11 Lesson 4's `lost_after`/`stop_on_client_after`, the reschedule policy, the task, its identity, its template, the lease numbers, the resources — is byte-identical. The worker cannot tell where it is running because nothing it reads says so. If the diff ever shows a third line, this lesson found a bug in М9 or М11, and the fix goes there, not here.

## Step 5 — What differs by placement, and what must never

| Differs by placement | Must never differ |
|---|---|
| storage class and its cost curve | configuration ownership — the cluster controller's, in the cluster's raft |
| how the camera's stream reaches the worker (LAN, VPN, internet) | the epoch, and where it is issued (the cluster's raft) |
| who is paged when hardware dies (you, or the provider) | the certificate chain — the domain's root, everywhere |
| whether there is a spool, and what the buffer is | the update mechanism — the domain's server, pulled |

The left column is real and belongs on the deployment sheet. The right column is the module's thesis, and every row of it has a test somewhere in М9–М12.

## Step 6 — Closing the arc with М8

The course opened by renting a cloud VMS: cameras into Kinesis, playback from HLS, keys typed into an environment file. Rebuild that shape here with the pieces you now own: your workers in a cluster you rented, footage on the rented servers' resources — and, if you choose S3 for events and heartbeats there, an object store in your account, live view through the gateway, playback from the resource's segments through the gateway — and no vendor's keys anywhere, because the domain provisioned the cluster with the customer's account and the signer issued every certificate in it. The hand-provisioned AWS credentials from М9 are not migrated; they are retired by having nothing left to do.

**Deliverable:** one domain, two clusters — one local, one rented from the customer's cloud account by the domain itself — both recording, both in one directory, `where()` finding a camera in each; and a written bandwidth-and-cost estimate for a fifty-camera site saying which shape it should be and why, with the two sentences the customer needs to hear (the uplink, and the camera as the buffer).

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| The rented cluster joins gossip but `where()` says unreachable | The provider's security group allows the gossip port and not the RPC port that forwarding uses. Both. |
| Heartbeats fail in the rented cluster with a signature error | You chose `s3+https://` for the object store there and `AWS_ACCESS_KEY_ID`/`SECRET` are not in the jobs' Variable — the template renders them empty. М11's `s3.py` signs with what it is given; `variables://objects` needs no keys at all. |
| A cloud-recorded site shows gaps after every uplink blip | Expected: no spool. Either the camera has local storage and backfill is not wired, or it has none and the sentence in Step 3 was not said. |
| The diff shows a third line | A placement-specific value crept into the worker's stanza. That is the bug this lesson exists to find; fix `vmsworker.nomad.hcl` in М11. |
| The cloud bill is higher than the estimate | Egress. The arithmetic counts storage; playback out of the cloud is billed too, and a wall showing sixteen streams all day is a number to add. |

## Recap

- A cloud region is a cluster. The domain provisions it from the customer's account, and Nomad cannot tell.
- Do the arithmetic first: bits per second up, terabytes per day, hot storage per month against disks once. Mixed is the shape.
- Cloud is operationally simpler, not cheaper. Say which.
- A cloud site has no spool; the camera is the buffer, or the footage is gone.
- A worker deployed three ways is one artifact from `group` down. Two lines differ and neither is the worker's.
- Ownership, the epoch, the chain and the update path never differ by placement.

## Exercises

1. Re-run `recommend()` with a sub-stream at 1 Mbit/s for recording. Which shapes change, and what did the customer give up to get there?
2. Add egress to `Prices` — a fee per TB played back out of the cloud — and a wall of sixteen streams for eight hours a day. Recompute the six-camera cloud site.
3. Write the ONVIF backfill as a reconcile-loop concern (М9 Lesson 6): what is desired, what is actual, what closes the gap, and what the backoff is for a camera that has no local storage.
4. Take the split shape and cut the site's uplink for a day. List what the operators can and cannot do, hour by hour, from the failure arithmetic in Lesson 3.
5. Find one thing М8's Kinesis deployment did that this shape does not. Decide whether it was a feature or a dependency.

## Where this is going

The module is complete. A domain sits over any number of clusters — in server rooms, in the customer's cloud, or both — knows what it does not know, places by reachability, serves people without ever serving them from a worker, issues and rotates its own trust, admits boxes with nobody typing a secret, and can be switched off without a single camera noticing. Every signal it emits — replica lag, the read model's ages, the outage arithmetic — is collected by [**М13 — Observability**](../М13_Observability/module-design.md), whose remote observer is one more domain service, hosted here.
