# Lesson 5 — The Database the Cloud VMS Didn't Need

**Module:** EdgeVMS — the box owns its truth (Module 9)
**You will build:** the recorder's schema — configuration, archive index and events — with time partitioning, a GiST-indexed timeline query, and migrations that run unattended at boot on a box nobody visits.
**Time:** ~120 minutes.

## Why this lesson exists

М8's specification forbade a database, and it was right to. Kinesis held the configuration *and* the archive; the VMS was a client of somebody else's truth, and adding a local database would have created a second place for the same facts to live.

On a box, that argument inverts. There is no Kinesis holding your camera list. If the box does not remember what it is supposed to be doing, then nothing does — and М9 made that concrete by giving you an appliance whose entire configuration was a file you edited by hand and a container you restarted.

This lesson is where the box starts owning its own truth. It is mostly schema, and schema is where a system's assumptions become permanent, so it is worth more care than its lesson number suggests.

> **What you can verify without hardware.** All of it. Everything below was run against **PostgreSQL 16.13** while writing this lesson, and every timing and byte count printed is real output rather than an estimate. You need no appliance and no cameras.

## Prerequisites

- **М9 Lesson 4** — Quadlet, and the three-way boundary that decides where `PGDATA` goes. This lesson depends on that answer being right.
- **М8 Lesson 6** — configuration is read from the environment; credentials never live in the image.
- SQL at the level of `CREATE TABLE`, `JOIN` and `INSERT`. No prior Postgres administration is assumed.
- Postgres 14 or later. On the bench: `podman run -d --name pg -e POSTGRES_PASSWORD=... -v /data/pg:/var/lib/postgresql/data postgres:16`.

## Learning objectives

1. Explain why М8 was right to forbid a database and why the answer flips on-premises.
2. Separate configuration from observation, and justify the split by their properties rather than by taste.
3. Write a `tstzrange` + GiST schema that answers *what footage covers this window* directly.
4. Partition by time from day one, and demonstrate why `DELETE` is not a retention strategy.
5. Distinguish operator-owned from controller-owned columns, and say why the distinction is a security boundary.
6. Write migrations that are safe to run unattended at boot.

---

## Step 1 — One database, and this recorder owns it

Before any SQL, one decision, because it shapes everything after it and it is the opposite of what most control-plane tutorials do.

**This database is not a cache.** The recorder is the authority for its own configuration. Nothing above it ever writes these rows — not in this module, and not in М11 when there are twenty recorders and a layer above them. That layer is a *directory*: it holds a list and a durable copy, and it may be unreachable while an operator edits a camera right here.

The consequence you can feel later: when М11 moves this recorder to a different server, its configuration travels with it and **nothing rewrites who owns what**. Ownership never changed, because ownership was always local.

So: **there is no second database here, and none arrives later.** Worth saying plainly, because the instinct after a few years of building control planes is to put a central one in.

## Step 2 — Three kinds of data, one engine

The recorder holds three things. They share an engine and almost nothing else, and Lesson 8 depends on you having noticed the difference:

| | **Configuration** | **Archive index** | **Events** |
|---|---|---|---|
| Is | what an operator asked for | what this box observed | what this box noticed |
| Written by | people, rarely | the AppHost, constantly | detectors and the AppHost |
| Rate | a few rows a day | ~100 rows/second at scale | bursty, high volume |
| Read | on every reconcile | on every timeline query | rarely, and by search |
| If lost | **the recorder is gone** | rebuildable by scanning segments | acceptable — they are observations |
| Retention | forever | a rolling window | a rolling window |

That bottom-left cell is the one that matters. Configuration is the only thing here that cannot be re-derived from something else, which is why М11 replicates it upward and ignores the rest.

### Why Postgres and not SQLite

The honest version of this argument, because "Postgres is more serious" is not one.

If the recorder held only configuration — a few hundred rows, one writer — SQLite would be plenty and would save you a service. But the index and the events are in the picture regardless, and they change the question:

- **Concurrency.** SQLite permits one writer at a time; WAL lets readers run alongside a writer but does not change that. Twenty media workers writing index rows, an event stream, and the AppHost reading is real contention.
- **Partitioning is the deciding feature**, and Step 5 makes it concrete with numbers.
- **Types that match the work.** `tstzrange` with a GiST index answers М8's timeline query directly; JSONB carries event payloads that differ per detector.
- **One engine, one skillset.** The same `psql`, `pg_dump`, backup story and client library. Students learn one thing; whoever operates the appliance operates one thing.

So a recorder is running Postgres either way, and putting configuration anywhere else buys nothing.

## Step 3 — The configuration schema

```sql
CREATE TABLE sites (
    id    text PRIMARY KEY,
    name  text NOT NULL
);

CREATE TABLE cameras (
    id              bigserial PRIMARY KEY,
    site_id         text REFERENCES sites(id),
    name            text    NOT NULL,
    rtsp_url        text    NOT NULL,
    enabled         boolean NOT NULL DEFAULT true,
    retention_days  int     NOT NULL DEFAULT 30,

    -- controller-owned from here down
    revision          bigint      NOT NULL DEFAULT 1,
    observed_revision bigint      NOT NULL DEFAULT 0,
    phase             text        NOT NULL DEFAULT 'pending',
    last_seen         timestamptz
);
```

### The credential hiding in `rtsp_url`

Before the column split, deal with the thing that column actually contains. An RTSP URL conventionally carries the camera's credentials inline:

```
rtsp://admin:Sup3rSecret@10.0.0.41/stream1
```

So a schema with `rtsp_url text NOT NULL` is a schema that stores **every camera's password in plaintext**, at every site, times a thousand cameras. Nobody decided that; it arrived through a field name.

It is worth being precise about whose problem this is, because it changes the answer:

> **These are not the product's secrets. They are the customer's** — and unlike a database password you provisioned, you cannot rotate them, you did not choose them, and they are very often the same password on every camera the installer touched.

Split the credential out of the URL and encrypt it:

```sql
CREATE TABLE cameras (
    ...
    rtsp_url        text NOT NULL,          -- scheme://host/path, no credentials
    cred_username   text,
    cred_secret     bytea,                  -- encrypted; never selected into a log
    ...
);
```

**The hard part is not the encryption, it is where the key lives**, and М9 Lesson 4's boundary already tells you what is wrong with the easy answer. A key in `/data/config/agent.env` travels with the database in every backup a support engineer takes, so it is not a second factor — it is the same factor in a different file.

Two answers that actually work on an appliance:

- **The TPM**, where М9's hardware allows: seal the column key so it can only be unwrapped by *this box, running this software*. A stolen disk yields ciphertext.
- **Delivered at runtime** by whatever starts the recorder, and held only in memory. On one box that is systemd reading from the data partition — no better than the file. From М11 it is a Nomad Variable, which is encrypted, ACL'd, and genuinely not in your `pg_dump`.

**This module ships the first, weaker version and says so**: the column is encrypted, the key is on the data partition, and the improvement is named as a debt. What it must not do is leave the credential in a URL string, because that version cannot be improved later without touching every row and every log that has already been written.

> **The rule worth carrying: a secret in a URL is a secret in every log line, every error message and every stack trace that URL ever appears in.** Lesson 7 formats this string into a GStreamer pipeline description; Lesson 8 prints pipeline errors. Both would have leaked it.

### The line through the middle of that table

The comment is not decoration. It is the most important thing in the lesson.

| Operator-owned | Controller-owned |
|---|---|
| `enabled`, `rtsp_url`, `cred_username`, `cred_secret`, `retention_days`, `site_id`, `name` | `revision`, `observed_revision`, `phase`, `last_seen` |
| Written by people through a form | Written by machines, by observation |
| Appear as form fields | **Never** appear as form fields |

An API that lets a client set `phase` has handed the client the ability to lie about reality. An API that lets a client set `observed_revision` has handed it the ability to claim a change was applied that never was. These are not hypothetical: they are the default outcome of generating CRUD endpoints from a table definition, which is why the split has to be in your head before the endpoints exist.

**There is also no recorder column here that a client may write**, and there will not be one in М11 either. Which recorder owns a camera is decided *for* the operator. An operator assigns cameras to a **site** — where they physically are — and the controller turns that into a placement decision of its own.

### `revision` is an integer, not a hash and not a timestamp

```sql
CREATE FUNCTION bump_revision() RETURNS trigger AS $$
BEGIN
    NEW.revision := OLD.revision + 1;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER cameras_bump BEFORE UPDATE ON cameras
    FOR EACH ROW WHEN (
        OLD.enabled        IS DISTINCT FROM NEW.enabled        OR
        OLD.rtsp_url       IS DISTINCT FROM NEW.rtsp_url       OR
        OLD.cred_username  IS DISTINCT FROM NEW.cred_username  OR
        OLD.cred_secret    IS DISTINCT FROM NEW.cred_secret    OR
        OLD.retention_days IS DISTINCT FROM NEW.retention_days OR
        OLD.site_id        IS DISTINCT FROM NEW.site_id        OR
        OLD.name           IS DISTINCT FROM NEW.name
    )
    EXECUTE FUNCTION bump_revision();
```

**The `WHEN` clause names the operator-owned columns, and that is not fussiness.** The first draft of this lesson wrote `WHEN (OLD.* IS DISTINCT FROM NEW.*)`, which reads well and is wrong: the AppHost's own status write — `observed_revision`, `phase`, `last_seen` — is an `UPDATE` too, so every report bumped `revision`. Measured: report `observed_revision = 2` and `revision` goes to 3; the lag is 1 forever and the recorder chases its own tail. The line through the middle of the table is enforced here, by the database, rather than remembered by whoever writes the next `UPDATE`.

Monotonic, controller-assigned, one per object. Three candidates and only one survives:

- **A hash** tells you two states differ. It cannot tell you which is newer, so it cannot express *how far behind* a worker is — only *whether* it is behind.
- **A timestamp** depends on clocks agreeing, and they do not.
- **An integer** orders. `observed_revision >= revision` means applied, and `revision - observed_revision` is a lag you can put on a dashboard.

That comparison is the only definition of "applied" this course uses, at every layer.

## Step 4 — The archive index, and М8's query answered directly

```sql
CREATE TABLE segments (
    camera_id  bigint      NOT NULL,
    span       tstzrange   NOT NULL,
    path       text        NOT NULL,
    bytes      bigint      NOT NULL,
    epoch      int         NOT NULL DEFAULT 1
) PARTITION BY RANGE (lower(span));
```

`tstzrange` stores a half-open interval — `['2026-09-14 09:00', '2026-09-14 09:10')` — and Postgres has operators over it. The one that matters is `&&`, "overlaps", which is exactly М8's timeline question: *which segments cover this window?*

Two partitions and the index:

```sql
CREATE TABLE segments_2026_09 PARTITION OF segments
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE TABLE segments_2026_10 PARTITION OF segments
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');

CREATE INDEX segments_span_gist ON segments USING gist (span);
```

A GiST index on a partitioned table works, and creating it on the parent creates one on each partition automatically. Check that it is used:

```sql
EXPLAIN (ANALYZE, COSTS OFF)
SELECT camera_id, span, path FROM segments
WHERE span && tstzrange('2026-09-14 09:00','2026-09-14 11:00')
  AND camera_id = 1;
```

Real output, against ~17,000 segments:

```
 Append (actual time=0.060..0.064 rows=12 loops=1)
   ->  Index Scan using segments_2026_08_span_idx on segments_2026_08 (actual time=0.024..0.024 rows=0 loops=1)
         Index Cond: (span && '["2026-09-14 09:00:00+00","2026-09-14 11:00:00+00")'::tstzrange)
   ->  Bitmap Heap Scan on segments_2026_09 (actual time=0.035..0.038 rows=12 loops=1)
         ->  Bitmap Index Scan on segments_2026_09_span_idx (actual time=0.026..0.026 rows=24 loops=1)
```

Sub-millisecond, index used. But look at the first branch: **it scanned the August partition too**, and found nothing.

### The pruning trap, which is worth meeting now

Partition pruning needs a predicate on the **partition key**. The key here is `lower(span)`; the query filters on `span && ...`, and Postgres cannot derive one from the other. So it opens every partition's index. With two partitions that is invisible. With sixty — five years of monthly partitions — it is sixty index scans to answer a question about one afternoon.

Add the bound explicitly and it prunes:

```sql
SELECT camera_id, span, path FROM segments
WHERE span && tstzrange('2026-09-14 09:00','2026-09-14 11:00')
  AND lower(span) >= '2026-09-14 08:00'
  AND lower(span) <  '2026-09-14 12:00';
```

```
 Aggregate (actual time=0.057..0.057 rows=1 loops=1)
   ->  Bitmap Heap Scan on segments_2026_09 (actual time=0.045..0.050 rows=24 loops=1)
```

One partition. The extra clause is redundant *logically* and essential *operationally* — segments have a known maximum length, so widening the window by that much is always safe. **Put this in the query builder once, with a comment, or somebody will remove it as dead code.**

## Step 5 — Events, and why they are not metrics

```sql
CREATE TABLE events (
    id         bigserial,
    at         timestamptz NOT NULL DEFAULT now(),
    camera_id  bigint,
    kind       text  NOT NULL,
    payload    jsonb NOT NULL DEFAULT '{}'
) PARTITION BY RANGE (at);

CREATE INDEX events_payload_gin ON events USING gin (payload);
```

JSONB because detectors differ: a motion event carries a region, a licence-plate detector carries a string and a confidence, and next year's detector carries something nobody has designed yet. A column per detector is a migration per detector.

> **Events are product data: an operator searches them. Metrics are operational data: an engineer alarms on them.**

They look alike and they are not. "Camera 7 went offline at 14:02" is an event a security manager searches for next Tuesday. "The reconcile loop is taking 400 ms" is a metric an engineer alarms on and nobody ever searches. Different consumers, different retention, different modules — М13 has the second kind, and mixing them produces a database full of samples nobody reads and a monitoring system that cannot answer questions about a building.

## Step 6 — Retention, and the number that decides it

Both index and events are rolling windows taking on the order of a hundred rows a second at scale. The naive retention loop is a `DELETE`, and it is worth watching it fail.

A table with 285,696 rows in the August partition:

```sql
DELETE FROM segments WHERE lower(span) < '2026-09-01';
```

```
DELETE 276768
Time: 230.735 ms
```

231 milliseconds. Fast. Now look at the disk:

```
     relname      | pg_size_pretty
------------------+----------------
 segments_2026_08 | 38 MB
```

**Nothing was freed.** The rows are dead tuples occupying exactly as much disk as before, waiting for `VACUUM` to reclaim them — and `VACUUM` is I/O you are asking a box to do at the precise moment it is running out of disk. That is why `DELETE` is not a retention strategy: it is slowest and most expensive under exactly the pressure that triggers it.

The alternative is to drop the whole partition. **One correction first, because it catches people and it caught this course's own design notes:**

```sql
ALTER TABLE segments DROP PARTITION segments_2026_08;
```

```
ERROR:  syntax error at or near "segments_2026_08"
```

**PostgreSQL has no `DROP PARTITION` statement.** That is Oracle and MySQL syntax. In Postgres it is two steps:

```sql
ALTER TABLE segments DETACH PARTITION segments_2026_08;   -- Time: 1.713 ms
DROP TABLE segments_2026_08;                              -- Time: 3.328 ms
```

Five milliseconds total, and the 38 MB is gone immediately — the file is unlinked, not marked reusable. Against 231 ms plus a vacuum that never got scheduled.

The `DETACH` step is not ceremony. It removes the partition from the parent first, so a query running concurrently sees a table with one fewer partition rather than a table whose partition vanished underneath it. `DETACH CONCURRENTLY` exists if you cannot take the brief lock.

**Retention therefore means creating partitions ahead of time and dropping old ones** — a scheduled job, not a delete loop. Lesson 8 makes it degrade under a full disk.

One footnote worth knowing: `pg_total_relation_size('segments')` on the parent reports **0 bytes**. The parent holds no data. Size a partitioned table by summing its partitions, or your monitoring will cheerfully report an empty archive.

## Step 7 — Operators and grants, before they are needed

```sql
CREATE TABLE operators (
    id       bigserial PRIMARY KEY,
    username text NOT NULL UNIQUE,
    pwhash   text NOT NULL
);

CREATE TABLE grants (
    id          bigserial PRIMARY KEY,
    subject     text NOT NULL,
    capability  text NOT NULL,
    valid_until timestamptz          -- unused here. See below.
);
```

On one recorder, authorization is a non-problem: one operator, all capabilities. So this lesson builds the tables and **no policy**.

`valid_until` does nothing at all in this module, and the lesson says so rather than leaving you to wonder why a column is dead. It is there because of what happens in М11. With many recorders, a *revoke* that cannot reach a recorder is silent and unbounded — the removed administrator keeps access until someone successfully talks to that box, which may be weeks, and they have every incentive not to mention it. The fix is that grants expire and are renewed, which turns an unbounded window into a number the product states.

**Adding that column now costs nothing. Adding it later is a migration against live authorization data on every appliance in the field.** That is the whole argument for dead columns you can justify, and it is a narrow licence — not a reason to speculatively add fifteen.

The one operator account and the database password are both **hand-provisioned and temporary**, exactly like М9's AWS credentials. Count them as you go: М9's AWS keys, and now two more here.

## Step 8 — Migrations that run on a box nobody visits

Your migrations do not run when an engineer types a command. They run **at boot, unattended, on an appliance in a ceiling void**, immediately after an OS update replaced the entire root filesystem.

Three constraints follow, and the third is the one people miss:

**Idempotent.** The migration runner must be safe to run on every boot, because it will be. A recorded version table plus `IF NOT EXISTS` is the floor.

**Never able to leave the box unbootable.** A migration that fails must leave the previous schema working and the box recording, because a migration failure that stops the VMS turns a schema bug into a site visit. Run migrations in a transaction where you can, and have the AppHost start read-only rather than not start, if it must.

**Forward-compatible with the running application, because of A/B.** This is the one the appliance shape forces on you. М9 Lesson 3's rollback means the *old* application may run again after the *new* schema is applied. So migrations must be **expand-only within a release**: add columns and tables, never drop or rename them in the same release that starts using them. Dropping happens a release later, once the rollback target no longer exists.

```
Release N:    add column, write both, read old
Release N+1:  read new
Release N+2:  stop writing old, drop column
```

Three releases to rename a column. That is what shipping to hardware you cannot visit costs, and М14 Lesson 3 generalises it to a fleet of customers on mixed versions.

## Step 9 — `PGDATA` on the data partition

М9's boundary, with consequences:

```ini
# /etc/containers/systemd/postgres.container
[Unit]
Description=recorder database

[Container]
Image=docker.io/library/postgres:16
Volume=/data/pg:/var/lib/postgresql/data:z
EnvironmentFile=/data/config/pg.env
PublishPort=127.0.0.1:5432:5432

[Service]
Restart=always

[Install]
WantedBy=multi-user.target
```

`/data/pg`, not anywhere in a rootfs slot. Get this wrong and the first OS update destroys every camera the operator configured — and because the new slot boots perfectly, **nothing rolls back**. М9 Lesson 3's health check would catch it only if it reaches all the way to *is footage being written*, which is precisely why that lesson pushed it that far.

`PublishPort` binds to loopback only. Nothing outside the box talks to this database in this module, and in М11 nothing outside the recorder does either.

**Deliverable:** schema and migrations applied, then simulate an A/B update — replace the root filesystem, reboot, and confirm every camera row and every segment is still there.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `no partition of relation "segments" found for row` | You inserted a segment outside every partition's range. Partitions must be created *ahead* of time — that is the scheduled job from Step 6, and its absence is a recording outage. |
| The GiST index is never used | Check you are using `&&` and not `<@` or `BETWEEN` on `lower(span)`. Also check the table has enough rows for the planner to prefer an index at all. |
| Queries slow down as months accumulate | The pruning trap from Step 4. Add the `lower(span)` bounds. |
| `pg_total_relation_size` says the archive is empty | You measured the parent. Sum the partitions. |
| Disk does not shrink after a big `DELETE` | Working as designed — see Step 6. Use partitions. |
| Migration succeeds on the bench, fails at boot on the appliance | Almost always ordering: the database container is not up yet. Express it in the unit dependencies, not with `sleep`. |
| Every camera row vanished after an OS update | `PGDATA` was in a rootfs slot. Step 9. |

## Recap

- М8 was right to forbid a database and the answer flips on-premises: if the box does not remember what it should be doing, nothing does.
- **The recorder owns this database — it is not a cache.** Nothing above ever writes these rows, which is what lets configuration travel with the recorder in М11 without ownership being rewritten.
- Three kinds of data, one engine. Only **configuration** cannot be re-derived; the index and the events can.
- **Operator-owned versus controller-owned columns is a security boundary**, not a naming convention. `phase` and `observed_revision` must never be settable by a client.
- `revision` is a monotonic integer because ordering expresses *distance*; a hash expresses only difference and a timestamp needs clocks to agree.
- `tstzrange` + GiST answers М8's timeline query directly — but **partition pruning needs a predicate on the partition key**, so bound `lower(span)` explicitly.
- **`DELETE` is not retention.** 276,768 rows deleted in 231 ms freed zero disk. Detach-and-drop took 5 ms and freed 38 MB. Postgres has no `DROP PARTITION` statement — it is `DETACH` then `DROP TABLE`.
- **`rtsp_url` was storing the customer's camera passwords in plaintext**, because the credential hides inside the URL. Split it out and encrypt it — and note the hard part is key placement, not encryption. A secret in a URL is a secret in every log line that URL reaches.
- Migrations run unattended at boot after an OS update, and A/B rollback means they must be **expand-only within a release**. Renaming a column takes three releases.

## Exercises

1. Build the partition-creation job. Decide how far ahead it runs and what happens if it fails — then work out how long the recorder keeps recording before the first `no partition found` error. That number is an alert threshold.
2. Write the query the console needs — *is camera 7 recording right now, and how far behind is it?* — as a single statement. Then explain why `revision - observed_revision` is more useful on a dashboard than a boolean.
3. Add a `CHECK` constraint that makes an empty or backwards `span` impossible to insert. Then argue whether that belongs in the database or the application, and be specific about who else writes to this table.
4. Attempt a rename the naive way — `ALTER TABLE cameras RENAME COLUMN rtsp_url TO source_url` — then work out precisely what happens if М9 Lesson 3's rollback fires afterwards. Write the three-release plan that avoids it.
5. Grep your own М8 code for places an RTSP URL reaches a log, an exception message or an HTTP response. Count them. That number is how many places the credential leaked before this lesson.
6. Measure it yourself: insert a million segment rows, `DELETE` half, and record the time *and* the disk. Then do it with partitions. Bring both numbers to Lesson 8, where the disk is already full.

## Where this is going

You have a database that knows what the box should be doing, and absolutely nothing that acts on it. `INSERT INTO cameras` currently causes precisely as much recording as it did before: none.

**Lesson 6 writes the loop that closes the gap** — and writes it with nothing on the other end, so the control logic is visible before GStreamer arrives to obscure it. It also has you make both classic mistakes on purpose, because the one that persists actual state produces a system that reports pipelines that do not exist.
