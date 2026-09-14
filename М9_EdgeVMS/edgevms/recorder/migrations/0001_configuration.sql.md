# 0001_configuration.sql — sites and cameras, the operator/controller column split, revision bump and NOTIFY triggers

**Role.** Lesson 5, Steps 1–3 (and Lesson 6, Step 3 for the notify trigger). The first migration `PgStore.migrate()` applies, in one transaction, recorded in `schema_migrations`. The recorder owns this database: nothing above it writes these rows except through the operator-owned columns. Expand-only: nothing here is renamed or dropped in the release that starts using it (Step 8). Every statement is `IF NOT EXISTS` / `OR REPLACE`, so re-running is safe (the README ran all four twice).

## Statement by statement
### `CREATE TABLE sites`
- `id text PRIMARY KEY` — a site identifier chosen by the operator (`"hq"` in the README).
- `name text NOT NULL` — display name; `store.upsert_site()` sets it equal to the id when the console or `provision.py` creates a site implicitly.

### `CREATE TABLE cameras`
Operator-owned columns (the only ones `store.create_camera()`/`update_camera()` accept, listed in `OPERATOR_COLUMNS`):
- `id bigserial PRIMARY KEY` — the camera id used in segment paths, `segments.camera_id`, `camera_conditions`, and as AES-GCM associated data for `cred_secret`.
- `site_id text REFERENCES sites(id)` — nullable; the console upserts the site before inserting.
- `name text NOT NULL`.
- `rtsp_url text NOT NULL` — `scheme://host/path`, NO credentials; enforced at the console (`_credential_free`) and in `provision.py`, not by the database.
- `cred_username text`, `cred_secret bytea` — the credential that was hiding in `rtsp_url`; `cred_secret` is nonce + AES-GCM ciphertext (`secrets.py`), never selected into a log (`DESIRED_SQL` fetches it only for the pipeline; `store.cameras()` for the console omits it).
- `enabled boolean NOT NULL DEFAULT true` — a disabled camera is desired-stopped; the reconciler filters on it.
- `retention_days int NOT NULL DEFAULT 30` — the per-camera window `retention.py` enforces.
- `priority int NOT NULL DEFAULT 100` — for the `by_priority` disk-full policy: lowest priority is sacrificed first.
Controller-owned from here down — no API may write these; only `store.report()`:
- `revision bigint NOT NULL DEFAULT 1` — an integer, not a hash and not a timestamp; the version of the desired state.
- `observed_revision bigint NOT NULL DEFAULT 0` — the revision the Worker last applied; `observed_revision >= revision` is "applied" (`reconciler.py`'s `>=`).
- `phase text NOT NULL DEFAULT 'pending'` — a POSITION (`pending`, `starting`, `running`, `failed`) written by `Worker.phases()`; reasons live in `camera_conditions`.
- `last_seen timestamptz` — set to `now()` by every `report()`; the console's `node_reporting` is "within 3 × REPORT_INTERVAL".

### `FUNCTION bump_revision()` and `TRIGGER cameras_bump`
`BEFORE UPDATE … FOR EACH ROW WHEN (<operator-owned columns> IS DISTINCT FROM …)` sets `NEW.revision := OLD.revision + 1`. The `WHEN` clause names the eight operator-owned columns explicitly instead of comparing whole rows — the README's "one correction to Lesson 5": whole-row comparison made the Worker's own `report()` (writing `observed_revision`, `phase`, `last_seen`) bump `revision`, so the lag was 1 forever and the recorder chased its own tail. This trigger is the column split from Step 3 enforced by the database rather than by convention.

### `FUNCTION notify_cameras()` and `TRIGGER cameras_notify`
`AFTER INSERT OR UPDATE OR DELETE … FOR EACH ROW` calls `pg_notify('cameras', <id>)`. `store.listen("cameras", …)` sets the Worker's `wake` event on it. Lesson 6, Step 3: NOTIFY is latency, never correctness — the Worker polls every `POLL_INTERVAL` regardless, because a notification missed while disconnected is gone forever. `RETURN NULL` is correct for an AFTER trigger; the `COALESCE(NEW.id, OLD.id)` covers DELETE.

## Notes
- The bump trigger fires on `report()`'s own UPDATE too, but its `WHEN` evaluates false, so `revision` is untouched — and `last_seen` still moves. That is what makes `revision - observed_revision` a meaningful lag in the `camera_status` view.
- There is no `updated_at`; the revision integer is the only change marker, on purpose (Step 3: "revision is an integer").
