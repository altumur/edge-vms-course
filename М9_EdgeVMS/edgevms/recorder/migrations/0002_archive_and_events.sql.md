# 0002_archive_and_events.sql — the segment index and the event log, partitioned by month from day one

**Role.** Lesson 5, Steps 4–6. Both tables are rolling windows: `segments` is the index that turns the spool into an archive (one row per closed file, written by `store.index_segment()` from `Worker.report_once()`), `events` is what the recorder says about itself (`retention.degraded`, `retention.stopped`, `archive.orphans`). Both are `PARTITION BY RANGE` on time because DELETE is not a retention strategy: the header quotes 276,768 rows deleted in 231 ms freeing zero disk versus `DETACH + DROP TABLE` freeing 38 MB in 5 ms. `retention.py` drops whole months; this migration only guarantees the current month exists.

## Statement by statement
### `CREATE TABLE segments … PARTITION BY RANGE (lower(span))`
- `camera_id bigint NOT NULL` — no foreign key to `cameras`: a deleted camera's footage stays until retention takes it, and the row must survive `DELETE FROM cameras`.
- `span tstzrange NOT NULL` — `[start, end)` of the segment, from `_format_location` time to the bus-message time in `pipeline._closed()`. `&&` against a requested range is the timeline query.
- `path text NOT NULL` — the file under `ARCHIVE_DIR`; `retention.py` remembers paths before dropping a partition and unlinks after. `store.delete_segment()` deletes by path, so path is assumed unique though no constraint says so.
- `bytes bigint NOT NULL` — file size at close; summed by the disk-full policy as "freed".
- `epoch int NOT NULL DEFAULT 1` — the `EPOCH` setting; the fencing token that М11 makes meaningful.
- `CONSTRAINT segments_span_nonempty CHECK (NOT isempty(span) AND lower_inc(span))` — no zero-length or open-start spans; the index never says a segment spans nothing.
- The partition key is `lower(span)`, an expression: every query that wants pruning must carry a `lower(span)` predicate, which is the "pruning trap" the README maps to `store.timeline()` and the `camera_status` view.

### Indexes on `segments`
- `segments_span_gist USING gist (span)` — for `&&` overlap queries; one index on the parent creates one per partition, present and future.
- `segments_camera_lower (camera_id, lower(span))` — the per-camera time order used by the view's `ORDER BY lower(span) DESC LIMIT 1`, `expire_rows`, and `oldest_segments`.

### `CREATE TABLE events … PARTITION BY RANGE (at)`
- `id bigserial` — not a primary key: a partitioned table's PK would have to include `at`; the id is only a sort/reference value in `store.events()`.
- `at timestamptz NOT NULL DEFAULT now()` — partition key and the `ORDER BY at DESC` of the console.
- `camera_id bigint` — nullable; retention events are recorder-wide.
- `kind text NOT NULL` — `retention.degraded`, `retention.stopped`, `archive.orphans`; filterable in `/events?kind=`.
- `payload jsonb NOT NULL DEFAULT '{}'` — free-form detail (`policy`, `bytes_freed`, per-camera new oldest, orphan count).

### Indexes on `events`
- `events_payload_gin USING gin (payload)` — containment queries on the payload.
- `events_kind_at (kind, at)` — the console's `WHERE kind = $1 ORDER BY at DESC`.

### `FUNCTION ensure_month_partition(parent regclass, month date) RETURNS text`
Builds the name `<parent>_YYYY_MM`, the month bounds `[first day, first day of next month)`, and `CREATE TABLE … PARTITION OF … FOR VALUES FROM … TO …` if `to_regclass(name)` is NULL. Returns the name. `store.ensure_partition()` calls it; `retention.ensure_partitions()` keeps `PARTITIONS_AHEAD` months ahead for both parents. A missing partition is a recording outage ("no partition of relation segments found for row"), not an error — hence ahead of time.

### `SELECT ensure_month_partition('segments', current_date); … ('events', current_date)`
The current month for both, so a fresh recorder can record before the retention job's first pass.

## Notes
- `store.partitions()` reads the bounds back by parsing `pg_get_expr(relpartbound)` with a regex; the naming `parent_YYYY_MM` here and in `retention._months()` must agree, and they do.
