# api.py — Lesson 3: the domain console's write façade — idempotency keys, forwarding to the owning cluster's console, and the fields a client may never set

**Role in the module.** Lesson 3, "the API, and what it refuses". An edit goes to the directory ("where is camera 7") and then to the *owning cluster's* console — the one in front of that cluster's controller, the only writer of its `vms/*` (`../../../М11_ClusterVMS/clustervms/cluster/console.py.md`) — and that cluster's grants decide. The domain console owns nothing and never writes a camera row on its own account; a create goes to the cluster the placement service chose, and that cluster's controller places it on a worker (М11 Lesson 10). The domain never names a worker or a server. The docstring's four rules: idempotency keys (a retried PUT is the same PUT, not a second edit); what it refuses (placement at either level, and what a worker observes or takes); `phase`/`position` passed through untouched; authentication shipped off in Lesson 3 and said so, added as `verifier` in Lesson 4. Used by `console.Console` (HTTP) and the Lesson 3 tests, which supply a `FakeClusterConsole`.

## Module-level names
- `FORBIDDEN_FIELDS = ("cluster", "worker", "server", "placement", "epoch", "observed_revision", "phase", "revision")` — the keys a client PUT/POST may not contain. The refusal message explains the ownership: the domain places on a cluster and the cluster's controller on a worker, each with a stored reason; `epoch`, `phase` and `observed_revision` are the worker's; `revision` is the controller's.

## `class ClusterConsole(Protocol)`
What the domain can ask of a cluster's console: `update_camera(camera, fields, subject) -> dict` and `create_camera(fields, subject) -> dict` — the same two writes its controller offers, with the caller's subject so the cluster's grants can decide. In production this is that cluster's `console` Nomad service; in tests a dict of fakes.

## `class ApiError(Exception)` (dataclass)
- `status: int`, `detail: str` — an HTTP status and its sentence; `__str__` is `"<status>: <detail>"`. `console.py` turns it into the response.

## `class ConsoleAPI`

### `__init__(self, directory, consoles, verifier=None, pending=None, last_known=None)`
`directory` is anything with `where(camera) -> Answer` — a `DomainDirectory`, or since Lesson 11 a `ReadView`, which answers from memory; `consoles(cluster_name) -> ClusterConsole` finds a cluster's console (its Nomad service in production, a dict lookup in tests); `verifier(token) -> subject` is Lesson 4's offline token check — `None` means unauthenticated, and every response says so (`"authenticated": False`). `pending` (a `PendingEdits`) and `last_known` (`ReadView.last_known`) are Lesson 9: without them an edit for a silent cluster is `503`, as in Lesson 3. `_seen` is the idempotency store: key → the response already given.

### `_subject(self, token) -> str | None`
No verifier → `None` (unauthenticated mode). A verifier and no token → `ApiError(401, "a token is required")`. Otherwise whatever the verifier returns (it raises its own error for a bad token).

### `_refuse_placement(self, fields)`
Any key in `FORBIDDEN_FIELDS` → `ApiError(400, "a client may not set [...]: …")`. `test_api_refuses_placement_at_both_levels_and_is_idempotent` tries `worker`, `cluster`, `server`, `placement`, `phase`, `epoch` and gets 400 for each.

### `update_camera(self, camera, fields, idempotency_key, token=None) -> dict`
A seen key returns the stored response object itself (the test asserts `r1 is r2` and one edit at the fake console). Otherwise: refuse placement fields, resolve the subject, `directory.where(camera)`. Not found → `_keep` (Lesson 9), and failing that `404` if the answer is complete, `503` if a cluster was unreachable, with `Answer.sentence()` as the detail (`test_api_says_503_not_404_when_a_cluster_is_unreachable`). Found → `consoles(cluster).update_camera(camera, fields, subject)`; if that raises `Unreachable` — the owner went silent between the directory and the forward, or since the read view's last pass — the edit is kept for that cluster with `_keep_for`, and failing that it is `503` (`test_a_bulk_edit_answered_from_memory_keeps_what_went_silent_since_the_pass`). The response `{camera, cluster, worker, result, authenticated}` is stored under the key.

### `_keep(self, camera, fields, subject, ans)` and `_keep_for(self, cluster, camera, fields, subject)`
Lesson 9. `_keep` keeps an edit only when the answer is incomplete and the read view last saw the camera in one of the clusters that did not answer — a camera missing from a cluster that DID answer is gone, not waiting. `_keep_for` does the keeping for a named cluster: `PendingEdits.add` against the row last seen, and a response `{camera, cluster, pending: True, fields, detail, authenticated}` — which the console serves as `202`.

### `create_camera(self, fields, cluster, idempotency_key, token=None) -> dict`
The docstring: `cluster` comes from the placement service's stored decision, which the console reads and forwards — it does not choose; the worker is the cluster controller's decision, *returned*, never sent. Same idempotency and refusal; forwards `fields` (which should carry the `ref` the domain assigned) to that cluster's console; response `{cluster, result, authenticated}`. The test's fake returns `{"id": 1, "worker": "w-0"}` and the test asserts the worker came back from the cluster.

## Notes
- The refusal is checked before the directory lookup, so a bad PUT for an unknown camera is 400, not 404; and a refused call is never stored under its key, so a corrected retry with the same key goes through.
- `_seen` is per process memory. М11's cluster console kept its Idempotency-Keys in raft (`vms/idem/*`) so any instance answers a retry; `deploy/console.nomad.hcl` runs `count = 2` of this console, and a retry that lands on the other instance is a second edit. See the report.
- `update_camera` forwards `camera` — the domain's ref — to the cluster console's `update_camera(camera, …)`. М11's console addresses cameras by their cluster-local `id`; the `Answer` does not carry the row's `id`, so nothing here translates ref → id. In the tests the fake console ignores the number. See the report.
- The module docstring's list of refused fields omits `revision`, which `FORBIDDEN_FIELDS` includes.
