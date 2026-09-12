# Lesson 9 — What the Console Shows, and What Python Stops Being Right For

**Module:** EdgeVMS — the box owns its truth (Module 9)
**You will build:** the operator's view — one query answering *is this camera actually recording?* — behind a login; and a written argument for the production language split.
**Time:** ~120 minutes.

## Why this lesson exists

Two things close this module, and they are less unrelated than they look.

The first is that everything built so far is invisible. The recorder converges, survives four kinds of failure, and the only way to see any of it is `psql`. A console is not decoration: it is where the desired/actual distinction stops being an architecture diagram and becomes something an operator can act on — or, done badly, a screen that shows amber for both "changed 300 ms ago" and "broken since Tuesday".

The second is the question you should be asking by now. Fifty pipelines in Python worked. Does that mean the product should ship in Python? **No** — and the interesting part is precisely which parts change, because it turns out to be a much smaller set than "the slow ones", and the reconcile loop you wrote by hand is not in it.

> **What you can verify without hardware.** The query, the login and the status vocabulary run against Postgres and need nothing else. The rewrite argument is a decision record — sourced from the binding projects' own documentation, and marked where it is a judgement rather than a fact.

## Prerequisites

- **Lesson 5** — the schema, the `operators` table, and the operator/controller column split.
- **Lessons 6–8** — the loop, the real actuator, and the status vocabulary.
- **М8 Lessons 6–8** — FastAPI, the credential boundary, and the timeline page. The console is that work, pointed at a local database instead of Kinesis.

## Learning objectives

1. Write one query that answers *is this camera recording, and how far behind is it?*
2. Keep **positions** and **reasons** on separate axes, and say why merging them is a design error with a documented precedent.
3. Put a login in front of the console and mark it honestly as temporary.
4. State what an operator is never asked to decide, and the four places physics leaks anyway.
5. Argue the production language split from evidence, and identify what a rewrite would *not* touch.

---

## Step 1 — One query, not three round trips

The operator's question is single: *is camera 7 recording?* If answering it takes three queries and some application logic, the console will drift out of agreement with itself.

```sql
CREATE VIEW camera_status AS
SELECT c.id,
       c.name,
       c.site_id,
       c.enabled,
       c.revision,
       c.observed_revision,
       c.revision - c.observed_revision              AS lag,
       c.phase,
       c.last_seen,
       s.last_segment_end,
       now() - s.last_segment_end                    AS silent_for
FROM   cameras c
LEFT JOIN LATERAL (
    SELECT upper(span) AS last_segment_end
    FROM   segments
    WHERE  camera_id = c.id
      AND  lower(span) > now() - interval '2 hours'   -- prunes partitions
    ORDER  BY lower(span) DESC
    LIMIT  1
) s ON true;
```

Two things in there are load-bearing.

**`lag` is a number, not a boolean.** `revision - observed_revision` tells an operator *how far behind* rather than merely *behind*, which is the whole reason Lesson 5 made `revision` an ordered integer instead of a hash. On a dashboard, a lag of 1 that clears in a second and a lag of 1 that has sat there for an hour look completely different, and a boolean cannot tell them apart.

**`lower(span) > now() - interval '2 hours'` is the pruning bound from Lesson 5**, and it is why this view stays fast after two years of monthly partitions. Without it, every console page-load opens every partition's index. It looks like a redundant clause and it is the difference between one index scan and sixty — **put a comment on it or somebody will remove it as dead code.**

The killer column is `silent_for`. A camera can be `converged`, `enabled`, phase `running`, with `lag = 0` — and have written nothing for forty minutes. Every field agrees the system is healthy, because every field is describing *the control plane*. `silent_for` is the only one describing the product.

## Step 2 — Positions and reasons are different axes

The tempting design is one enum:

```
pending | starting | running | failed | unlicensed | no_storage | unreachable
```

Do not. The first four are *where the object is*; the last three are *why it cannot get further*, and they compose with the first four rather than replacing them. A camera can be `starting` **and** unlicensed. Flattening them forces you to invent `starting_but_unlicensed` and then discover you need it for every combination.

Kubernetes shipped exactly this enum and then documented why it was wrong; conditions were added alongside it and the phase field survives mostly as compatibility. You get to skip that particular decade:

| Axis | Values | Answers |
|---|---|---|
| **Phase** (position) | `pending`, `starting`, `running`, `failed` | Where is it? |
| **Conditions** (reasons) | `licensed`, `storage_available`, `camera_reachable`, `within_retention` | Why can it not get further? |

```sql
CREATE TABLE camera_conditions (
    camera_id  bigint NOT NULL,
    condition  text   NOT NULL,
    status     boolean NOT NULL,
    reason     text,
    since      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (camera_id, condition)
);
```

`since` is the field that makes this worth building. *"Not recording"* is a support ticket; *"not recording, storage unavailable since 14:02"* is a fix. And the console rule follows directly: **never show a red phase without the condition that explains it.**

Then the vocabulary from Lesson 6, now with a home on the screen:

| Word | Test | Shown as |
|---|---|---|
| **converged** | `observed_revision >= revision` | green |
| **lagging** | behind, few failures, recent | amber, **with the lag number** |
| **stalled** | behind, repeated failures | red, **with the failing condition** |
| **unreachable** | no status write within the window | grey — the *recorder*, not the camera |

That last row is about the recorder, and greying it out is deliberate: when the AppHost is not reporting, you do not know what the cameras are doing. Showing them green because they were green four minutes ago is precisely the lie Lesson 6's persisted-actual bug produced, arriving through the interface instead of the data model.

### The recorder's two exported signals

The console renders these for a human. The same two numbers are what the recorder **exports** for a machine, and М9 Lesson 4 already established the pattern with `spool_oldest_seconds`:

| Signal | Question it answers | Why this one |
|---|---|---|
| **`camera_lag`** = `revision - observed_revision` | is the control plane keeping up? | a *distance*, so a lag of 1 clearing in a second is visibly different from a lag of 1 stuck for an hour — which is why Lesson 5 made `revision` an ordered integer |
| **`camera_silent_seconds`** = now − `last_segment_end` | **is footage arriving?** | the only signal here describing the *product* rather than the control plane |

Alarm on the second. A camera can be `converged`, `enabled`, phase `running`, `lag = 0` — every control-plane field agreeing the system is healthy — and have written nothing for forty minutes. That is М9 Lesson 3's rule in its third instance: **alarm on the product, not on the process.**

And a warning about the first that М13 spends a whole section on: `camera_lag` is **per camera**, so at a thousand cameras it is a thousand time series. That is exactly how a metrics system becomes more expensive than the thing it watches. Export the *distribution* — how many cameras are lagging, and the worst lag — and keep the per-camera number in the database where the console already reads it. **A metric is not a database, and the temptation to make it one is what kills a monitoring system.**

## Step 3 — A login, marked temporary

```python
@app.post("/login")
async def login(form: LoginForm, db=Depends(get_db)):
    row = await db.fetchrow("SELECT id, pwhash FROM operators WHERE username=$1",
                            form.username)
    if row is None or not argon2.verify(row["pwhash"], form.password):
        raise HTTPException(401)                    # same error for both cases
    return {"token": issue_session(row["id"])}
```

One account, provisioned by hand at commissioning, all capabilities. No policy — the `grants` table from Lesson 5 exists and nothing consults it yet.

Three things worth being explicit about:

**No VMS ships with an open API**, and it costs an hour to do the minimum here rather than treating authentication as somebody else's module.

**The same 401 for an unknown user and a wrong password.** Distinguishing them hands an attacker a username oracle for free.

**This account is superseded in М12, not extended.** With N recorders, a local `operators` table means N accounts for one person, N password hashes to steal, and — the part that matters — **a grant that expires attached to a credential that does not.** М12 Lesson 4 removes the hash from the recorder entirely: the recorder holds an issuer's *public key*, verifies a short-lived signed token offline, and looks up its own grants for the subject that token names. A student who keeps this table and adds a `node_id` column has built the problem on purpose.

**This is the course's fourth temporary secret**, and the count is deliberate — М9's AWS credentials, М9's database password, this operator account, and М12 will add a per-recorder credential and a self-signed domain CA. **М12 collects all five** — this account becomes a token from the domain signer (М12 Lesson 4), and the self-signed CA is the one stand-in that gets *promoted* rather than replaced, because it turns out to be the customer's own root. Naming a stand-in where it appears is what stops it becoming permanent by silence — or, in that one case, what lets it become permanent on purpose.

This is also the last module where there is exactly **one** surface to protect. М11 gives every recorder its own API, which is N endpoints where there used to be one, and that is where authorization stops being trivial.

## Step 4 — What the operator is never asked

The instinct is right: an operator wants to assign cameras, not machines. The useful part is knowing exactly where that stops being true.

**Which recorder owns a camera is decided for the operator, never by them.** Lesson 5 made that concrete — the `cameras` table has no recorder column a client may write. An operator assigns a camera to a **site**, which is where it physically is; the controller turns that into placement.

But servers are physical, and physics leaks in four places where hiding it would be a lie:

| Where it surfaces    | What the operator actually needs to know                                                                                                                                    |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Capacity**         | *"You cannot add camera 1001."* Expressed as **the system is full**, not *recorder 3 is full* — but the number has to come from somewhere real                                  |
| **Storage locality** | Recordings live on the **server** that wrote them, and a recorder moving does not move them. A dead server means unavailable footage, and that must be visible *before* it dies |
| **Failure grouping** | When a server fails, its recorders move and two hundred cameras go red together. The console must show **one cause**, not two hundred faults                                    |
| **Reachability**     | A camera on an isolated VLAN may be reachable from only some servers. The operator expresses this as a **site**; the controller turns it into a placement constraint        |

> **Site is a first-class operator concept. Server is not, and recorder barely is.**

The same relationship a filesystem has to disks: you do not assign files to spindles, and you certainly see the spindle when one fails.

None of the four bites in this module — one recorder, one server. All four bite in М11, and the schema that survives that is the one that never let a client write placement in the first place.

**Write the list down as a deliverable.** "Every decision the operator is never asked to make" is a one-page document, and it is the most useful page in a product specification, because every entry is a support call that will not happen and a form field that does not exist.

## Step 5 — What Python stops being right for

The design is proven. Now be honest about the language.

Three things end Python's case for the *product*, and none of them is "Python is slow":

**The per-process baseline `B` is larger than a compiled worker's.** You measured it in Lesson 7. Multiply by the number of shards on a server and it is memory that could have been page cache for video.

**One segfault takes the whole shard.** True in any language; the difference is that a compiled worker with no interpreter and no binding layer has meaningfully fewer places to segfault.

**Any requirement for per-frame work in Python is fatal**, by Lesson 7's table. Today the pipeline never decodes. The moment a product manager asks for on-box analytics with a Python model in the path, the design is over — and "we cannot do that" is a bad answer to give at that point.

### The split

**Go for the controller.** It is a gRPC-and-Postgres service — Go's centre of gravity — and its per-frame exposure is exactly zero, because the controller never touches a buffer. Everything Lesson 6 built maps across without redesign.

**C++ for the media worker.** GStreamer is a C library, so C++ calls it with **no binding layer at all**. That is not a performance argument; it is a *whole class of problem that stops existing* — no GIL, no cgo pointer rules, no binding maintained by three volunteers.

### Binding reality, because it is easy to choose wrong

| Binding                   | Status                                                                                    |
| ------------------------- | ----------------------------------------------------------------------------------------- |
| **`gstreamer-rs`** (Rust) | Maintained by GStreamer's own developers; the strongest non-C binding by some distance    |
| **`go-gst`**              | The live Go binding, successor to `tinyzimmer/go-gst`. Real, and a much smaller community |
| **`gstreamermm`** (C++)   | **Archived.** C++ means calling the C API directly — which is what C++ projects do anyway |

That last row surprises people and then stops being surprising: a C++ wrapper around a C API adds a layer whose only job is to be idiomatic, and GStreamer's C API is already object-oriented in all but syntax.

If the team is Rust-shaped rather than C++-shaped, `gstreamer-rs` is a genuinely defensible substitution for the worker and the rest of this argument is unchanged.

### What the rewrite does *not* touch — the point of having written it in Python

| Survives unchanged                                               | Gets rewritten              |
| ---------------------------------------------------------------- | --------------------------- |
| The schema                                                       | The actuator                |
| The reconcile loop's **logic**                                   | Its implementation language |
| The state machine and its transitions                            |                             |
| The backoff and jitter policy                                    |                             |
| The desired/actual contract, and `observed_revision >= revision` |                             |
| The status vocabulary and the conditions model                   |                             |

**Only the actuator changes.** Everything expensive to get right — and everything that was wrong in your first draft — is language-independent, and you established all of it in a language where a wrong idea costs ten minutes instead of an afternoon.

That is the honest defence of building it in Python first, and it is not "Python is easier". It is that **the risky part of this system was never the code; it was the design**, and you de-risked the design cheaply. Lesson 7's backoff policy needing no changes when the actuator went from `print()` to GStreamer was the same property, demonstrated one layer down.

### The claim, measured

That argument is cheap to make and cheap to check, so the module checks it. [`recorder-go/`](./recorder-go/README.md) ports Lesson 6's reconciler to Go — the same `>=`, the same stop loop over *actual*, the same jitter — and runs **the Python suite's seven tests plus the cap test against it, unchanged in meaning**. All eight pass. Then it puts a controller-shaped process in each language at idle — fifty converged cameras, a status map, a JSON encoder, an HTTP listener, no GStreamer in either — and reads PSS:

| | Go | Python |
|---|---|---|
| Controller at idle, 50 cameras | **6.0 MB** | **25.7 MB** |
| Bare runtime | 1.9 MB | 6.2 MB |
| Deployable artifact | one static binary, 5.5 MB; arm64 cross-compiled in one command, 5.1 MB | interpreter 55 MB + ≈ 28 MB of packages, in a rootfs М9 ships twice and signs |

Four times the controller's share of `B`, and the thing М9's bundle carries shrinks by an interpreter. What the table does *not* show is the media worker, because GStreamer's 24 MB of libraries cost the same in every language and the per-frame rule from Lesson 7 survives in Go — which is why the worker is the C++ half of the split, not the Go half.

**Deliverable:** the console view behind a login, and a written statement of every decision the operator is never asked to make. Then `go test ./reconciler/` in `recorder-go/` — and `measure.sh`, to produce the table above on your own hardware.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| The status view gets slower every month | The pruning bound was removed from the lateral join. Step 1. |
| Everything shows amber | `lagging` and `stalled` collapsed into one state. Split them on failure count, and show the lag number. |
| A camera shows green but records nothing | You are reading control-plane fields only. `silent_for` is the column that catches this. |
| Conditions and phase disagree | Something is writing `phase` from a condition. They are separate axes — a condition never sets a phase. |
| The console shows stale green during an AppHost outage | Not handling `unreachable`. When the recorder stops reporting, you do not know — say so, do not imply health. |
| Login works with any password | `argon2.verify` argument order, or an exception being swallowed. Test the negative case explicitly. |
| The operator asks which recorder a camera is on | The UI leaked a controller-owned field. Step 4. |

## Recap

- One query, not three round trips — and `lag` as a **number**, because ordering is what `revision` was made an integer for.
- The recorder exports exactly two signals: **`camera_lag`** as a distribution, never per-camera, and **`camera_silent_seconds`**, which is the one to alarm on.
- **`silent_for` is the only column describing the product.** Everything else describes the control plane, and all of it can look healthy while nothing records.
- **Positions and reasons are different axes.** Phase says where an object is; conditions say why it cannot get further, and `since` turns a ticket into a fix. Kubernetes shipped the merged enum and documented why it was wrong.
- `unreachable` greys the recorder out rather than showing its cameras green. Stale green is Lesson 6's lying cache, arriving through the interface.
- The login is the course's **fourth temporary secret**, named where it appears. This is the last module with exactly one surface to protect.
- **Site is a first-class operator concept; server is not, and recorder barely is** — but physics leaks in four places, and hiding it there would be a lie.
- Python ends for three reasons: baseline memory, blast radius, and per-frame work being fatal. **Go for the controller, C++ for the worker** — and `gstreamermm` is archived, so C++ means the C API directly.
- **Only the actuator gets rewritten.** The schema, the loop, the state machine, the backoff policy and the desired/actual contract all survive — which is the real defence of prototyping in Python.

## Exercises

1. Write the "decisions the operator is never asked to make" page. Keep it to one side of paper. Then, for each entry, name the support call it prevents.
2. Add a condition the module has not needed yet — `within_licence` — wire it to nothing, and show it in the console. Then explain why a condition that is always true is still worth having in the model.
3. Build the failure-grouping view: given a server with 200 cameras, produce **one** row saying the server is down rather than 200 rows saying cameras are unreachable. This is М11's console, sketched a module early.
4. Take one non-trivial piece — the backoff policy — and port it to Go. Time yourself. That number is the honest cost of the rewrite for the parts that are pure logic, and it is smaller than people assume.
5. Argue the opposite case: keep the media worker in Python and ship it. Be specific about camera counts, memory budget and what feature request ends it. A good version of this argument is worth having before somebody makes it badly in a meeting.

## Where this is going

The module is complete. `INSERT INTO cameras` starts a recording, `DELETE` stops it, four kinds of failure are survived and asserted, and an operator can see all of it behind a login.

**And there is exactly one box.** Every claim here — one writer, one AppHost, a convention instead of a fencing token, one API to protect — holds only because there is nothing to disagree with.

[**М11 — ClusterVMS**](../М11_ClusterVMS/module-design.md) adds the second box, and everything gets harder in one specific way: a recorder becomes a scheduler allocation that **moves between servers**, carrying its cameras with it. Nothing you built here changes — that is the design working — but two instances of the same recorder can briefly exist during a failover, and Lesson 8's one-line convention has to become a fencing token that the archive itself enforces.
