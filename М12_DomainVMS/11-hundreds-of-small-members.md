# Lesson 11 — Hundreds of Small Members

**Module:** DomainVMS — the smallest layer above a set of clusters (Module 12)
**You will build:** a measurement of what the domain costs at three hundred members, a tenth of them off — calls counted, link time charged per call — and the three changes it forces: the snapshot read once per member, "where is camera X" answered from memory, and a pass that waits for silent members together and then stops asking them every few seconds.
**Time:** ~90 minutes.

## Why this lesson exists

*Several clusters, one domain* in the module design says the directory and the read view hold "low hundreds of clusters' worth", and nobody ever checked. With server rooms nobody had to: a customer with three campuses has three clusters and hundreds of cameras, and the domain's loops run over **clusters**. Lesson 10 changed the arithmetic without changing a line of the loops. A site of three hundred cameras is now three hundred clusters, and on any given evening thirty of them are off.

The code has the same shape it had at three clusters. Its cost does not, and it hides in three places nobody had reason to look at before:

- **per pass** — what one refresh of the read view asks of every member;
- **per edit** — what `where(camera)` costs, which every forwarded edit calls;
- **per silence** — what a member that does not answer costs, which on a real link is not zero but a whole connect timeout.

This lesson measures all three before promising anything, finds a defect that was invisible at three clusters, and changes the read view so the promise can be kept.

> **What you can verify without hardware.** Everything, in `tests/test_lesson11_hundreds.py`: three hundred `DeviceCluster`s from Lesson 10 in one domain, a `Meter` around each member's stores counting calls and charging link time, and a real-thread test with stores that sleep. The numbers in this lesson are what those tests print.

## Prerequisites

- **Lesson 3** — the read view: one pass over every member, heartbeats and snapshot shards, rows served from memory.
- **Lesson 9** — an edit kept for a member that is off.
- **Lesson 10** — a camera as a cluster of one.

## Learning objectives

1. Count the calls a pass makes per member, and say what the count was before this lesson.
2. Show why `DomainDirectory.where` does not survive a bulk edit at three hundred members, and answer it from memory without becoming less honest.
3. Put a number on what silent members cost a sequential pass, and remove most of it with lanes and a back-off.
4. State the back-off ceiling as a promise to the operator.
5. Handle the new case that answering from memory creates.

---

## Step 1 — A meter, not an estimate

`domain/scale.py` wraps each member's two stores and does two things per call: counts it, and charges it what it would cost on a link — `latency` (20 ms) for an answer, `timeout` (2 s) for learning there is none. `pass_time(lanes)` spreads the charged work over `lanes` concurrent readers the way a thread pool would, longest first onto the least loaded:

```python
    def pass_time(self, lanes: int = 1) -> float:
        load = [0.0] * max(1, lanes)
        for c in sorted(self.cost.values(), reverse=True):
            load[load.index(min(load))] += c
        return max(load)
```

One lane is the sum: the pass exactly as Lesson 3 wrote it, one member after another.

## Step 2 — A pass, counted

Three hundred cameras, all on. One pass of the read view:

| | |
|---|---|
| calls | **1 202** — four per member, two for the domain cluster's empty listings |
| bytes | **172 KB** — about 575 bytes per member: one heartbeat, one snapshot shard |
| time, in turn | **24 s** |

The bytes are nothing. The time is not: a pass meant to run every five seconds takes twenty-four with every camera answering, because twelve hundred round trips of twenty milliseconds, one after another, are twenty-four seconds.

And the count is four only since this lesson. `refresh` used to read the snapshot twice per member:

```python
                self.configured[name] = (c.snapshot() or {}).get("cameras", [])
                self.configured_at[name] = float((c.snapshot() or {}).get("ts", 0))
```

once for the rows and once for their age. At three clusters that was six calls instead of four, and nobody could see it. At three hundred members it is six hundred extra round trips a pass — a third of the pass. The member's part of a pass is now one function that reads each thing once:

```python
    @staticmethod
    def _read(c) -> tuple[dict, dict]:
        return c.heartbeats(), (c.snapshot() or {})
```

## Step 3 — Where is camera X, fifty times

Every forwarded edit starts with `where(camera)` (Lesson 3), and `DomainDirectory.where` reads every member's snapshot on every call. That was right when "every member" was three: always fresh, no cache to be wrong.

A bulk edit of fifty cameras, with thirty cameras off:

| | calls | time |
|---|---|---|
| `DomainDirectory.where`, 50 times | **28 550** | **3 541 s** — most of an hour |
| `ReadView.where`, 50 times | **0** | — |

Each silent member costs its full timeout on every scan, and there are fifty scans. The read view already holds every member's rows from its last pass, so it answers from memory — and it stays exactly as honest as the directory, because it builds the same `Answer`: found only in a member that answered, the silent ones named, `complete` false while any is silent.

```python
    def where(self, camera) -> Answer:
        down = sorted(n for n in self.fed.clusters if n in self.cluster_down_since or n not in self.cluster_ok)
        searched = sorted(n for n in self.fed.clusters if n not in down)
        hits = [(cl, row) for cl in searched for row in self.configured.get(cl, [])
                if str(row.get("ref", "")) == str(camera)]
        ...
```

`ConsoleAPI` takes anything with a `where`, so the read view is passed where the directory was.

## Step 4 — Silence, waited for together

The same site with thirty cameras off, one pass:

| | time |
|---|---|
| in turn | **82 s** — 270 × 4 × 20 ms, plus 30 × 2 s |
| sixteen lanes | **5.1 s** — the timeouts overlap |
| sixteen lanes, silent members backing off | **1.4 s** |

Lanes are `ReadView(lanes=16)`: a thread pool over the members due this pass. Nothing else about the pass changes — the fourth test runs the same site with one lane and with sixteen and compares every row — and the last test checks that the lanes are real threads, with stores that actually sleep, not only arithmetic in the meter.

Lanes make thirty silences cost one timeout instead of thirty. The back-off makes them cost nothing most of the time. A member that did not answer is not asked again until `retry_at`: five seconds after its first silence, doubling, up to a ceiling. It does not leave the list: its last rows stay, marked *unreachable*, with their age.

```python
            if got is None:
                self.cluster_down_since.setdefault(name, now)
                if self.backoff:
                    self.failures[name] = self.failures.get(name, 0) + 1
                    self.retry_at[name] = now + min(self.backoff_max, self.backoff * 2 ** (self.failures[name] - 1))
                continue
```

## Step 5 — The ceiling is a promise

A back-off without a ceiling is a camera off for a weekend that is asked about once an hour by Monday, and appears on the list an hour after somebody plugged it in. The ceiling is what the product can say to the operator: *a camera that comes back is on the list within a minute.* The test keeps a camera off for an hour of passes, switches it on, and finds it live within the ceiling.

The defaults — one lane, no back-off — are Lesson 3's pass, unchanged. A server-room domain does not need either, and its tests still switch a link off and on and expect the next pass to see it.

## Step 6 — The case that memory creates

Answering from memory has one new case: a member that answered the last pass and is off by the time its edit is forwarded. The directory never had it, because it looked a moment before forwarding. Now the forward itself meets the silence.

It is Lesson 9's case with the order reversed, and it ends the same way. `ConsoleAPI` catches `Unreachable` from the forward and keeps the edit for the cluster it was forwarding to:

```python
        try:
            result = self.consoles(ans.cluster).update_camera(camera, fields, subject)
        except Unreachable:
            kept = self._keep_for(ans.cluster, camera, fields, subject)
            if kept is None:
                raise ApiError(503, f"{ans.cluster} did not answer the edit")
            ...
```

The bulk edit of twenty cameras, one switched off after the pass: nineteen applied, one kept with `202`, no error from deep inside the forward.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| The console's list is a minute old on a large site | One lane: the pass runs members in turn, and silent ones cost a timeout each. `lanes=16`. |
| A bulk edit takes minutes and the operator retries it | The API scans the directory per camera. Pass the read view as the directory. |
| A camera that was switched on stays *unreachable* for a long time | No ceiling on the back-off, or one far above what the product promises. |
| A bulk edit fails with an exception from the forward | `Unreachable` from the owner is not caught; a member went silent after the pass. Keep the edit (Lesson 9). |
| The pass count is six calls per member | The snapshot is read twice. Read it once with the heartbeats. |

## Recap

- The module promised "low hundreds of clusters"; cameras reach it on the first site. Measured, not assumed.
- A pass: 4 calls and ~575 bytes per member — once the double snapshot read was found.
- `where` from the directory is a scan per edit; from the read view it is memory, and exactly as honest.
- Silent members cost a timeout each in turn: lanes overlap them, a back-off stops asking every pass.
- The back-off ceiling is a promise: *back on the list within a minute*.
- A member silent since the last pass is Lesson 9's case, and ends in `202`.

## Exercises

1. Run the site at 1 000 members. Which number breaks first — pass time, bytes, or the threads a pass needs — and at what lane count?
2. The read view answers `where` from a pass that may be five seconds old. Name an edit for which that age is wrong, and what the forward does about it.
3. Replace the doubling back-off with a fixed 60 s. What does the operator see differently in the first minute after a camera goes off, and in the first minute after it comes back?
4. A site's WAN link carries the domain's reads to three hundred cameras at 172 KB per pass. At one pass every five seconds, what fraction of a 10 Mbit/s uplink is that, and would you shorten the pass or the heartbeat?

## Where this is going

Hundreds of members, each with its own settings, and no server to keep the settings they share. [**Lesson 12**](12-shared-settings-without-a-database.md) publishes those — defaults, the folder tree, the scenarios between cameras — once, signed, carried to every member by its agent.
