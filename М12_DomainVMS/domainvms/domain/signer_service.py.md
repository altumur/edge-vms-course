# signer_service.py — the domain signer as a process: keys from `domain/signer`, the key set and revocation list published for agents, the identity set published on a floor, logins answered with tokens; `python3 -m domain.signer_service`

**Role in the module.** Wiring for `deploy/signer.nomad.hcl`. Holds the keys (`signer.Signer` over the domain cluster's `NomadVariables`), publishes the key set and the revocation list where agents read them (`agent.DomainPublisher`), publishes the identity set object-first on a floor (`identity.IdentityStore.publish`), and answers logins with tokens. The docstring says what is *not* here: the CA half (issue, renew, rotate) is driven by the registrar and by renewal requests over mTLS, which need the bench. Not exercised by the tests; everything it wires is.

## Functions

### `main()`
Environment: `DOMAIN_ID` (default `domain`; the token `iss` and the root CN prefix), `NOMAD_ADDR` (the domain cluster's local agent), `OBJECT_STORE_URL` (default `file:///data/domain`; `open_store`), `IDENTITY_PUBLISH_FLOOR` (default 60 s — the stated identity RPO), `SIGNER_HOST` (default `0.0.0.0`), `SIGNER_PORT` (default 8445). Builds `Signer`, `IdentityStore`, `DomainPublisher`, loads the existing `RevocationList` from `domain/revoked`, and publishes the key set once at start (so a fresh domain cluster has `domain/keys` before any agent asks).

#### `class H(BaseHTTPRequestHandler)`
- `_send(status, body)` — JSON reply.
- `do_GET`: `GET /keys` → `200` with `signer.tokens.keyset().to_items()` — the same items the agents copy, for a human or a bench to read; anything else 404.
- `do_POST` (body parsed as JSON first, `{}` if empty):
  - `POST /login` `{"user", "password"}` → `200 {"token": …}` via `ids.login`; an `AuthError` → `401 {"detail": "bad credentials"}`. Missing keys raise `KeyError`, which is not caught.
  - `POST /revoke` `{"token"}` → verifies the token against the current key set (so only a genuine, unexpired token can be revoked — and the verify yields the payload with `jti`/`exp`), adds it to the list, publishes `domain/revoked`, replies `{"revoked": true}`.
  - anything else 404.
- `log_message` silenced.

Then a `ThreadingHTTPServer` on a daemon thread, SIGTERM/SIGINT handlers, and the main loop every 5 s: `ids.publish()` (honours the floor and the dirty flag — "object first, then the pointer, on a floor") and `revoked.prune(time.time())`; exceptions swallowed. On stop, `srv.shutdown()`.

## Notes
- `prune` trims the in-memory list, but the pruned list is only republished on the next `/revoke`; the Variable can carry expired jtis until then (harmless — `verify` rejects an expired token before consulting the list).
- No route creates or edits users; `IdentityStore.create_local` and friends have no HTTP surface, so the identity set this loop publishes changes only if something else in the process calls them.
- `TOKEN_LIFETIME` from the jobspec's environment is not read; `identity.TOKEN_LIFETIME` is the constant used.
- Federated login (`login_federated`) and break-glass have no route here either.
