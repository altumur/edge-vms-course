# crossing.py — Lesson 13: a server room's recorder recording a camera of another cluster, found through a source book the agent carries home

**Role in the module.** The recorder (М10B Lessons 15–16) found its camera's doors in its own cluster's heartbeats; a camera that is its own cluster publishes them elsewhere, and the recorder must not depend on the domain. So the domain — which reads every member — publishes, per recording cluster, a book of the doors of the cameras it records from other clusters, with their age; the agent carries it to `domain/sources`; the recorder resolves `ref:<serial>` against it. Data crosses; work does not. Which cluster records a camera is a stored domain decision, one per camera (a device serves one live session and one backfill).

## Module-level names
- `CROSSINGS = "domain/crossings"` — `{ref: recording cluster}` in the domain cluster.

## `class Crossings`
### `record(self, ref, on) -> dict` — 404 for a camera the read view never saw, 400 if `on` is its own cluster, 409 if another cluster records it; else CAS it in. Asking again is the same answer.
### `publish(self) -> dict` — each recording cluster's book from the read view's memory (`Snapshot.doors`), `{ref: {cluster, worker, live_url, playback_url, coverage, as_of, reachable}}`, into `domain/sources/<cluster>` when changed.

## `class Source` (dataclass), `class NotResolvable`

## Functions
### `resolve(cluster_vars, source, now) -> Source | None` — `None` for a source that is not `ref:` (the cluster's own, found as always); `NotResolvable` naming the ref if the book lacks it; else the entry with its `age`.
### `plan_backfill(src, ours, now, keep_days, settle, ask_device) -> (fetch, dropped)` — М10B's bounds and `vms.archive.subtract` against the book's coverage, then each hole clipped to what `ask_device()` says the card holds NOW; what is gone is dropped with `"no longer on the card"`, not retried.

## Notes
- The accepted failure: a camera that changes address while the domain is off is recorded from the old address until the domain's next publish (`test_a_camera_that_moved_while_the_domain_was_off_is_found_again_when_it_is_back`).
- The seam in `vms/recworker.py` is a fallback in `device_source` for a `ref:` source; it is described in the lesson, not wired here.
