# Lesson 13 — A Stream From Another Cluster

**Module:** DomainVMS — the smallest layer above a set of clusters (Module 12)
**You will build:** a server room's recorder recording a camera that is a cluster of its own — found not in its own cluster's heartbeats, which do not contain it, but in a *source book* the domain publishes for the recording cluster and that cluster's agent carries home; one recording cluster per camera, decided and stored by the domain; recording that goes on with the domain switched off; and backfill that plans from the book and fetches only what the card still holds.
**Time:** ~100 minutes.

## Why this lesson exists

Cameras with a server room: the camera records to its card, and the server records it too. The card holds days, the server's volume months, and the card is inside the camera a thief takes with him. М10B built all of this for cameras that are not ours: the recorder pulls RTSP and writes its volume; the camera's card is a *device archive* (Lesson 15); after an outage the recorder closes its gaps from the card — *backfill from the edge* (Lesson 16).

None of that changes. What changes is one line of it. The recorder found its camera in its **own cluster's** heartbeats — `device_source` reads the holder's `playback_url` and `coverage` there:

```python
    def device_source(self, cam) -> tuple[str, dict] | None:
        found = holder_of(self.objects, "vms/", cam, self.wall(), field="playback_url")
```

A camera that is a cluster of one (Lesson 10) publishes its heartbeat in **its** cluster. The server room's recorder cannot read that, and must not depend on reading it: the thesis says the server room records on with the domain gone, and a recorder that asked the domain where its camera was would stop with it.

So the domain, which reads every member anyway, writes down what the recorder needs and gives it to the recorder's cluster the way it gives grants: carried by the agent, read locally, stale by a stated amount.

> **What you can verify without hardware.** Everything, in `tests/test_lesson13_crossing.py`: a domain, a server room with М11's real controller and worker, and a camera cluster from Lesson 10 with an hour on its card; the source book carried and resolved; a second recording cluster refused; the domain switched off for six hours; a camera that changes its address meanwhile; and a backfill plan checked against a card that has overwritten part of what the book said it held.

## Prerequisites

- **Lesson 1** — placement stored with a reason, by CAS, and refused when a second placer disagrees.
- **Lesson 3** — the read view, which now also keeps each worker's published doors.
- **Lesson 4** — the agent carrying a per-cluster row from `domain/<x>/<cluster>` to `domain/<x>`.
- **Lesson 10** — a camera as a cluster of one, publishing `live_url`, `playback_url` and `coverage`.
- **М10B Lessons 15–16** — the device archive, coverage, and backfill bounded by `keep_days` and `settle`.

## Learning objectives

1. Say what crosses between clusters here and what does not, and why the recorder stays where it is.
2. Make "which cluster records this camera" a stored domain decision, and refuse a second one.
3. Resolve a camera of another cluster from the recording cluster's own Variables, with the domain off.
4. Name the failure this design accepts, and how long it lasts.
5. Treat carried coverage as a hint: plan from it, fetch only what the device still has.

---

## Step 1 — Data crosses; work does not

The rule of the whole module is that a worker never crosses a cluster. It still does not. The recorder is a worker of the server room, scheduled by the server room's orchestrator, writing the server room's volume with the server room's epoch. The camera learns of no server: it serves its RTSP and its playback door to whoever asks, as it always did. Nobody writes a row, an epoch or a request into the camera's cluster except the camera — the first test counts its flash writes before and after and finds them equal.

What crosses is **data**: footage over RTSP, ranges from the card over the playback door. And one small piece of knowledge, which is this lesson: *where the camera is.*

## Step 2 — One camera, one recording cluster

A camera serves one live session and one backfill (М10B Lesson 15) — its encoder and its uplink are small. Two server rooms both recording it would each take half of what it has, or the second would take it from the first. So *which cluster records camera SN4471* is not something each server room decides for itself. It is a domain decision, stored once, by CAS, with the same shape as Lesson 1's placement:

```python
    def record(self, ref: str, on: str) -> dict:
        ...
        if known[0] == on:
            raise ApiError(400, f"camera {ref} is in {on} already: a cluster records its own cameras as it always did")
        ...
            if ref in items:
                raise ApiError(409, f"camera {ref} is recorded by {items[ref]} already: a device serves one live "
                                    f"session and one backfill, and a second recorder would take them from the first")
```

Asking again for the same cluster is the same answer; asking for another is a `409` with the reason; asking for a camera the domain has never seen is a `404`. The recording row itself is the recording cluster's — its console writes `rec/recordings/<id>` with `source: ref:SN4471`, forwarded by the domain as Lesson 3 forwards a create.

## Step 3 — The source book

For each recording cluster, the domain publishes a book: for every camera of another cluster that it records, the doors that camera's worker last published and **when**:

```python
    def _doors(self, ref: str) -> dict | None:
        for (cluster, worker), s in self.view.snapshots.items():
            if any(str(st.get("ref", "")) == ref for st in s.status) and s.doors:
                return {"cluster": cluster, "worker": worker, **s.doors, "as_of": s.ts,
                        "reachable": cluster not in self.view.cluster_down_since}
```

It is built from the read view's memory — the read view now keeps each heartbeat's `live_url`, `playback_url` and `coverage`, which it used to drop — so publishing it asks no camera anything. It is written under `domain/sources/<cluster>` in the domain cluster, and the recording cluster's agent carries it to `domain/sources`, exactly as it carries `domain/grants/<cluster>`. The agent learned one generic thing for this: a list of per-cluster rows to carry, of which this is the first.

The recorder resolves a `ref:` source against its own cluster's copy; any other source is its own cluster's and is found as always:

```python
def resolve(cluster_vars, source: str, now: float) -> Source | None:
    if not str(source).startswith("ref:"):
        return None
    ...
    return Source(ref, e["cluster"], e["live_url"], e["playback_url"], e.get("coverage"),
                  max(0.0, now - float(e["as_of"])), bool(e.get("reachable", True)))
```

The seam in `recworker.py` is one fallback in `device_source`: not found in my heartbeats, and the row's source is `ref:` — resolve it.

## Step 4 — The domain off, and the failure it costs

Switch the domain off for six hours. The recorder never asked it anything, so nothing changes: it resolves from the copy its agent last carried, and the copy's `age` says six hours. The console shows that age beside the recording, as it shows every other age in this module.

The failure this design accepts is precise: **a camera that changes its address while the domain is off is recorded from the old address until the domain is back.** The recorder sees a dead URL, reports it as any dead source, and has nothing better to try. When the domain returns, one read-view pass, one publish and one agent pass later the book is right again — the test moves the camera to a new DHCP lease with the domain off and checks both halves. Cameras on a site get fixed addresses or long leases for reasons older than this lesson; the lesson makes the cost of not doing so a number.

## Step 5 — Backfill: the book plans, the card decides

After an outage the recorder closes its gaps from the card, and М10B's rule for what to fetch is unchanged: what the device has, minus what we have, not older than our own volume keeps, not fresher than `settle`. The subtraction is `vms.archive.subtract`, the one the recorder and the console already share.

What is new is that the device's coverage now comes from the book, and the book is as old as its last carry. A card is a ring: the oldest hour it listed may have been overwritten since. So the plan comes from the book and the fetch from the card:

```python
    card = ask_device()                                  # {"from", "to"} — the card, now
    for h in holes:
        lo, hi = max(h[0], float(card["from"])), min(h[1], float(card["to"]))
        if hi > lo:
            fetch.append((lo, hi))
        for gone in subtract(h, [(lo, hi)] if hi > lo else []):
            dropped.append((gone, "no longer on the card"))
```

The test's book says the card holds the last hour; the server holds all of it but minutes 30 to 10. The card, asked, now starts at minute 25. Minutes 25 to 10 are fetched; minutes 30 to 25 are **dropped with their reason**, not retried — a retry would ask the same card the same question. This is М10B's "check twice — at planning and before writing" with the first check made from further away.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| A recording stops whenever the domain is restarted | The recorder asks the domain for the camera. Resolve from the cluster's own `domain/sources`. |
| Two server rooms record one camera and both have gaps | Nothing stored which cluster records it. `record` refuses the second. |
| After a site's DHCP renumbering, recordings are dead until someone restarts the domain | The accepted failure, lasting until the domain's next publish. Fixed addresses, or shorten the book's journey. |
| Backfill keeps retrying the same range and failing | The range is no longer on the card; the book said it was. Check the card before fetching; drop what is gone. |
| The camera's store has keys written by the server room | Something crossed that should not. Only data crosses; nothing is written into another cluster. |

## Recap

- The recorder is unchanged in what it does; only where it finds the camera changes.
- Data crosses between clusters — footage and ranges. Work does not: no worker, row, epoch or request crosses.
- Which cluster records a camera is a stored domain decision, one per camera, refused like a second placement.
- The source book is built from the read view's memory, carried by the agent, resolved locally — with the domain off.
- A camera that moves while the domain is off is recorded from its old address until the domain returns: stated, bounded.
- Backfill plans from the carried coverage and fetches only what the card still holds.

## Exercises

1. The recording cluster itself is unreachable from the domain for a day. What does its recorder keep doing, and what does the console show about the recording?
2. A camera is moved from one server room's recording to another's. Write the steps in order so that neither room records it twice and neither loses more than a pass.
3. The book carries `coverage` as one range. A card that failed for an hour last week has two. Change the book's entry and `plan_backfill` so the hole in the card is not planned as a fetch.
4. Should a camera that is recorded by a server room still record to its card? Argue from what each copy protects against, and say which one Lesson 14's alarm mirror makes less necessary.

## Where this is going

Footage now has two homes; alarms still have one, and it is the camera's card. [**Lesson 14**](14-alarms-from-every-member.md) builds the one list of alarms an operator watches across every member — and a second copy of each camera's alarms on a neighbour, for the camera that is off.
