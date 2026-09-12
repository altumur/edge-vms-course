-- 0002 — Lesson 5, Steps 4–6: the archive index and events.
-- Both are rolling windows, partitioned by month from day one, because
-- DELETE is not a retention strategy (276,768 rows in 231 ms freed zero disk;
-- DETACH + DROP TABLE freed 38 MB in 5 ms).

CREATE TABLE IF NOT EXISTS segments (
    camera_id  bigint      NOT NULL,
    span       tstzrange   NOT NULL,
    path       text        NOT NULL,
    bytes      bigint      NOT NULL,
    epoch      int         NOT NULL DEFAULT 1,
    CONSTRAINT segments_span_nonempty CHECK (NOT isempty(span) AND lower_inc(span))
) PARTITION BY RANGE (lower(span));

-- One GiST index on the parent creates one per partition, present and future.
CREATE INDEX IF NOT EXISTS segments_span_gist ON segments USING gist (span);
CREATE INDEX IF NOT EXISTS segments_camera_lower ON segments (camera_id, lower(span));

CREATE TABLE IF NOT EXISTS events (
    id         bigserial,
    at         timestamptz NOT NULL DEFAULT now(),
    camera_id  bigint,
    kind       text  NOT NULL,
    payload    jsonb NOT NULL DEFAULT '{}'
) PARTITION BY RANGE (at);

CREATE INDEX IF NOT EXISTS events_payload_gin ON events USING gin (payload);
CREATE INDEX IF NOT EXISTS events_kind_at     ON events (kind, at);

-- Partitions are created AHEAD of time by the retention job
-- (apphost/retention.py: ensure_partitions). The migration only guarantees
-- the current month exists so a fresh recorder can record before the job's
-- first pass. A missing partition is a recording outage, not an error.
CREATE OR REPLACE FUNCTION ensure_month_partition(parent regclass, month date)
RETURNS text AS $$
DECLARE
    name text := parent::text || '_' || to_char(month, 'YYYY_MM');
    lo   date := date_trunc('month', month)::date;
    hi   date := (date_trunc('month', month) + interval '1 month')::date;
BEGIN
    IF to_regclass(name) IS NULL THEN
        EXECUTE format('CREATE TABLE %I PARTITION OF %s FOR VALUES FROM (%L) TO (%L)',
                       name, parent, lo, hi);
    END IF;
    RETURN name;
END $$ LANGUAGE plpgsql;

SELECT ensure_month_partition('segments', current_date);
SELECT ensure_month_partition('events',   current_date);
