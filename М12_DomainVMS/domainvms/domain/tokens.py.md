# tokens.py — Lesson 4: Ed25519 tokens that name a subject and nothing else, a key SET for rotation overlap, offline `verify`, and a self-pruning revocation list

**Role in the module.** Lesson 4. The docstring's flow: Alice → signer: a short-lived signed token (`sub`, `exp`, `jti`); the cluster's console verifies the signature with the public key, OFFLINE, checks expiry, checks the revocation list it holds, then looks up ITS OWN grants for "alice". The token names the subject and nothing else — rights are not in it (М9's Authorization row), because a token that carried rights would be a lookup that expired with the domain. The format is the JWS shape — `base64url(header).base64url(payload).base64url(signature)` — with one algorithm (`EdDSA`) and no library, so a console verifies it with forty lines and a public key. Keys are a SET (`kid` → public key) so rotation overlaps: a token signed by the previous key verifies until that key's retirement time passes. `TokenIssuer` is half of `signer.Signer`; `KeySet` and `RevocationList` are what `agent.DomainPublisher` writes and `agent.ClusterTrust` reads back; `verify` is what `grants.ClusterAuthoriser`, `console.main` and the tests call. Needs `cryptography`.

## Exceptions
- `TokenError` — base. `Expired`, `Revoked`, `UnknownKey` (kid not in the set, or retired), `BadSignature` (also "not a token" and "issued in the future") — each a distinct answer the console can log.

## Functions (helpers)
### `_b64(b) -> str` / `_unb64(s) -> bytes` — base64url without padding, and its inverse (re-pads).

## `class KeySet` (dataclass)
What every cluster and agent holds.
- `current: str` — the kid the signer signs with now.
- `keys: dict[str, bytes]` — kid → raw 32-byte Ed25519 public key.
- `retire_at: dict[str, float]` — kid → wall time after which it is no longer accepted; a missing entry means current/never.
### `to_items(self) -> dict` — the Variable shape: `current`, `key:<kid>` (hex), `retire:<kid>` (string float) — flat string items, as Nomad Variables require.
### `from_items(cls, items) -> KeySet` — the inverse; used by `ClusterTrust.keyset()`.
### `usable(self, kid, now) -> bool` — in the set and not yet retired.

## `class TokenIssuer`
"Half of the domain signer. Holds the private key; issues short tokens."

### `__init__(self, domain, private_key=None, kid=None)`
`domain` becomes the `iss` claim; a fresh Ed25519 key and a random 8-hex `kid` unless the signer passes the persisted ones. `previous` is the list of `(kid, public, retire_at)` still in the set after rotations.

### `public_bytes` (property) — the raw public key.
### `keyset(self) -> KeySet` — the current key plus every un-retired previous key with its retirement time: what `DomainPublisher.publish_keys` writes.
### `issue(self, subject, lifetime, now=None, **claims) -> str`
Header `{"alg": "EdDSA", "kid"}`; payload `{iss, sub, iat: now, exp: now + lifetime, jti: 16 hex}` plus any extra claims (break-glass adds `via`/`who`); both JSON with sorted keys; signature over `header.payload`. `identity.TOKEN_LIFETIME` (900 s) is the lifetime the product states.

### `rotate(self, overlap, now=None) -> str`
Pushes the current `(kid, public, now + overlap)` onto `previous`, generates a new key and kid, prunes previous entries already past retirement, returns the new kid. The old key's tokens keep verifying for `overlap` seconds (`test_revocation_travels_by_the_agent_and_rotation_overlaps`: 600 s overlap; after 700 s the old token is `UnknownKey`).

## Functions

### `verify(token, keys, revoked=frozenset(), now=None, skew=60.0) -> dict`
Offline; returns the payload. Steps, in order: split into three parts and decode header/payload (anything malformed → `BadSignature("not a token")`); `kid` must be `usable` at `now` → else `UnknownKey`; Ed25519 verify over `header.payload` → else `BadSignature`; `now > exp + skew` → `Expired`; `now < iat − skew` → `BadSignature("issued …s in the future — clock skew")`; `jti in revoked` → `Revoked`. The 60 s skew on both sides is why the tests advance `TOKEN_LIFETIME + 61`.

## `class RevocationList`
"Small, rare, consistent: raft's shape. Entries carry the token's own expiry so the list prunes itself — revocation is a lifetime problem."
### `__init__(self)` — `entries: {jti: exp}`.
### `revoke(self, payload)` — records the token's `jti` with its `exp` (the caller `verify`s the token first to get the payload — `signer_service` `/revoke`).
### `prune(self, now)` — drops entries whose token has expired anyway.
### `to_items(self) -> dict` — one item `jtis = "j1:e1,j2:e2,…"` sorted, so the whole list is one Variable write.
### `from_items(cls, items) -> RevocationList` — the inverse; tolerates `None`.
### `jtis` (property) — the set `verify` takes as `revoked`.

## Notes
- Items are strings on the way through Variables; `from_items` casts `retire:`/exp values back with `float()`.
- Nothing in the token says what Alice may do: `test_login_ends_in_a_token_naming_the_subject_and_nothing_else` asserts `"roles" not in payload and "grants" not in payload`.
