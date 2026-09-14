# grants.py — Lesson 4: cluster-local grants with an expiry (`ClusterGrants`), their Variable shape, the stated revocation window, and `ClusterAuthoriser` — signature, then its own table, never a network call

**Role in the module.** Lesson 4, "grants are cluster-local, carry an expiry, and expiry IS the revocation mechanism". Each cluster holds `subject X may do Y on camera Z until T` in its own Variables under `domain/grants`, written by the domain agent (the only writer of `domain/*` in that cluster — `deploy/agent-policy.hcl`) and read by the cluster's console and live gateway. Enforcement is a local read — no lookup, no token exchange — which is the only way authorisation survives the domain being down. A cluster whose agent cannot reach the domain lets its grants lapse; that converts an unbounded revocation window into a number the product states. Workers never see a user, a grant or a token: the console and the gateway are the cluster's clients of them, and those two enforce. Two lifetimes, not independent: the revocation window is the SHORTER of the token lifetime and the grant lifetime. Depends on `tokens.verify`/`KeySet`; `agent.DomainPublisher.publish_grants` and `ClusterTrust.grants()` use the two item converters; `gateway.WorkerLiveEndpoint.authorise` is the slot `ClusterAuthoriser.authorise` fills.

## Module-level names
- `GRANT_LIFETIME = 24 * 3600.0` — one day: "Lesson 4 makes students pick and defend it; the product states it". The tests grant `now + GRANT_LIFETIME`.

## `class Grant` (frozen dataclass)
- `subject: str`; `capability: str` (`view` | `edit` | `admin`); `camera: int | None` (`None` = every camera in this cluster); `valid_until: float`.

## `class ClusterGrants`
"One cluster's grants, as its console and gateway hold them in memory from `domain/grants/*` (М9's `grants` table, with `valid_until`, one level up)."
### `__init__(self, cluster, now=time.time)` — `grants: {(subject, capability, camera): valid_until}`.
### `grant(self, subject, capability, camera, valid_until)` — add or replace one entry.
### `revoke(self, subject)` — drop every entry for a subject (the local, immediate revoke — only useful when the cluster can be reached).
### `may(self, subject, capability, camera, now=None) -> bool`
True if any entry matches the subject, has the capability or `admin`, names the camera or `None`, and is still valid at `now`.
### `renew_from_domain(self, renewals)`
"The agent carried the domain's current grants for this cluster: replace, so a grant the domain dropped is not renewed." Wholesale replacement, never a merge — the test's last lines: renewing with only `view` leaves `edit` on camera 7 gone.
### `access_ends(self, subject, token_exp) -> float`
State in advance: if a revoke cannot reach this cluster, when does the subject lose access? `min(token_exp, max(valid_until over the subject's grants))`, or `token_exp` if it has no grants. The test states the number, then advances the clock and measures it in both directions (token outlives grant, grant outlives token).

## Functions
### `grants_to_items(grants) -> dict`
The Variable `domain/grants/<cluster>`: one item per grant, key `subject|capability|camera` (empty camera for `None`), value the expiry as a string.
### `grants_from_items(items) -> list[Grant]` — the inverse; tolerates `None`.
### `revocation_window(token_lifetime, grant_lifetime) -> float`
`min` of the two. The docstring: "Most people answer the token." With the module's numbers, 900 s.

## `class ClusterAuthoriser`
"What a cluster's console and live gateway run: signature against the key set the cluster holds (in its Variables, via the domain agent), the revocation list it holds, then its grants. Never a network call, never a worker."
### `__init__(self, grants, keyset, revoked=frozenset(), now=time.time)`.
### `update_trust(self, keyset, revoked)` — swap in what the agent last synced.
### `subject(self, token) -> str` — `verify(...)["sub"]` at `self.now()`.
### `authorise(self, token, capability, camera) -> str`
A `TokenError` becomes `PermissionError("token refused: …")`; no matching grant → `PermissionError("alice has no edit grant on camera 12 in south")`; otherwise the subject. `test_grants_are_cluster_local…`: `view` on any camera and `edit` on 7 pass, `edit` on 12 fails; after `TOKEN_LIFETIME + 61` the token is refused as expired; after a further `GRANT_LIFETIME` a fresh token is refused for lack of a live grant.

## Notes
- The item key splits on `|`; a subject containing `|` would break `grants_from_items`. Subjects here are login ids.
- `gateway.py` defines its own `Forbidden` for the same refusal; the two are not related classes.
- In production the grants a cluster reads live at `domain/grants` (the agent copies `domain/grants/<cluster>` from the domain cluster to that exact path in its own cluster — see `agent.py.md`).
