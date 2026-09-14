# 0004_conditions_and_status.sql — reasons on their own axis (`camera_conditions`) and the one query (`camera_status`)

**Role.** Lesson 9. `phase` in `cameras` is a position; this migration adds the table of reasons that explain a position without changing it, and the view that answers "is camera 7 recording, and how far behind is it?" in one query. `store.set_condition()` is the only writer of conditions; `store.status()` and `store.conditions()` are what `console/app.py::build_status` reads; `/metrics` derives every `recorder_*` series from the view.

## Statement by statement
### `CREATE TABLE camera_conditions`
- `camera_id bigint NOT NULL` — no FK; `store.clear_conditions()` exists for cleanup but the Worker does not call it.
- `condition text NOT NULL` — `camera_reachable`, `storage_available`, `licensed` (`Worker.report_once()`); М14 wires `licensed` for real.
- `status boolean NOT NULL` — true = fine.
- `reason text` — the human sentence when false: `"failing, retry in 8s"`, `"disk full; policy=stop_recording"`.
- `since timestamptz NOT NULL DEFAULT now()` — when the status last flipped. `set_condition()`'s `ON CONFLICT … DO UPDATE` keeps `since` when the status is unchanged and resets it to `now()` when it flips — "storage unavailable since 14:02" is the sentence that turns a ticket into a fix (verified in the README's Postgres run).
- `PRIMARY KEY (camera_id, condition)` — one row per axis per camera; the target of the upsert.

### `CREATE OR REPLACE VIEW camera_status`
Per camera: `id`, `name`, `site_id`, `enabled`, `revision`, `observed_revision`, `lag = revision - observed_revision`, `phase`, `last_seen`, and from a `LEFT JOIN LATERAL` over `segments`: `last_segment_end = upper(span)` of the newest segment and `silent_for = now() - last_segment_end`.
- The lateral subquery filters `camera_id = c.id` and `lower(span) > now() - interval '2 hours'`, `ORDER BY lower(span) DESC LIMIT 1`. The `lower(span)` clause is the partition-pruning predicate: "this clause looks redundant. It is the difference between one index scan and sixty. Do not remove it."
- Consequence: a camera whose newest segment started more than two hours ago has `last_segment_end` and `silent_for` NULL. The console reports `silent_for_seconds: null`, counts it in `recorder_cameras_never_recorded` (HELP: "no segment in the last two hours"), and excludes it from `recorder_camera_silent_seconds_max`.

## Notes
- `lag` can be negative only transiently (a row reported at revision N while an operator bumps it); `build_status` treats `lag <= 0` as converged.
- The view is `OR REPLACE`, so a later migration can extend its column list but not remove columns without dropping it first — the same expand-only rule as the tables.
