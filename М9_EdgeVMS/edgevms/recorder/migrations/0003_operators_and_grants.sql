-- 0003 — Lesson 5, Step 7: operators and grants, before they are needed.
-- One operator, all capabilities, no policy. valid_until does nothing in М9
-- and is here because adding it later is a migration against live
-- authorization data on every appliance in the field.

CREATE TABLE IF NOT EXISTS operators (
    id       bigserial PRIMARY KEY,
    username text NOT NULL UNIQUE,
    pwhash   text NOT NULL
);

CREATE TABLE IF NOT EXISTS grants (
    id          bigserial PRIMARY KEY,
    subject     text NOT NULL,
    capability  text NOT NULL,
    valid_until timestamptz          -- unused here. See Lesson 5, Step 7.
);
