# Lesson 8 — Failure Is the Feature

**Module:** EdgeVMS — the box owns its truth (Module 9)
**You will build:** a test suite that kills, fills, stalls and unplugs — and a recorder that converges after each.
**Time:** ~150 minutes.

## Why this lesson exists

Everything up to now assumed things work. Cameras answer, disks have room, the AppHost keeps running.

None of that is true for more than a few days at a time, and on an appliance nobody visits, the difference between a product and a demo is entirely in what happens when it stops being true. So this lesson does not add a feature. It induces four failures on purpose, handles each one, and — this is the part that makes it stick — **asserts each one in a test that runs on every commit.**

There is a fifth thing here that looks like a detail and is the seed of М11's hardest problem: on restart, never resume the previous segment.

> **What you can verify without hardware.** The disk-full behaviour and the retention arithmetic run against real Postgres — every number below is measured output from **PostgreSQL 16.13**. The backoff and restart tests run against the fake actuator from Lesson 6. The stall test needs GStreamer and a camera you can unplug, real or simulated.

## Prerequisites

- **Lesson 7** — the real actuator, the `watchdog` element, and `splitmuxsink`.
- **Lesson 6** — backoff with jitter, and the state vocabulary.
- **Lesson 5** — partitioning, and the `DELETE`-frees-nothing demonstration. This lesson collects on it.
- **М9 Lesson 4** — the spool's bound policy. Step 3 is the same question with a different answer.

## Learning objectives

1. Reproduce four distinct failure modes deliberately and assert recovery from each.
2. Explain why jitter matters more than backoff when many devices fail together.
3. Enforce retention under disk pressure without a scan, and choose a degradation policy.
4. Bound what a crash loses, and prove the bound.
5. State the fencing rule and explain the problem it is standing in for.

---

## Step 1 — Camera offline

The most common failure and the least interesting one — until two hundred of them happen at once.

A camera is unplugged, or rebooting, or on a switch that just lost power. `rtspsrc` fails to connect and the pipeline never reaches PLAYING. The state machine goes `STARTING → FAILED`, backoff applies, and it retries.

The single-camera behaviour is already correct from Lesson 6 and needs no new code. What this lesson adds is the test that proves the *fleet* behaviour:

```python
def test_switch_reboot_does_not_stampede():
    """200 cameras fail simultaneously. Retries must not arrive together."""
    r = Reconciler(FakeStore([cam(i) for i in range(200)]),
                   actuator=always_fails)
    r.reconcile(now=0)
    retries = sorted(f["retry_at"] for f in r.failures.values())
    spread  = retries[-1] - retries[0]
    assert spread > 0.5, f"retries bunched within {spread:.3f}s"
```

Real output from the suite:

```
6. backoff + jitter ............... OK (200 cameras, first retry spread 1.00-2.00s)
```

**Without the jitter this test fails with a spread of 0.000s**, and that is the whole lesson: two hundred RTSP connections arriving in the same millisecond, at a switch that has just finished booting, which fails them all, which schedules another two hundred for the same instant. The system oscillates until something outside it breaks the symmetry.

Backoff protects one camera. **Jitter protects the network** — and the network is the thing whose failure caused this in the first place.

## Step 2 — Stalled stream, socket still open

The nasty one, from Lesson 7 Step 4: the camera stops sending video and the TCP connection stays up. Nothing errors, the pipeline is PLAYING, and the archive develops a hole with no complaint anywhere.

`watchdog timeout=8000` handles it, and the test is about **blast radius** rather than detection:

```python
async def test_one_stall_does_not_disturb_the_others():
    worker = await start_worker(cameras=50)
    await stall_camera(7)                       # stop sending, hold the socket
    await asyncio.sleep(12)

    assert worker.state(7) in (FAILED, STARTING)
    for i in [c for c in range(50) if c != 7]:
        assert worker.state(i) == RUNNING
        assert worker.segments_written(i) > 0   # still recording, not merely "up"
```

That last assertion is the one worth copying into your own work. Checking that the other pipelines are in state `RUNNING` proves the AppHost's bookkeeping is intact; checking that they are still **writing segments** proves the actual job is still happening. A worker whose state machine says RUNNING while every pipeline is wedged passes the first assertion and fails the customer.

To stall a camera without hardware: point `rtspsrc` at a simulated source you control and stop feeding it while keeping the connection open. `nc -l` holding an accepted socket is the crude version and is enough to trip the watchdog.

## Step 3 — Disk full

The interesting one, because retention has to run **at the moment there is no room to run it**.

### Why the naive loop fails exactly when needed

Lesson 5 measured this. Deleting a month of index rows:

```
DELETE 276768
Time: 230.735 ms
```

Fast — and the disk did not move:

```
     relname      | pg_size_pretty
------------------+----------------
 segments_2026_08 | 38 MB
```

Not one byte freed. The rows are dead tuples awaiting `VACUUM`, and `VACUUM` is heavy I/O you are requesting at the precise moment the box is under storage pressure. A retention strategy whose cost peaks when it is most needed is not a retention strategy.

The partition version, and note the syntax — **Postgres has no `DROP PARTITION` statement**, that is Oracle and MySQL:

```sql
ALTER TABLE segments DETACH PARTITION segments_2026_08;   -- Time: 1.713 ms
DROP TABLE segments_2026_08;                              -- Time: 3.328 ms
```

Five milliseconds, and the space is returned to the filesystem immediately because the file is unlinked rather than marked reusable.

### But the index is not the footage

Dropping a partition frees megabytes of *index*. The gigabytes are the `.mp4` files, and they need unlinking too — which means the deletion loop has to walk the segments it is about to forget **before** it forgets them:

```python
async def enforce_retention(db, now):
    # 1. find whole partitions older than the longest retention on any camera
    for part in await db.partitions_entirely_older_than(now - max_retention):
        paths = await db.fetch(f"SELECT path FROM {part}")     # remember first
        await db.execute(f"ALTER TABLE segments DETACH PARTITION {part}")
        await db.execute(f"DROP TABLE {part}")
        for p in paths:
            unlink_if_exists(p)                                 # then unlink
```

Order matters and is not symmetric. Unlink first and a crash leaves index rows pointing at files that do not exist — the console offers footage that 404s. Drop the index first and a crash leaves **orphaned files with no index row**: invisible, wasting space, and recoverable by a scan. One failure mode lies to the operator; the other only wastes disk. Choose the one that wastes disk.

> **Never delete what you cannot prove is superseded.** An orphan sweep is a background job you can afford. A missing recording is not recoverable at all.

### The policy, which is М9's question with a different answer

М9's spool faced the same wall and answered *drop oldest* or *stop recording*, because the footage was in transit and not yet safe anywhere.

Here it is **the archive**, and the answer changes: retention decides, and the retention is the customer's, written into `retention_days` per camera. But retention alone does not save you — if every camera is inside its retention window and the disk is still full, something has to give:

| Policy | Behaviour | Right when |
|---|---|---|
| **Honour retention, stop recording** | Refuse new segments; keep everything within its window | Footage is evidence and a gap is better than a missing week |
| **Degrade retention** | Shorten the window, oldest first, and record on | Recent footage matters most — usually true for security |
| **Degrade by camera priority** | Sacrifice the car park before the safe room | The customer has actually ranked their cameras |

Pick one, put it in the specification, and **make the recorder say which it did** — as an event, in the table from Lesson 5, because a silent drop is indistinguishable from a bug. This is the first thing in the course that a recorder needs to tell somebody about and has nowhere to send; М11 gives it one.

```python
def test_disk_full_degrades_by_policy_and_says_so():
    fill_disk_to(97)
    run_retention()
    assert still_recording()
    assert event_logged(kind="retention.degraded")
    assert oldest_segment_age() < configured_retention
```

## Step 4 — The AppHost dies

```bash
systemctl kill --signal=SIGKILL apphost
```

No cleanup, no handlers, no chance to write anything. systemd restarts it, and the requirement is Lesson 6's rule made physical: **it must rebuild its picture from Postgres plus observation, remembering nothing.**

```python
def test_kill_mid_change_converges():
    insert_camera(id=9)
    kill_apphost_after(0.2)                # mid-reconcile
    restart_apphost()
    assert converged(9, within=10)
```

What is lost is exactly one thing: **the open segment on each running camera.** `splitmuxsink` writes a segment and closes it; a segment closed before the kill is complete on disk and indexed. The one in progress is truncated, and depending on the muxer may be unplayable.

That bounds the loss at *segment length*, which is why Lesson 7 called `max-size-time` a product decision rather than a tuning knob:

```
10-minute segments  →  up to 10 minutes lost per camera on a hard kill
 1-minute segments  →  up to 1 minute lost, and 10× the index rows
```

Measure it rather than assuming it, and write the number down. "How much do we lose if the box loses power?" is a question customers ask, and the honest answer is a number you have tested.

## Step 5 — The fencing rule, in its smallest form

One line, and it looks like a detail:

> **On restart, never resume the previous segment. Open a new one.**

Resuming looks strictly better — you would recover those truncated seconds. Here is why it is forbidden.

The AppHost was killed. It did not necessarily *stop*. `SIGKILL` reaches the process; it does not reach a pipeline that has already handed a file descriptor to a kernel thread, and on a box under memory pressure a process can be stopped for seconds and then continue. If the restarted AppHost opens the same file and the old one is still writing to it, **two writers are appending to one video file.** The result is not a merge. It is a file that is neither, and the corruption is silent — you find out when someone asks to play it back.

So the restarted instance opens a *new* segment. The truncated one stays as it is: complete up to its last valid frame, indexed as such, and never touched again.

**On one recorder this is a convention.** Nothing enforces it; nothing needs to, because there is only one AppHost and systemd starts one at a time.

In М11 there are two instances of the same recorder during a failover — the new one on a healthy server, and the old one on a server everybody believes is dead and which is actually just slow. Both believe they own camera 7. A convention is worthless against that, and the mechanism that replaces it is a **fencing token**: the `epoch` you put in the path in Lesson 7, issued by a single authority, checked *at the archive* so the stale writer's files land where nobody reads them.

You cannot stop a zombie from writing. You can only make its writes harmless — and that idea starts here, as a one-line rule on a box with no zombies yet.

## Step 6 — Assemble the suite

```
tests/
  test_converge.py     Lesson 6's seven — still passing, unchanged
  test_offline.py      one camera; then 200, asserting retry spread
  test_stall.py        watchdog fires; the other 49 keep writing segments
  test_diskfull.py     retention under pressure, policy honoured, event logged
  test_restart.py      SIGKILL mid-change; converges; loss bounded by segment length
```

Two properties make this suite worth having rather than a box-ticking exercise.

**It runs on every commit.** The Lesson 6 tests need no database, no GStreamer and no network, so they run in milliseconds. The rest need a Postgres container and a simulated camera — still no appliance.

**Every test asserts convergence, not absence of error.** The question is never "did it throw?" It is: *after this failure, does the recorder end up doing what the database says it should?* That is the only definition of correct this module has, and it is the one that transfers unchanged to М11, where the failure is a whole server rather than a camera.

**Deliverable:** a test suite that kills, fills, stalls and unplugs, and asserts convergence after each — plus two numbers written down: how much footage a hard kill loses, and what your recorder does when the disk is full.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Retry-spread test passes with jitter removed | The assertion is too loose, or all 200 cameras share one `random` seed. It should fail with a spread of exactly 0.000s. |
| Retention runs but the disk does not shrink | You dropped index rows and never unlinked the files. Step 3 — the gigabytes are the `.mp4`s. |
| Retention deletes files still inside their window | Comparing against a global retention rather than per-camera `retention_days`. The longest window on any camera bounds what a whole partition may drop. |
| `DETACH PARTITION` blocks | It needs a brief lock. `DETACH CONCURRENTLY` avoids it if a long query is running. |
| After a kill, the console lists segments that will not play | The truncated open segment was indexed as complete. Index on segment *close*, not on segment *open*. |
| After a kill, disk usage exceeds indexed bytes | Orphaned files from a crash between unlink and drop — the safe direction. Run the orphan sweep. |
| The stall test passes but real cameras still wedge | Your simulated stall closes the socket; real ones do not. Hold the connection open — that is the whole failure mode. |

## Recap

- **Backoff protects one camera; jitter protects the network.** Without jitter, 200 cameras retry in the same millisecond and turn a switch reboot into a self-sustaining outage — a test with a spread assertion catches it.
- The stall test asserts that the other 49 cameras are **still writing segments**, not merely in state RUNNING. A worker can be green and wedged.
- `DELETE` freed **zero disk** in 231 ms; `DETACH` + `DROP TABLE` freed 38 MB in 5 ms. Postgres has no `DROP PARTITION` statement.
- Drop the index **before** unlinking files: a crash then leaves orphaned files (wasteful, recoverable by a scan) rather than index rows pointing at nothing (a lie to the operator).
- Disk-full is М9's question with a new answer — retention decides, but a full disk still needs a stated degradation policy, and the recorder must **log an event saying which it did**.
- A hard kill loses **the open segment and nothing else**, so segment length is a product decision. Measure the number; customers ask for it.
- **On restart, never resume the previous segment.** A convention here, because there is one AppHost; a fencing token in М11, because there will briefly be two.

## Exercises

1. Delete the jitter and run the 200-camera test. Record the exact spread. Then put it back and diff the two numbers — this is the cheapest strong argument you will ever make in a code review.
2. Implement all three disk-full policies behind a config flag and write the datasheet sentence for each. They should read like different products.
3. Measure the real cost of a hard kill at 1-minute, 10-minute and 30-minute segments: footage lost, index rows written per day, and files per directory after a year. Recommend a length and defend it.
4. Write the orphan sweep — files on disk with no index row — and decide how often it runs and what it does when it finds one. Deleting immediately is one answer and not obviously the right one.
5. Break the fencing rule: make the AppHost resume the previous segment on restart. Then simulate the slow-process case with `SIGSTOP`, `SIGCONT` and a restarted instance writing to the same path. Describe the resulting file. This is М11's central problem, met early and cheaply.

## Where this is going

The recorder now converges, and keeps converging through the four failures that actually happen. Nobody can see any of it — the only interface is `psql`.

**Lesson 9 builds the console**, with a login against Lesson 5's `operators` table, and a status vocabulary that keeps *positions* apart from *reasons*. It also closes the module by asking the uncomfortable question: now that the design is proven, which parts of it should not stay in Python — and the answer is more interesting than "the slow parts", because the reconcile loop you wrote by hand turns out to be the part that survives.
