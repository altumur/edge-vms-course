# identity.py — Lesson 4: users as Variables under `identity/*` with the signer the only writer, IdP subjects with no secret, the identity set published object-first, prefs as objects, and break-glass

**Role in the module.** Lesson 4, "where users live, and creating a user touches no cluster". The docstring's table: `identity/users/<id>` in the domain cluster's Variables, the signer the only writer (`deploy/signer-policy.hcl` grants `identity/*` to the signer job) — a local user is a scrypt hash, a federated one an IdP subject and no secret; `identity/pointer` → `identity/rev-N`, the whole set as one object, published object-first (the pointer moves only after the object exists — М11's publish-then-point with different nouns); `users/<id>/prefs`, per-user UI configuration as an object, last write wins with a revision. Nothing about a user ever reaches a worker: authentication ends in a token naming the subject (`tokens.py`), and what the subject may do is each cluster's grants (`grants.py`), carried by the agent. The RPO for users is the publication interval, and it is stated (`publish_floor`). Break-glass is the honest residue: one local account, audited on every use, alarmed on, rotated after — it reintroduces exactly the password hash the design removed, "and the module says so out loud". Used by `signer_service.main` (login, publish) and the Lesson 4 tests.

## Module-level names
- `TOKEN_LIFETIME = 15 * 60.0` — 900 s: "the number the product states; Lesson 4 makes students defend it". Every `login` issues with it; `grants.revocation_window` takes it as one of its two bounds.

## `class AuthError(Exception)` — bad credentials, unknown IdP subject, bad break-glass password.

## Functions (helpers)
### `_hash(password, salt=None) -> str`
scrypt (n=2¹⁴, r=8, p=1, 32 bytes) over a random 16-byte salt; stored as `<salt hex>:<hash hex>`.
### `_check(password, stored) -> bool`
Re-derives with the stored salt and compares in constant time.

## `class User` (dataclass)
- `id: str` — the login name and the token's `sub`.
- `kind: str` — `local` | `idp` (and `deleted` as a tombstone written by `delete`).
- `roles: list[str]` — the domain's roles; they never enter a token.
- `pwhash: str = ""` — local only.
- `idp_subject: str = ""` — idp only.
- `created: float = 0.0`.
### `to_items(self) -> dict` — flat string items for a Variable (roles comma-joined).
### `from_items(cls, it) -> User` — the inverse, tolerant of missing keys.

## `class IdentityStore`

### `__init__(self, signer, vars_, objects, publish_floor=60.0, now=time.time)`
`signer` issues tokens (`signer.tokens`); `vars_` is the domain cluster's Variables (records and the pointer); `objects` the object store (the published set and prefs); `publish_floor` is the minimum seconds between publishes — the stated RPO; `_dirty` marks unpublished changes; `published_rev`, `publishes` are counters.

### `_path(self, uid)` — `identity/users/<uid>`.
### `get(self, uid) -> User | None` — reads the record (a tombstone comes back as a `User` of kind `deleted`).
### `_put(self, u)` — read the index, `put` with CAS, mark dirty.
### `create_local(self, uid, password, roles) -> User` — refuses an existing id (`ValueError`), hashes, stores.
### `create_federated(self, uid, idp_subject, roles) -> User`
"The customer has an IdP: the record holds a subject, not a person, and no secret." Stores without an existence check.
### `set_roles(self, uid, roles)` — `KeyError` if unknown; rewrites the record.
### `delete(self, uid)` — writes the tombstone `{id, kind: "deleted"}` by CAS (not a Variable delete, so the id stays reserved and the publish notices the change).
### `users(self) -> list[User]` — every record under `identity/users/` whose kind is `local` or `idp`.

### `login(self, uid, password) -> str`
Local users only; wrong id, kind or password → `AuthError("bad credentials")`. Success is `signer.tokens.issue(uid, TOKEN_LIFETIME, now=…)` — the token names the subject and nothing else.
### `login_federated(self, idp_assertion) -> str`
The IdP authenticated Alice (the assertion's `sub`); the store finds the `idp` user with that subject and the signer issues a *domain* token naming her domain id — "the clusters never learn the IdP exists". No match → `AuthError`. Linear scan over `users()`.

### `publish(self, force=False) -> bool`
Nothing dirty and not forced → `False`; dirty but inside the floor since the last publish → `False`. Otherwise: revision `published_rev + 1`; write the object `identity/rev-<n>` = `{"format": 1, "revision", "users": [to_items…]}` (step 1), then move `identity/pointer` = `{object, revision}` by CAS (step 2); update the counters. `test_identity_publishes_object_first_then_pointer_and_restores_elsewhere` asserts the pointer names `identity/rev-1`, that the object exists, and that a role change publishes `rev-2`.

### `restore(cls, signer, new_vars, objects, pointer_items, now=time.time) -> IdentityStore`
Re-hosting the domain: after `Signer.restore` put the backed-up key in a new cluster's Variables, load the identity object the pointer names from an object store that has a copy (the test copies `identity/rev-2` into south's store), refuse if it is missing ("refusing to guess"), write every user record into the new Variables, set `published_rev` from the object, and write the pointer there too. The test then logs alice in on the restored store and verifies with the restored signer's key set.

### `get_prefs(self, uid) -> (dict, int)` — the object `users/<uid>/prefs` as `(prefs, revision)`, `({}, 0)` if none.
### `put_prefs(self, uid, prefs, base_revision) -> int`
Optimistic concurrency on an object: if `base_revision` is not the current revision → `Conflict("prefs revision 2, you had 1")` so "a second tab is TOLD rather than silently overwritten"; otherwise write `{revision: n+1, prefs}` and return `n+1`. `test_prefs_are_objects_and_a_stale_tab_is_told` also asserts `vars.list("users/") == []` — nothing of this in raft.

## `class BreakGlass` (dataclass)
"One account. Audited on every use, alarmed on, rotated after."
- `signer: Signer`, `pwhash: str`, `audit: list[dict]`, `alarm: list[str]`, `used_since_rotation: int`.
### `create(cls, signer, password)` — hashes the password.
### `use(self, password, who, why, now) -> str`
Appends an audit entry `{at, who, why, ok}` and an alarm line (`BREAK-GLASS used by carol: …` or `… ATTEMPTED …`) *before* deciding; a bad password → `AuthError`; success increments `used_since_rotation` and issues a token for subject `break-glass` with extra claims `via="break-glass"`, `who=<who>` — so the audit trail names the person even though the subject is the account.
### `rotate(self, new_password)` — new hash, counter to 0.

## Notes
- `login` takes the clock from `self.now`, so a token issued in a test carries the fake time — the Lesson 4 tests depend on it.
- `deploy/signer.nomad.hcl` sets `TOKEN_LIFETIME=900` in the environment, but the constant here is the only lifetime used; nothing reads that variable.
- No process exposes `create_local`/`create_federated`/`set_roles` over HTTP (`signer_service` has only `/login`, `/revoke`, `/keys`); creating a user in production is not wired.
