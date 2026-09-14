# Lesson 6 — A Reconcile Loop with Nothing in It

**Module:** EdgeVMS — the box owns its truth (Module 9)
**You will build:** the worker's reconcile loop, complete with backoff, status vocabulary and a restart test — with `print()` where GStreamer will go.
**Time:** ~120 minutes.

## Why this lesson exists

This is the `camera_sim.py` move from М8 Lesson 2, applied to control instead of media: **build the loop before the thing it controls.**

The reason is not convenience. A reconcile loop is about thirty lines, and every one of them is a decision. Attach real pipelines and those decisions become invisible — every bug looks like a GStreamer bug, every test takes eight seconds to run, and you cannot tell a broken state machine from a camera that is genuinely offline. Build it against a `print()` and the loop is the only thing in the room.

You will also make both classic mistakes on purpose, because one of them produces a system that lies to its operator and it is much better to meet that here than in production.

> **What you can verify without hardware.** All of it. The reconciler below is pure logic over an injected store and actuator — no database, no GStreamer, no network. The seven tests in Step 6 were run while writing this lesson and their output is printed as it came out.

## Prerequisites

- **Lesson 5** — the schema, `revision`, and the operator/controller column split.
- **М8 Lesson 2** — process supervision, exponential backoff, and the self-matching `pkill` bug. The worker is what `looper.py` grows into.
- Python 3.11+. No third-party packages are needed for this lesson.

## Learning objectives

1. State the desired/actual rule and explain what each mistake in violating it produces.
2. Write a reconcile loop that converges, then does nothing.
3. Use `observed_revision >= revision` as the only definition of "applied".
4. Explain why polling is correctness and `LISTEN/NOTIFY` is only latency.
5. Implement exponential backoff **with jitter**, and say what the jitter is actually for.
6. Distinguish `lagging` from `stalled`, and keep both out of the phase enum's way.

---

## Step 1 — The rule, and the two ways to break it

| | Holds | Written by | Survives |
|---|---|---|---|
| **Desired state** | what the operator asked for | the operator, through the API | reboots, OS updates, the worker dying |
| **Actual state** | what is running right now | the worker, by observation | **nothing** — it is re-derived every time |

> **Desired state is persisted. Actual state is derived.**

Break it in the first direction — forget to persist desired state — and you have built something that forgets its cameras on reboot. Annoying, obvious, fixed in an afternoon.

Break it in the second — persist actual state — and you have built **a cache that lies**. The worker restarts, reads its own saved notes saying camera 7 is recording, believes them, and never starts camera 7. The console is green. Nothing is recording. Nothing in the system is aware of a problem, and the discovery happens when a customer asks for footage.

The second bug is worse in every dimension: silent, plausible, and it survives restarts. Step 5 makes you build it.

## Step 2 — The loop

```python
"""worker.py — Lesson 6. No database, no GStreamer: the loop only."""
import random

CONVERGED, LAGGING, STALLED = "converged", "lagging", "stalled"

class Reconciler:
    def __init__(self, store, actuator, max_backoff=60.0):
        self.store = store
        self.actuator = actuator
        self.actual = {}          # camera_id -> {"revision": n}   IN MEMORY ONLY
        self.failures = {}        # camera_id -> {"n":, "retry_at":, "delay":}
        self.max_backoff = max_backoff

    def reconcile(self, now=0.0):
        desired = {c["id"]: c for c in self.store.desired() if c["enabled"]}
        actions = []

        for cid, cam in desired.items():
            have = self.actual.get(cid)
            if have and have["revision"] >= cam["revision"]:
                continue                                   # already applied
            if self.failures.get(cid) and now < self.failures[cid]["retry_at"]:
                continue                                   # in backoff, not yet
            verb = "start" if not have else "restart"
            if self.actuator(verb, cam):
                self.actual[cid] = {"revision": cam["revision"]}
                self.failures.pop(cid, None)
                actions.append((verb, cid))
            else:
                self._fail(cid, now)
                actions.append(("failed", cid))

        for cid in list(self.actual):
            if cid not in desired:
                self.actuator("stop", {"id": cid})
                del self.actual[cid]
                actions.append(("stop", cid))
        return actions
```

Read what is *not* there. No `if camera_was_added` and no `if camera_was_deleted`. The loop does not process events; it compares two sets and acts on the difference. That is the property that makes it survive missing an event, arriving late, restarting mid-change, or being run twice — and it is why a reconciler is not the same shape as a message handler even though both end up calling `start()`.

Three lines deserve attention.

**`self.actual = {}` in `__init__`, with a shouted comment.** A fresh process knows nothing and must rediscover everything. The comment is there because the "optimisation" of saving it looks so reasonable at 4pm on a Friday.

**`have["revision"] >= cam["revision"]`, not `==`.** Ordering, not equality — the whole reason Lesson 5 made `revision` an integer. `>=` also means a worker somehow ahead of the store is left alone rather than pointlessly restarted.

**The stop loop iterates `self.actual`, not `desired`.** You cannot learn about a deletion by looking at rows that exist. Everything that must be *stopped* is found by walking what you are running and asking whether it is still wanted.

## Step 3 — Poll, and notify only for speed

The obvious way to know a row changed is `LISTEN/NOTIFY`:

```sql
CREATE FUNCTION notify_cameras() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('cameras', NEW.id::text);
    RETURN NEW;
END $$ LANGUAGE plpgsql;
```

Use it — and never rely on it.

**`NOTIFY` is not durable.** It is delivered to sessions currently listening. A listener whose connection dropped during a network blip, a failover, or a Postgres restart misses every notification sent while it was away, **permanently**. There is no backlog and no redelivery.

A system built only on NOTIFY therefore stops converging and does not say so. It looks perfect in testing, where nothing disconnects.

```python
async def watch(self, poll_interval=2.0):
    while True:
        await self.reconcile_once()
        await self.wait_for_notify_or_timeout(poll_interval)
```

> **The timer is correctness. The notification is latency.**

The poll is what guarantees convergence eventually; the notify is what makes `INSERT` feel instant instead of taking two seconds. Delete the notify and the system is slower. Delete the timer and the system is broken in a way that only appears in production.

This same pairing returns in М11, where a recorder watching the domain has a streaming watch **and** a periodic full read, for exactly this reason.

## Step 4 — One task per concern, not one per camera

```
Worker (one process)
  asyncio tasks
    reconcile()    every 2 s, and on NOTIFY    desired (Postgres) vs actual (dict)
    pump_buses()   every 200 ms                drain each pipeline's messages
    report()       every 5 s                   write observed_revision + conditions back
```

Three tasks, whether the recorder has five cameras or five hundred.

The instinct is a task per camera, and it is wrong here for a reason worth having: **per-camera state is a state machine, not a coroutine.** A coroutine per camera means five hundred places that can independently decide to talk to the database, five hundred backoff timers you cannot inspect together, and no single point where you can ask *what is this recorder doing right now*. A dict of state machines driven by one loop gives you all three.

`report()` being separate is what keeps the loop honest: reconciling and reporting are different concerns, and if reporting is slow or fails, convergence must not stop.

## Step 5 — Make the bad version, on purpose

Persist actual state and watch what happens.

```python
class Persisted(Reconciler):
    """The mistake. Do not ship this."""
    def __init__(self, *a, saved=None, **k):
        super().__init__(*a, **k)
        self.actual = saved or {}          # loaded from disk on startup
```

Run it with a saved file claiming camera 1 is at revision 2:

```python
liar = Persisted(store, actuator, saved={1: {"revision": 2}})
assert liar.reconcile() == []                     # it does nothing
assert liar.status()[1][0] == CONVERGED           # and reports success
```

Real output from the test suite:

```
5. persisted actual reports a running pipeline that does not exist  <-- the bug
```

No pipeline exists. The loop is satisfied, the console is green, and the box is recording nothing. **Sit with that for a moment** — every mechanism in М9 was built so a box nobody visits could be trusted, and this one line of "optimisation" makes it confidently wrong.

Now delete the class and keep the test. The test that proves a fresh process re-derives everything is the one that stops somebody re-adding this in six months:

```python
r2 = Reconciler(store, actuator)                  # fresh process, empty actual
assert r2.reconcile() == [("start", 1)]           # rebuilds from the store
```

## Step 6 — Backoff, jitter, and the outage you cause yourself

A camera that will not start must be retried, and retrying immediately in a two-second loop is a denial-of-service attack on a device with a 200 MHz CPU.

```python
def _fail(self, cid, now):
    n = self.failures.get(cid, {}).get("n", 0) + 1
    base = min(2 ** n, self.max_backoff)
    delay = base * (0.5 + random.random() * 0.5)      # jitter: 50-100% of base
    self.failures[cid] = {"n": n, "retry_at": now + delay, "delay": delay}
```

The exponential part is obvious. **The jitter is the part that matters, and it is not about being polite to one camera.**

A network switch reboots. Two hundred cameras drop at the same instant, fail at the same instant, and back off by the identical amount. Twelve seconds later, two hundred RTSP connections open simultaneously — into a switch that has just finished booting. They fail together, back off together, and do it again, harder. **You have built a self-inflicted outage with a rhythm**, and it recovers only when something outside the system breaks the symmetry.

Jitter breaks it on the first retry. Real output, 200 cameras failing at the same moment:

```
6. backoff + jitter ............... OK (200 cameras, first retry spread 1.00-2.00s)
```

Two hundred retries spread across a full second instead of arriving in the same millisecond. That is the entire fix, it is one multiplication, and it is missing from most retry code.

Here is the whole suite, and its real output:

```
1. converge, then idle ............ OK
2. revision bump -> restart ....... OK
3. disable and delete ............. OK
4. restart re-derives actual ...... OK
5. persisted actual reports a running pipeline that does not exist  <-- the bug
6. backoff + jitter ............... OK (200 cameras, first retry spread 1.00-2.00s)
7. lagging vs stalled ............. OK

all 7 pass
```

Test 1's second half is the one people forget to write: **a converged loop must be silent.** `reconcile()` on an already-converged world must return no actions at all. A loop that restarts something on every pass converges in the same sense a car with the handbrake on is stationary.

## Step 7 — The vocabulary

The console will need words, and picking them now stops them being invented ad hoc later:

| Word | Means | Test |
|---|---|---|
| **converged** | what is running is what was asked for | `observed_revision >= revision` |
| **lagging** | a change has not been applied yet, and that is normal | `observed_revision < revision`, few failures |
| **stalled** | it has been trying and failing | repeated failures past a threshold |
| **unreachable** | the recorder itself is not reporting | no status write within a window |

The distinction that costs you if you skip it is **lagging versus stalled**. Both mean *not applied*. One is the system working — a change made 300 ms ago has not reached the worker — and one is the system failing. An interface that shows the same amber for both trains its operators to ignore amber, which is how a genuinely stalled camera stays stalled for a fortnight.

And one thing to keep *out* of that list. `unreachable` is about the recorder; the other three are about a camera. **A camera that cannot converge because the disk is full is not a fourth phase** — it is `lagging` with a *reason*. Positions and reasons are different axes, and Lesson 9 keeps them apart properly. Kubernetes shipped a phase enum and then documented why it was a mistake; this is the cheap moment to not repeat it.

**Deliverable:** a worker that converges a fake world, is silent once converged, spreads its retries, and passes a test that kills it mid-change and confirms it rebuilds from the store alone.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| The loop restarts a camera on every pass | Comparing `!=` instead of `>=`, or forgetting to record `observed_revision` after a successful start. Test 1's silence assertion catches this. |
| Deletions are never noticed | The stop loop is iterating `desired` instead of `self.actual`. You cannot find a deletion by looking at rows that exist. |
| Everything retries in lockstep after a network fault | No jitter, or jitter applied to the wrong term. Print the spread across 200 simulated cameras — it should be a range, not a value. |
| A camera in backoff is never retried | `retry_at` is compared against a different clock from the one passed to `reconcile()`. Pass `now` explicitly, as here, so tests can control it. |
| Convergence stops after a Postgres restart | You are relying on `LISTEN/NOTIFY` alone. Step 3. |
| Status flaps between lagging and converged | `report()` and `reconcile()` are racing on the same dict. Reporting reads a snapshot. |
| The loop is fine in tests and slow in production | One task per camera. Step 4. |

## Recap

- **Build the loop before the thing it controls.** Against a `print()`, the control logic is the only thing that can be wrong.
- A reconciler compares two sets and acts on the difference. It does not process events — which is why it survives missed events, late events, duplicate events and restarts.
- Persisting actual state produces a **cache that lies**: a green console over a box recording nothing. It is silent, plausible, and survives restarts.
- **The timer is correctness; the notification is latency.** `NOTIFY` is not durable — a disconnected listener misses notifications forever, so a NOTIFY-only system stops converging without saying so.
- One asyncio task per *concern*, not per camera. Per-camera state is a state machine.
- **Jitter is not politeness.** Without it, two hundred cameras retry in lockstep and turn a switch reboot into a self-sustaining outage.
- `lagging` and `stalled` both mean *not applied*; one is normal and one is not, and showing the same colour for both trains operators to ignore it.

## Exercises

1. Add `pause` — a camera that stays configured but stops recording, without being deleted. Decide whether that is a new phase or a value of `enabled`, and defend the answer against the positions-versus-reasons rule.
2. Write a test that changes a row *while* `reconcile()` is mid-pass, and assert convergence within two passes. Then say what would have to be true for the loop to converge in one.
3. Instrument the loop: count passes, actions, and time per pass, and print them every ten seconds. Predict which number moves first when a camera goes offline. (М13 turns these into metrics; having predicted them first is the point.)
4. Set `max_backoff` to 10 seconds and simulate a camera offline for an hour. Count the connection attempts. Then argue for a specific cap and say what it costs when the camera comes back.
5. Delete the jitter, simulate a switch reboot with 200 cameras, and plot retry times. Keep the plot — it is the most persuasive two lines of code in this module.

## Where this is going

The loop works and does nothing useful: its actuator prints. Every decision in it is now visible and tested, which is exactly the state you want before adding a media framework.

**Lesson 7 swaps the `print()` for GStreamer** — fifty pipelines in one Python process, the GIL boundary demonstrated by deliberately crossing it and watching the worker fall over, and the `watchdog` element doing stall detection in C so that Python never touches a buffer. It is also where М9's spool quietly becomes an archive: the same sink writes the same files, and instead of deleting each one on upload, you write an index row.
