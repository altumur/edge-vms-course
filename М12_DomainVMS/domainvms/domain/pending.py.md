# pending.py — Lesson 9: an edit kept for a cluster that is off, per field, beside the grants, applied by the cluster's own console when it is back

**Role in the module.** Lesson 3 answered `503` when the owning cluster did not answer. Right for a server room in your own building; useless for a branch on a weak link, and more so for a camera that is its own cluster (Part two). The domain keeps the edit where it keeps everything a cluster needs from it — the domain cluster's Variables, `domain/pending/<cluster>`, beside `domain/grants/<cluster>` — and the cluster's agent carries it home. Nothing about ownership moves: the cluster's console is still the only writer of its rows, the agent still writes nothing but `domain/*`. Used by `api.ConsoleAPI._keep_for` (to keep), `agent.DomainAgent.sync` (to carry and apply), and the domain's own collect loop.

## Module-level names
- `PENDING_PATH = "domain/pending"`, `OUTCOMES_PATH = "domain/outcomes"`.

## `class PendingEdits`
The domain's side. One Variable per cluster, one item per camera by the DOMAIN's name (`ref`), each a JSON entry `{fields: {f: {old, new, via?, conflict?}}, rev, subject, since, refused?}`.
### `of(self, cluster) -> dict` — the entries waiting for a cluster.
### `add(self, cluster, camera, fields, base, subject) -> dict`
Merge an edit into the entry, by CAS, retried on `Conflict`. A new field: `{old: base[f], new}`. A field the camera reported as a conflict: `{old: that conflict, new}` — a person resolving it, measured against what the camera holds now. A waiting field with a different value: its previous `new` goes into `via` (it may already be on the camera, applied and not yet reported) and it takes the new one. `rev` +1, `subject` and `since` set, any refusal dropped (a new edit is a new decision).
### `reconcile(self, cluster, outcomes)`
Fold a cluster's `domain/outcomes` in — only an outcome whose `rev` equals the entry's: never by clock. `gone` drops the entry; `refused` annotates it; applied and already-there fields go; conflicts are annotated with `conflict: <current>`; an empty entry goes.
### `collect(self, fed)` — for every cluster with something waiting, read its `domain/outcomes` through its stores; a silent cluster keeps its edits waiting.

## Functions
### `apply_pending(entries, current, console, now) -> dict`
The cluster's side, run by its agent. Per camera: `current(ref)` is `None` → `gone`. Per field: holds `new` → already; holds `old` or a `via` → apply; anything else → conflict `{old, new, current}`. The fields to apply go to `console.update_camera(id, fields, subject)` — as the operator, the grant checked NOW; an `ApiError` is recorded as `refused`, not retried. Returns `{ref: {rev, at, applied, already, conflicts, refused?|gone?}}`.

## Notes
- `test_an_edit_made_before_the_last_one_was_confirmed_is_not_a_conflict` is the race `via` and the `rev` match exist for; it was found by that test in the first design.
- Tested in `tests/test_lesson9_pending.py` (9 tests) and end to end on a device in `test_lesson10_cluster_of_one.py::test_a_kept_edit_reaches_a_real_camera_when_it_boots`.
