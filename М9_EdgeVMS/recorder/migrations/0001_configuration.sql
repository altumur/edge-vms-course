-- 0001 — Lesson 5, Steps 1–3: configuration. The recorder owns this database.
-- Expand-only. Nothing here is ever renamed or dropped in the release that
-- starts using it (Lesson 5, Step 8).

CREATE TABLE IF NOT EXISTS sites (
    id    text PRIMARY KEY,
    name  text NOT NULL
);

CREATE TABLE IF NOT EXISTS cameras (
    id              bigserial PRIMARY KEY,
    site_id         text REFERENCES sites(id),
    name            text    NOT NULL,
    rtsp_url        text    NOT NULL,          -- scheme://host/path, NO credentials
    cred_username   text,
    cred_secret     bytea,                     -- encrypted; never selected into a log
    enabled         boolean NOT NULL DEFAULT true,
    retention_days  int     NOT NULL DEFAULT 30,
    priority        int     NOT NULL DEFAULT 100,   -- for the by-priority disk-full policy

    -- controller-owned from here down. No API may write these.
    revision          bigint      NOT NULL DEFAULT 1,
    observed_revision bigint      NOT NULL DEFAULT 0,
    phase             text        NOT NULL DEFAULT 'pending',
    last_seen         timestamptz
);

-- revision is an integer, not a hash and not a timestamp. It bumps when an
-- OPERATOR-OWNED column changes. It must NOT bump when the AppHost writes
-- observed_revision / phase / last_seen back, or the recorder chases its own tail
-- and never converges. So the WHEN clause names the operator-owned columns
-- explicitly instead of comparing whole rows.
CREATE OR REPLACE FUNCTION bump_revision() RETURNS trigger AS $$
BEGIN
    NEW.revision := OLD.revision + 1;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER cameras_bump BEFORE UPDATE ON cameras
    FOR EACH ROW WHEN (
        OLD.site_id        IS DISTINCT FROM NEW.site_id        OR
        OLD.name           IS DISTINCT FROM NEW.name           OR
        OLD.rtsp_url       IS DISTINCT FROM NEW.rtsp_url       OR
        OLD.cred_username  IS DISTINCT FROM NEW.cred_username  OR
        OLD.cred_secret    IS DISTINCT FROM NEW.cred_secret    OR
        OLD.enabled        IS DISTINCT FROM NEW.enabled        OR
        OLD.retention_days IS DISTINCT FROM NEW.retention_days OR
        OLD.priority       IS DISTINCT FROM NEW.priority
    )
    EXECUTE FUNCTION bump_revision();

-- Lesson 6, Step 3: NOTIFY is latency, never correctness. The AppHost polls.
CREATE OR REPLACE FUNCTION notify_cameras() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('cameras', COALESCE(NEW.id, OLD.id)::text);
    RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER cameras_notify AFTER INSERT OR UPDATE OR DELETE ON cameras
    FOR EACH ROW EXECUTE FUNCTION notify_cameras();
