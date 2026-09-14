# 0003_operators_and_grants.sql — the operator account table and an unused grants table, created before they are needed

**Role.** Lesson 5, Step 7. `operators` backs the console's `/login` (`store.operator()`, `store.create_operator()` from `tools/provision.py operator`). `grants` exists and nothing consults it: one operator, all capabilities, no policy. The point of creating it now is stated in the header — adding it later is a migration against live authorization data on every appliance in the field. This is the course's fourth temporary secret; М12 Lesson 4 replaces the password with a domain-signed token.

## Statement by statement
### `CREATE TABLE operators`
- `id bigserial PRIMARY KEY` — the operator id a session token maps to (`Sessions.issue(row["id"])`).
- `username text NOT NULL UNIQUE` — the login name; `/login` looks it up and returns the same 401 whether it is unknown or the password is wrong.
- `pwhash text NOT NULL` — an argon2 hash from `console/auth.py::hash_password`; never a password.

### `CREATE TABLE grants`
- `id bigserial PRIMARY KEY`.
- `subject text NOT NULL` — who the grant is for (an operator name or a role, unspecified here).
- `capability text NOT NULL` — what it allows.
- `valid_until timestamptz` — "unused here. See Lesson 5, Step 7": an expiry that exists so the schema does not need to change when authorization becomes real.

## Notes
- No code path inserts into `grants` and no query reads it; the migration is pure expansion.
