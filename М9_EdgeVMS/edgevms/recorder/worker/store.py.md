# store.py — every SQL statement the recorder runs, through asyncpg, in one file

**Role in the module.** The recorder's database access. Every statement the Worker and the console run lives here so the operator/controller column split (Lesson 5, Step 3) is enforced in exactly one place: `report()` and `set_condition()` write controller-owned columns and nothing else; `create_camera()` and `update_camera()` accept operator-owned columns and nothing else; there is no code path that lets a client write `phase`, `observed_revision`, `last_seen` or `revision`. Also owns migrations (Lesson 5, Step 8), the retention primitives `retention.py` calls through its `RetentionDb` protocol, and the console's reads (Lesson 9). `asyncpg` is imported optionally so the millisecond test suite can import the module without it. Every statement was executed twice against PostgreSQL 16.13 (README).

## Module-level names
- `log` — `worker.store`.
- `OPERATOR_COLUMNS` — the eight columns an operator may set: `site_id, name, rtsp_url, cred_username, cred_secret, enabled, retention_days, priority`. `create_camera`/`update_camera` filter their input to this tuple; the same list is the `WHEN` clause of the `cameras_bump` trigger.
- `DESIRED_SQL` — the reconciler's read: id, the operator columns (including `cred_secret`, for the pipeline only) and `revision`, ordered by id.

## `class Desired`
What the `Reconciler` reads through its `Store` protocol: a `rows` list that `Worker.reconcile_once()` refreshes from Postgres on every pass. The reconciler's own state (`actual`, `failures`) stays in memory; only desired state is persisted.

### `__init__(self)` — empty `rows`.
### `desired(self)` — returns `rows`.

## `class PgStore`
An asyncpg pool and the statements. Created by `connect`; the Worker, the console (through the Worker) and `provision.py` share one instance.

### `__init__(self, pool)` — stores the pool.
### `connect(cls, dsn, min_size=1, max_size=4)` (classmethod) — `asyncpg.create_pool`; four connections cover the four tasks plus the LISTEN connection is one of them.
### `close(self)` — closes the pool.

### `migrate(self, migrations_dir) -> bool`
Creates `schema_migrations (version int PK, name, applied_at)` if missing, reads the applied versions, and for every `NNNN_*.sql` in name order not yet applied runs the file and the bookkeeping insert in one transaction. On the first failure it logs the exception and returns `False` — the Worker carries on with the schema it has ("never leaves the box unbootable"); later files are not attempted. Expand-only within a release is a rule for the author of the `.sql` files, not something code can check. Returns `True` when everything is applied.

### `fetch_desired(self)` — `DESIRED_SQL` as a list of dicts.

### `listen(self, channel, callback)`
Acquires a dedicated connection from the pool and `add_listener`s `callback(payload)`; returns the connection so the caller can release it at shutdown. The docstring's rule: keep polling regardless — a notification missed while disconnected is gone forever.

### `report(self, rows)`
`rows` are `(camera_id, observed_revision, phase)`; one `executemany` of `UPDATE cameras SET observed_revision=$2, phase=$3, last_seen=now() WHERE id=$1`. The only writer of these three columns. The `cameras_bump` trigger's `WHEN` clause ignores this update, so `revision` does not move.

### `set_condition(self, camera_id, condition, status, reason=None)`
Upsert into `camera_conditions` on `(camera_id, condition)`; `reason` and `status` are replaced, `since` is kept when the status is unchanged and reset to `now()` when it flips — "storage unavailable since 14:02" is the sentence that turns a ticket into a fix.

### `clear_conditions(self, camera_id)` — deletes a camera's condition rows. Not called by the Worker.

### `index_segment(self, camera_id, start, end, path, size, epoch)`
`INSERT INTO segments` with `span = tstzrange(start, end, '[)')`. The spool becomes an archive here. A missing month partition makes this raise — which is why partitions are created ahead.

### `log_event(self, kind, camera_id=None, payload=None)` — `INSERT INTO events` with the payload JSON-encoded and cast to `jsonb`.

### `partitions(self, parent) -> list[(name, lower, upper)]`
Reads `pg_inherits` for the parent's children and parses each `pg_get_expr(relpartbound)` (`FOR VALUES FROM ('2026-09-01') TO ('2026-10-01')`) with a regex into dates. `retention.py` drops by these bounds.

### `ensure_partition(self, parent, month)` — `SELECT ensure_month_partition($1::regclass, $2)`; returns the partition name.
### `paths_in_partition(self, part)` — every `path` in one partition table, read *before* the drop so the files can be unlinked after.
### `drop_partition(self, parent, part)` — `ALTER TABLE parent DETACH PARTITION part` then `DROP TABLE part`: Postgres has no `DROP PARTITION`; this is ~5 ms and unlinks the relation file instead of leaving rows for VACUUM. Identifiers are quoted; `part` comes from `pg_class`, not from a client.
### `expire_rows(self, camera_id, older_than) -> list[str]` — `DELETE … WHERE camera_id=$1 AND lower(span) < $2 RETURNING path`: per-camera retention inside a live partition. Rows first, files after — a crash leaves orphans, not lies.
### `oldest_segments(self, limit, priority_first) -> list[dict]` — the disk-full policy's candidates: segments joined with cameras, ordered by `c.priority ASC, lower(span)` (by_priority) or `lower(span)` alone (degrade_retention), first `limit`. No partition-pruning predicate: this is a scan across all partitions, run only under pressure.
### `delete_segment(self, path)` — one index row by path.
### `retention_days(self) -> dict[id, days]` — every camera's window.
### `indexed_paths_under(self, prefix) -> set[str]` — `path LIKE prefix%`, for the orphan sweep.

### `status(self)` — `SELECT * FROM camera_status ORDER BY id` (the view from migration 0004).
### `conditions(self) -> dict[camera_id, list[row]]` — all condition rows grouped by camera.
### `timeline(self, camera_id, start, end, segment_seconds)`
`span && tstzrange(start, end)` is the question; `lower(span) >= start - segment_seconds AND lower(span) < end` is what lets Postgres prune to one partition, widened by one segment length because that is the most a segment can extend past its own start. Returns `start`, `end`, `path`, `bytes`, `epoch` in time order.
### `events(self, kind, camera_id, limit)` — filtered by optional kind and camera, newest first; payload decoded from JSON.
### `operator(self, username)` — `id, pwhash` or `None`.
### `create_operator(self, username, pwhash) -> id`.

### `cameras(self)` — the console's list: every column except `cred_secret`.
### `create_camera(self, fields, encrypt) -> id`
Filters to `OPERATOR_COLUMNS`, pops `cred_secret`, inserts the rest and, in the same transaction, encrypts the secret with the new id as associated data and updates the row. The two-step is forced by the AAD: the ciphertext needs the id the insert produces. `encrypt(secret, cid)` is injected so the store never sees the key.
### `update_camera(self, camera_id, fields, encrypt) -> bool`
Filters to operator columns, encrypts `cred_secret` if present (empty string → NULL), builds `UPDATE cameras SET col=$n …` dynamically, returns whether one row was updated (`"UPDATE 1"`). An empty field set is a no-op `True`. Any change here bumps `revision` through the trigger and wakes the Worker through NOTIFY.
### `delete_camera(self, camera_id) -> bool` — `DELETE FROM cameras`; the reconciler's stop loop notices the row is gone.
### `upsert_site(self, site_id, name)` — `INSERT … ON CONFLICT (id) DO UPDATE SET name`, so a camera can name a site that does not exist yet.

## Notes
- `indexed_paths_under` uses `LIKE prefix || '%'` without escaping; an `ARCHIVE_DIR` containing `_` or `%` would match more than intended (`_` is a single-character wildcard). `/data/archive` is safe; the test archive path uses hyphens.
- `f'SELECT path FROM "{part}"'` and the DETACH/DROP statements interpolate identifiers; they are only ever fed names read back from `pg_class`.
