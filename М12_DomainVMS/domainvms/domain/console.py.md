# console.py — Lesson 3: the domain console over HTTP in the standard library — the read model, the causes, the directory, the proxied PUT; `python3 -m domain.console`

**Role in the module.** Lesson 3. The process that serves browsers, in the standard library (`ThreadingHTTPServer`, as М10/М11's consoles are). The docstring: one of the two cluster-level jobs (the other is the live gateway); a cluster runs it pointed at itself, and the domain cluster runs the same one pointed at every cluster (`deploy/console.nomad.hcl`). Stateless: kill it, start another, the first pass rebuilds everything. It composes three objects it does not own: `DomainDirectory` (`/api/where`), `ReadView` (`/api/cameras`, `/api/causes`, refreshed by a background thread) and `ConsoleAPI` (`PUT /api/cameras/<n>`). Tested end to end by `test_console_over_http`.

## `class Console`

### `__init__(self, directory, view, api, refresh_interval=5.0)`
The three collaborators and the refresh period; `_stop` is the event that ends the refresher.

### `_refresher(self)`
The loop on the `readview` thread: `view.refresh()` every `refresh_interval` seconds until stopped. Any exception is swallowed — the comment: a bad pass is a stale view, not a dead console (the rows keep their last age and the view keeps serving).

### `handler(self) -> type`
Builds and returns the request handler class closed over this console.

#### `class H(BaseHTTPRequestHandler)`
- `_send(status, body)` — JSON body with `Content-Type` and `Content-Length`.
- `_token()` — the bearer token from `Authorization: Bearer …`, else `None` (passed to the API, which decides whether one is required).
- `do_GET`:
  - `GET /healthz` → `200 {"ok": true, "passes": <view.passes>}` — the check `console.nomad.hcl` polls; `passes` says whether the refresher has run.
  - `GET /api/cameras?q=&page=&size=&cluster=` → `200` and `view.list(q, page, size, cluster)` (defaults `""`, 1, 50, `None`): rows with `as_of`, the `clusters` state map and `complete`.
  - `GET /api/causes` → `200` and a list of each `Cause`'s fields plus its `sentence`.
  - `GET /api/where/<camera>` → `directory.where(<last path segment>)` (a string ref); status `200` if found, else `404` when complete, `503` when a cluster was unreachable — the same rule as the API; body is the `Answer`'s fields plus `complete` and `sentence`.
  - anything else → `404 {"detail": "no such route"}`; any exception → `500 {"detail": str(e)}` (so a two-cluster claim on one ref is reported, not dropped).
- `do_PUT`:
  - only `/api/cameras/<camera>`; else 404.
  - `Idempotency-Key` header required, else `400` with the sentence "a retried PUT must be the same PUT".
  - reads `Content-Length` bytes of JSON as `fields` (empty body → `{}`), calls `api.update_camera(<camera>, fields, key, token)` → `200` with its response; an `ApiError` becomes its status and `{"detail": …}`. Other exceptions are not caught here.
- `log_message` — silenced.

### `serve(self, host="127.0.0.1", port=8090) -> ThreadingHTTPServer`
Starts the `readview` refresher thread and a `ThreadingHTTPServer` on a daemon thread named `console`; returns the server so the caller can read `server_address` (the test binds `port=0`) and stop it.

### `stop(self, srv)`
Sets `_stop`, `srv.shutdown()`, `srv.server_close()`.

## Functions

### `main()`
`python3 -m domain.console`. Builds the federation from `CLUSTERS`/`DOMAIN_CLUSTER` (`runtime.federation_from_env`), a `DomainDirectory`, a `ReadView` with `LOST_AFTER` (default 45), and a `ClusterTrust` over the domain cluster's Variables. The `verifier(token)` reads the key set from `domain/keys` (`503 "no signer key set in this cluster yet (is the domain agent running?)"` if absent) and returns `verify(token, keyset, revoked)["sub"]` — offline, from Variables the agent filled. `consoles(cluster)` is a stub that raises `ApiError(501, "forwarding to <cluster>'s console needs service discovery wired here (nomadService)")` — the forwarded write is a bench item, not code. `AUTH` (default `1`) turns the verifier on; `REFRESH_INTERVAL` (default 5), `CONSOLE_HOST` (default `0.0.0.0`), `CONSOLE_PORT` (default 8443, matching the jobspec's static port). Waits for SIGTERM/SIGINT, then `console.stop`.

## Notes
- The docstring's header line says "The cluster console"; the routes and `main()` are the domain's aggregate over `CLUSTERS`. Pointed at one cluster it is that cluster's read-only console; М11's own console (`cluster/console.py`) is a different program with the write routes.
- `verifier` uses `fed.domain_cluster.vars` — the *domain* cluster's `domain/keys` — whereas the design (and `ClusterTrust`'s docstring) has a cluster's console read its OWN cluster's Variables. For the domain cluster's console they are the same Variables; for a member cluster's instance this reads across the federation, and a domain outage would then take token verification down with it, which is exactly what the agent design avoids. See the report.
- The test proves: `/api/cameras` with `as of 2 s ago`, `/api/where/7` naming south/w-0 and `complete`, a PUT with `Idempotency-Key` forwarded to south, and a PUT with `{"worker": "w-1"}` refused with 400.
