-- 0004 — Lesson 9: positions and reasons on separate axes, and one query
-- that answers "is camera 7 recording, and how far behind is it?"

CREATE TABLE IF NOT EXISTS camera_conditions (
    camera_id  bigint  NOT NULL,
    condition  text    NOT NULL,
    status     boolean NOT NULL,
    reason     text,
    since      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (camera_id, condition)
);

CREATE OR REPLACE VIEW camera_status AS
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
      -- Partition pruning needs a predicate on the partition key, lower(span).
      -- This clause looks redundant. It is the difference between one index
      -- scan and sixty. Do not remove it.
      AND  lower(span) > now() - interval '2 hours'
    ORDER  BY lower(span) DESC
    LIMIT  1
) s ON true;
