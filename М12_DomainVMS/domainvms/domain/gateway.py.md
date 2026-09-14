# gateway.py — Lesson 3: the live tee with a leaky queue on the worker, and the gateway that subscribes to it once per camera and fans out to N browsers

**Role in the module.** Lesson 3, "who serves browsers: never a worker". A worker serves few, trusted, internal clients; the live gateway serves many, untrusted, external ones. The worker's pipeline carries a `tee` after the parser with a *leaky* queue on the live branch (М10 Lesson 4: a stalled subscriber loses frames, the recorder behind it never stalls). The gateway subscribes to that tee ONCE per camera and fans out to N viewers, each with its own leaky queue. It relays the viewer's token and the *cluster's* authoriser — its grants in its own Variables, verified offline (`grants.ClusterAuthoriser`) — decides; the gateway never authorises. It finds the worker through the directory ("where is camera 7": cluster, worker) and service discovery ("where is that worker's endpoint"), so a failover moves the endpoint and `reconnect` follows it. The docstring is explicit that this file is the *model* of that contract — subscriptions, fan-out, leaky queues, viewer counts, the token handed through; WebRTC, fMP4 and TURN are the transport underneath and belong to a bench with a browser (`deploy/gateway.nomad.hcl` is the job that would carry them). Used only by the Lesson 3 tests.

## `class LeakyQueue`
Bounded; a full queue drops its OLDEST frame; `push()` never blocks. A `deque(maxlen=…)`.
### `__init__(self, maxsize=30)` — the deque and a `dropped` counter.
### `push(self, frame)` — counts a drop if full (the deque evicts the oldest on append), appends.
### `drain(self) -> list` — everything queued, and empties the queue.

## `class LiveTee`
The worker side: one per camera, inside the pipeline; its subscribers are the gateway (one) — never a browser. `push` is called once per access unit by the pipeline, fire-and-forget.
### `__init__(self, camera)` — `subscribers: {who: LeakyQueue}`, `frames` pushed so far.
### `subscribe(self, who, maxsize=30) -> LeakyQueue` — returns the existing queue for `who` or creates one (idempotent per subscriber name).
### `unsubscribe(self, who)` — drops it.
### `push(self, frame)` — counts, pushes to every subscriber's queue; never blocks on any of them.
### `viewers` (property) — the number of subscribers: what the *worker* sees, which the tests keep at 1 while fifty browsers watch.

## `class Forbidden(Exception)`
Raised by an endpoint's `authorise` when the cluster refuses the token (the tests' fake authoriser raises it; `grants.ClusterAuthoriser` raises `PermissionError` — two names for the same refusal).

## `class WorkerLiveEndpoint` (dataclass)
A worker's live endpoint as found by service discovery.
- `worker: str` — the slot name.
- `tees: dict[int, LiveTee]` — the cameras it runs.
- `authorise: Callable[[token, camera], subject]` — the cluster's check, run *at the endpoint*: signature against the signer's public key it holds, then the cluster's grants.
### `open(self, camera, token, who) -> LeakyQueue`
Authorise (raises), then `tees[camera].subscribe(who)`; a camera not on this worker is `KeyError`.

## `class Upstream` (dataclass)
The gateway's one subscription for a camera: `worker`, `queue` (the tee's queue for the gateway), `viewers: {viewer: LeakyQueue}`.

## `class Gateway`
`where(camera) -> worker | None` is the directory; `endpoint(worker) -> WorkerLiveEndpoint` is service discovery. Both are looked up per subscription.

### `__init__(self, name, where, endpoint)` — `name` is the subscriber name the gateway uses at every tee; `upstreams: {camera: Upstream}`.

### `watch(self, camera, token, viewer, maxsize=30) -> LeakyQueue`
Ask the directory for the worker (`None` → `KeyError("… on no worker this domain can reach")`). If there is no upstream for the camera, or the upstream's worker differs from the directory's answer, open ONE subscription at the endpoint (authorising this viewer's token in the process) and record the `Upstream`. If the upstream exists, the token is still checked at the endpoint — "every viewer is still checked" — but no new subscription is made. Either way the viewer gets a fresh leaky queue of `maxsize`. `test_gateway_fans_out_and_the_worker_sees_one_viewer`: fifty `watch`es → `tee.viewers == 1`, `gw.viewers(7) == 50`; a bad token → `Forbidden`.

### `leave(self, camera, viewer)`
Removes the viewer's queue; when the last viewer leaves, unsubscribes the gateway from the tee (through `endpoint(worker).tees[camera]`) and drops the upstream — the worker's viewer count returns to 0.

### `pump(self) -> int`
Moves frames from each upstream queue to every viewer's queue; returns how many upstream frames moved. A slow viewer's queue leaks; nobody else waits. The test pushes 100 frames into a 30-deep upstream: `pump()` moves 30, the tee's queue for the gateway reports 70 dropped, and each 5-deep viewer queue holds 5 with 25 dropped.

### `reconnect(self, camera, token) -> str`
After a failover: the directory says the camera moved; if the upstream's worker differs, open a subscription at the new worker and replace the `Upstream`, *carrying the existing viewers over*. Returns the worker. `test_gateway_follows_a_failover`: home moves from w-0 to w-1; `reconnect` returns w-1, w-1's tee has one subscriber, the viewer count is still 1.

### `viewers(self, camera) -> int` — viewers on that camera's upstream, 0 if none.

## Notes
- `watch()` on a camera whose worker changed replaces the `Upstream` with an empty `viewers` dict, so existing viewers are silently dropped from fan-out; only `reconnect()` preserves them. Neither path unsubscribes the gateway from the old worker's tee. See the report.
- `deploy/gateway.nomad.hcl`'s `UPSTREAM_QUEUE=30` is the `maxsize` default here.
