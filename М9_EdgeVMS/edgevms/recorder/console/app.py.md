# app.py — the console: login, camera CRUD on operator-owned columns, status, timeline, events, and `/metrics`

**Role in the module.** Lesson 9 — the console: one query (`camera_status`), positions and reasons on separate axes, and the recorder's two exported signals. A FastAPI app built by `create_app(host)` where `host` is the `Worker`: the console runs on the Worker's loop and reads its `store`, `settings` and `key`. Operator-owned columns are the ONLY fields any request body can carry; there is no route that accepts `phase`, `observed_revision`, `last_seen` or `revision`, and no recorder column — which recorder owns a camera is decided for the operator, never by them. `/metrics` is what `health/rauc-health-check` reads locally and М13 scrapes. Depends on `console/auth.py` (sessions, argon2), `worker/reconciler.py` (position names), pydantic v2. The HTTP layer is listed in the README as not executed in the authoring sandbox.

## Module-level names
- `UNREACHABLE` — `"unreachable"`, the fourth position: the Worker has not reported within the window, so the console does not know (grey).

## Functions
### `_credential_free(v)`
Validator shared by both body models: `None` passes; otherwise the URL must be `rtsp` or `rtsps`, have a netloc, and contain no `@` in the netloc, else `ValueError` ("use cred_username / cred_secret"). A secret in a URL is a secret in every log line that URL reaches.

## `class LoginForm` (BaseModel) — `username`, `password`.

## `class CameraIn` (BaseModel)
Operator-owned columns and nothing else: `site_id` (optional), `name`, `rtsp_url`, `cred_username`, `cred_secret` (optional plaintext, encrypted by the store), `enabled` (default true), `retention_days` (30, 1..3650), `priority` (100, ≥ 0).
### `no_credentials_in_url(cls, v)` — `field_validator("rtsp_url")` → `_credential_free`.

## `class CameraPatch` (BaseModel)
The same eight fields, all optional, same validator; `model_dump(exclude_unset=True)` sends only what the client set.

## `create_app(host) -> FastAPI`
Builds `Sessions(settings.session_ttl)` and the app (`title="Recorder console"`, `version="0.10"`), then defines the closures and routes:

### `encrypt(secret, cid)` — `host.key.encrypt`, or HTTP 503 "no column key on this recorder" when the Worker started without one.

### `current_operator(authorization=Header(None)) -> int`
Dependency: strip `Bearer `, look the token up in `sessions`; 401 if absent or expired. Every route except `/login` and `/metrics` depends on it.

### `POST /login` — body `LoginForm`; reply `{"token": …}`
`store.operator(username)`; the same 401 for an unknown user and a wrong password — no username oracle. On success `sessions.issue(operator id)`.

### `GET /cameras` — `store.cameras()`: every camera with its operator columns (no `cred_secret`) and the controller columns read-only.
### `POST /cameras` (201) — body `CameraIn`; upserts the site if given (name = id), `store.create_camera(model_dump(), encrypt)`; reply `{"id": cid}`. This is `INSERT INTO cameras`, which starts a recording through NOTIFY and the next reconcile pass.
### `PATCH /cameras/{camera_id}` — body `CameraPatch`; `store.update_camera` with the set fields; 404 if no row; `{"ok": true}`. Any change bumps `revision` (trigger) and the Worker restarts the pipeline.
### `DELETE /cameras/{camera_id}` (204) — `store.delete_camera`; 404 if no row. The reconciler's stop loop stops the pipeline.

### `GET /status` — `build_status(store, settings)`: the operator's question.
### `GET /timeline?camera_id&start&end` — 422 unless `end > start`; `store.timeline(…, settings.segment_seconds)`.
### `GET /events?kind&camera_id&limit` — `limit` default 100, max 1000; `store.events`.
### `GET /metrics` (text/plain, no auth) — `render_metrics(build_status(...))`.

Returns the app.

## Functions (module level)
### `build_status(store, settings) -> dict`
Reads the `camera_status` view and all conditions. `window = 3 × report_interval`; a camera is `node_reporting` if `last_seen` is within it. Position: `UNREACHABLE` if not reporting; `CONVERGED` if `lag <= 0`; `STALLED` if `phase == "failed"` and at least one condition is false; else `LAGGING`. Each camera carries `id, name, site_id, enabled, position, lag, phase, silent_for_seconds` (None when the view found no segment in the last two hours) and its conditions (`condition, status, reason, since`). Top-level `node_reporting` is true if any camera is reachable — or if there are no cameras at all, so an empty recorder is not "unreachable".

### `render_metrics(status) -> str`
Prometheus text, over enabled cameras only: `recorder_cameras`, `recorder_cameras_lagging` (lag > 0), `recorder_cameras_stalled`, `recorder_camera_lag_max` (a distribution summary, never per camera — "a metric is not a database"), `recorder_camera_silent_seconds_max` (longest since any camera wrote a segment — "Alarm on this"; the health check's window test), `recorder_cameras_never_recorded` (enabled cameras with `silent_for_seconds` None — per the HELP text, "no segment in the last two hours"), `recorder_node_reporting` (0/1). Each with `# HELP` and `# TYPE gauge`.

## Notes
- Because the view bounds `lower(span)` to two hours, a camera silent for longer has `silent_for_seconds: null` and drops out of `recorder_camera_silent_seconds_max` into `recorder_cameras_never_recorded`; the health check's message "no camera has written a segment" and the README's "none has ever written a segment" describe that gauge as "never", which it is not once a camera has been silent for two hours. See `health/rauc-health-check.md`.
- `PATCH` with an explicit `"rtsp_url": null` passes the validator (`None` is allowed) and would reach `UPDATE cameras SET rtsp_url=NULL`, which the `NOT NULL` column rejects with a 500 rather than a 422.
- `/metrics` is deliberately unauthenticated: the health check runs on the box with no session, and Prometheus scrapes without one. `/cameras` is authenticated, which is why the health check's М10 branch (which fetches `/cameras` without a token) is not applicable to this console.
