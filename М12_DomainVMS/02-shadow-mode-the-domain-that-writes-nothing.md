# Lesson 2 — Shadow Mode: The Domain That Writes Nothing

**Module:** DomainVMS — the smallest layer above a set of clusters (Module 12)
**You will build:** a divergence report — six kinds of disagreement between what the directory says and what the clusters' workers report, told apart by distance and time — and the written criterion for letting the domain write.
**Time:** ~90 minutes.

## Why this lesson exists

The course has a convention: the stand-in before the real thing. `camera_sim.py` before the pipeline, `filesink` before `kvssink`, a fake actuator before GStreamer. At the top layer the convention has teeth, because a domain that writes into three clusters' worth of state on its first day, on the strength of a model nobody has checked against reality, is how a fleet ends up with cameras nobody placed and placements nobody runs.

So the domain's first mode is one where it computes what it *would* do, observes what actually is, and prints the difference. The point is not caution for its own sake. It is that the difference is the design work: every camera running that the model does not describe is a gap in the model, and driving that number to zero is what "the domain is correct" means. You cannot skip it, only postpone it to a worse moment.

> **What you can verify without hardware.** All of it. `domain/shadow.py` takes the directory's view, the workers' reports and a clock, and returns findings; `reports_from(fed)` builds those reports from what real clusters publish — heartbeats and `vms/epoch/*` — and `test_the_report_from_what_clusters_publish` runs it over М11's real controller and workers; `tests/test_lesson2_shadow.py` is the lesson. Running it against live traffic — real workers, real revisions moving — is the bench's job and the reason shadow mode exists.

## Prerequisites

- **Lesson 1** — the directory of directories and cluster placement; this lesson compares them to reality.
- **М11 Lesson 4** — the epoch. One of the six kinds is a report under a superseded epoch, and it is the fencing rule catching a writer that should have stopped.
- **М9 Lesson 6** — `observed_revision` and the `>=` rule. Shadow mode reads the same numbers, one level up.
- **М9 Lesson 9** — positions and reasons. The report has kinds, not a health enum.

## Learning objectives

1. Compute a divergence report from the domain's placement, the clusters' epochs and the workers' reports.
2. Name the six kinds and say which are faults, which are normal, and which are measurements.
3. Tell *slow* from *stuck* by distance and by time, and say why "diverged" is an alert you learn to ignore.
4. State the one number and defend its target.
5. Write the exit criterion for switching the domain into write mode.

---

## Step 1 — Ordering beats equality

Before the taxonomy, the token. Every camera row carries a `revision` its controller bumps on each edit and every worker reports the `observed_revision` it has applied (М10 Lesson 4), and the domain must be able to say how far behind a worker is on a camera. The token could be an opaque value compared for equality, or an ordered revision. Ordering wins three ways:

1. **It expresses distance, not just difference.** "Diverged" is an alert you learn to ignore; "behind by four revisions for forty minutes" is an incident.
2. **It permits skip-ahead.** A worker offline across revisions 7, 8 and 9 converges straight to 9 without replaying. Edge links go down constantly; this is not an optimisation.
3. **It survives replay and reordering.** A late report carrying a lower revision is ignored rather than ambiguous.

The cost: you lose proof that one *precise* configuration was applied at one moment. If that must be auditable it belongs in an audit log, not in the convergence token. The report below uses `revision - observed_revision` everywhere, and never asks whether two blobs are equal.

## Step 2 — The six kinds

`Shadow.compare()` takes three things: `placed` (camera ref → the cluster the domain's `domain/placement/<ref>` says), `epochs` ((cluster, ref) → the current epoch from that cluster's `vms/epoch/<id>`), and the workers' reports — each worker's heartbeat, carrying per camera the epoch it holds, the row's `revision` and its `observed_revision`. Everything is keyed by the domain's `ref` (Lesson 1), because two clusters both have an id 1. It returns findings of six kinds:

| Kind | Meaning | Fault? |
|---|---|---|
| **lagging** | behind, within grace | No — normal |
| **stalled** | behind past grace, and progress static | **Yes** — the real "it didn't take effect" |
| **orphaned** | placed by the domain, and no worker in the domain runs it | Yes |
| **unmanaged** | a cluster runs a camera the domain never placed | In shadow mode, **a measurement, not a fault** |
| **conflict** | two workers — or two clusters — claim one camera, or the claimant's cluster is not the placee | Always — a placement or fencing failure |
| **stale_epoch** | a report under a superseded epoch | The fencing rule catching a writer that should have stopped |

One world, four workers, and everything goes wrong at once:

```
lagging=0  stalled=0  orphaned=1  unmanaged=1  conflict=1  stale_epoch=1
  stale_epoch  camera=9 where=north/w-2: reports under epoch 1, current is 2
  conflict     camera=3 where=None: claimed by [('north', 'w-1'), ('north', 'w-3')]
  unmanaged    camera=4 where=north/w-1: running, never placed by the domain
  orphaned     camera=9 where=north: placed, and no worker in the domain runs it
```

Read the first and last lines together, because they are one story. w-2 reports camera 9 under epoch 1 while the cluster issued epoch 2 for it — the old instance of w-2 is still alive somewhere and still talking. Its claim counts for nothing (the code drops a stale-epoch claim before counting), which is why camera 9, placed in north, is then *orphaned*: the only thing claiming it was a zombie. That is М11 Lesson 4's fence, seen from the domain: a writer that should have stopped, caught by the number in its own report.

The conflict is the other kind of wrong. Two live workers both say they run camera 3. Whatever caused it — a move that did not remove the camera from the source's assignment, an ACL that let two controllers write one key — it is never a tie to break and always a fault to raise. The other conflict, `placed in south, running in north`, is the domain's own level caught out: its placement row and a cluster's snapshot disagree.

## Step 3 — Slow versus stuck

The lagging/stalled boundary is the one that needs a clock, and it is worth watching move:

```
t=100  observed=5  revision=7   lagging     behind by 2 revision(s) for 0s
t=200  observed=5  revision=7   stalled     behind by 2 revision(s) for 100s
t=210  observed=6  revision=7   lagging     behind by 1 revision(s) for 0s
t=220  observed=7  revision=7   (clean)
```

`Shadow` remembers, per worker and camera, the last `observed_revision` it saw and *when it changed*. Behind by two for a hundred seconds with no movement is *stalled* — the grace was sixty. The moment progress moves, the clock restarts and the worker is merely *lagging* again on that camera, even though it is still behind. Being behind is not the fault. Being behind and not moving is.

This is М9's `>=` rule with time added. A worker that reports `observed_revision = 5` against `revision = 7` is lagging; the same report ten minutes later, unchanged, means the reconcile loop on that worker is not converging on that camera, and *that* is what pages someone. "Diverged" would have fired at t=100 and been ignored by t=200.

## Step 4 — The one number

`unmanaged == 0`.

Anything running that the model does not describe is a gap in the model. In shadow mode it is not a fault — the domain has not placed anything yet, so on day one *everything* is unmanaged — and driving it to zero is the work: importing every camera the clusters already run into the domain's placement, with a reason and a `ref`, until the report is clean. A domain switched to write mode with unmanaged cameras will, at its first rebalance, treat them as free space.

```python
def exit_criterion(rep, consecutive_clean, required=3):
    if rep.unmanaged:
        return False, f"unmanaged={rep.unmanaged}: the model does not describe everything that runs"
    if rep.faults:
        return False, f"{len(rep.faults)} fault(s) outstanding: ..."
    if consecutive_clean < required:
        return False, f"{consecutive_clean}/{required} consecutive clean reports"
    return True, "unmanaged == 0, no faults, stable across reports: the domain may write"
```

The criterion is code because a criterion that lives in someone's head is renegotiated at the moment it is inconvenient. Three consecutive clean reports is the default; the number is yours to defend.

**Deliverable:** a divergence report against your own cluster from Lesson 1 — with a camera you never placed, a zombie you resumed with `kill -CONT` (М11 Lesson 4), and a worker whose reconcile loop you paused — and the written exit criterion, met.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Every worker is *stalled* immediately | The grace is shorter than the workers' heartbeat interval; a worker cannot move `observed_revision` faster than it reports. Grace ≥ 2 × the heartbeat interval. |
| A worker flips lagging/stalled/lagging | Progress moves in bursts (a batch of edits, a long reconcile). Widen the grace or look at why one pass takes that long — М9 Lesson 7's shard size. |
| `unmanaged` never reaches zero | Something adds cameras at a cluster's console without going through the domain's placement — which is allowed, and is what a single-cluster customer does all day. In shadow mode that is the measurement working: find the path and route it. |
| `stale_epoch` on a worker that is healthy | The `epochs` map is stale — the domain read `vms/epoch/<id>` before the camera's last restart. Re-read; if it persists, the worker is not taking the epoch before it starts (М10 Lesson 4's gate). |
| `orphaned` for a camera you know is recording | Its worker's report was dropped as stale-epoch (see above), or the worker has not heartbeaten since, or its `ref` is missing — the cluster's console created it without one. All three are worth knowing. |

## Recap

- The domain's first mode computes, observes, and prints the difference. It writes nothing.
- Six kinds: lagging (normal), stalled (fault), orphaned (fault), unmanaged (a measurement here), conflict (always a fault), stale_epoch (the fence, seen from above).
- Slow versus stuck is distance *and* time; "diverged" is neither.
- The one number is `unmanaged == 0`, and reaching it is the design work, not a formality.
- The exit criterion is code, met before the domain writes.

## Exercises

1. Add a seventh kind, *duplicate placement* — a camera placed in two clusters in the domain's own rows — and say whether it can happen if `domain/placement/<ref>` is written by CAS. If it cannot, delete the kind and write down why.
2. The progress memory is per worker *and* camera. Make it per worker only, and say what the report loses for a worker that converges forty-nine cameras and stalls on one.
3. Run the report with grace = 0. Count the stalled findings against the Lesson 1 cluster and say what the number measures.
4. Write the shadow report as a Prometheus exposition: one gauge per kind, labelled by cluster. Which kind deserves an alert with no threshold?

## Where this is going

The domain can now see. [**Lesson 3**](03-the-api-and-what-it-refuses.md) lets people see it: the camera list assembled from the same snapshots this lesson read, the write API that forwards to the owning cluster and refuses to set placement at either level, and — because someone has to serve browsers and it must not be a worker — the console and the live gateway.
