# auth.py — argon2 password hashing and in-memory bearer sessions, marked temporary

**Role in the module.** Lesson 9, Step 3 — a login, marked temporary. One account, provisioned by hand at commissioning (`tools/provision.py operator`), all capabilities; the `grants` table exists and nothing consults it. This is the course's fourth temporary secret; М12 Lesson 4 replaces it with a token from the domain signer and removes the password hash from the recorder entirely. Used by `console/app.py` (`verify_password`, `Sessions`) and `provision.py` (`hash_password`). Depends on `argon2-cffi`.

## Module-level names
- `_ph` — one `argon2.PasswordHasher()` with library defaults (argon2id).

## Functions
### `hash_password(password) -> str` — the argon2 encoded hash stored in `operators.pwhash`.
### `verify_password(pwhash, password) -> bool` — `PasswordHasher.verify`, returning `False` on `VerifyMismatchError` or `VerificationError` (a malformed hash) instead of raising, so `/login` can give one 401 for every failure.

## `class Sessions`
In-memory bearer tokens: `token -> (operator_id, expiry)` with a TTL. Lost on restart, which is fine: an operator logs in again, and there is exactly one surface to protect. Sessions are in memory (README "Known gaps").

### `__init__(self, ttl)` — the TTL in seconds (`SESSION_TTL`, default 12 h) and an empty dict.
### `issue(self, operator_id) -> str` — `secrets.token_urlsafe(32)` mapped to the operator with `monotonic() + ttl`.
### `operator_for(self, token) -> int | None` — `None` for a missing or unknown token; an expired token is deleted on lookup and returns `None`; otherwise the operator id.

## Notes
- Expired tokens are only purged when presented; a long-running console accumulates unused ones until restart. Bounded by the login rate of one operator.
