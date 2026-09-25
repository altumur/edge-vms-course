"""Lesson 9 — an edit for a cluster that is off.

Lesson 3 forwarded an edit to the owning cluster and answered 503 when the cluster did not answer. That is
right where a whole cluster is rarely unreachable, and wrong where a cluster is ONE DEVICE — a camera that is
its own cluster, off for its power, its switch, its maintenance, and edited in bulk with forty-nine others.

So the domain keeps the edit, and keeps it the way it keeps everything a cluster needs from it: in the domain
cluster's Variables, under `domain/pending/<cluster>`, beside `domain/grants/<cluster>`, carried home by that
cluster's agent when it is back. The cluster's own console applies it. Nothing about ownership moves: the
cluster's console is still the only writer of its rows, and the agent still writes nothing but `domain/*`.

    what is kept     per FIELD, the value the domain last saw (`old`), the value wanted (`new`), and any value
                     it wanted before that may already be on the camera (`via`)
    when it lands    a field that still holds `old` — or a `via` — takes `new`; one that holds `new` is done;
                     one that holds anything else moved underneath the edit — a CONFLICT, shown, not decided
    as whom          the operator who made it, with that operator's grant checked when it is applied
    how it returns   the agent writes the outcome in its own cluster's `domain/outcomes`; the domain reads
                     it the way it reads a snapshot, and clears what landed
"""
from __future__ import annotations

import json
import time

from cluster.variables import Conflict, Variables

from .api import ApiError
from .federation import Unreachable

PENDING_PATH, OUTCOMES_PATH = "domain/pending", "domain/outcomes"


def _load(items: dict | None) -> dict[str, dict]:
    return {k: json.loads(v) for k, v in (items or {}).items()}


def _dump(entries: dict[str, dict]) -> dict[str, str]:
    return {k: json.dumps(v, ensure_ascii=False, sort_keys=True) for k, v in entries.items()}


class PendingEdits:
    """The domain's side. One row per cluster, one item per camera in it, by the DOMAIN's name for the
    camera (its `ref`) — a cluster's own id is the cluster's."""

    def __init__(self, domain_vars: Variables, wall=time.time):
        self.vars, self.wall = domain_vars, wall

    def _path(self, cluster: str) -> str:
        return f"{PENDING_PATH}/{cluster}"

    def of(self, cluster: str) -> dict[str, dict]:
        items, _ = self.vars.get(self._path(cluster))
        return _load(items)

    # Keep an edit for a camera whose cluster did not answer.
    #
    # Per FIELD, and merged, because the camera needs the last value of each field, not a replay of every
    # edit made while it was off: a field already waiting keeps the value it was based on — while the camera
    # is off that is what the domain last saw, and nothing new arrives to move it — takes the latest wanted
    # value, and remembers the ones it wanted in between (`via`), because one of those may already be on the
    # camera, applied and not yet reported back. A field the camera reported as a conflict is different — an edit to it
    # now is a person resolving it, "apply again", and it is measured against what the camera now holds,
    # or it would conflict for ever.
    #
    # `rev` counts the edits to this camera's entry. The outcome the agent reports names the `rev` it
    # applied, and only an outcome for the CURRENT rev clears anything: comparing the agent's clock with the
    # domain's would be comparing two machines' clocks, and an old outcome must never clear a newer edit.
    def add(self, cluster: str, camera, fields: dict, base: dict, subject: str | None, retries: int = 20) -> dict:
        camera = str(camera)
        for _ in range(retries):
            items, idx = self.vars.get(self._path(cluster))
            entries = _load(items)
            e = entries.get(camera) or {"fields": {}, "rev": 0}
            for f, new in fields.items():
                have = e["fields"].get(f)
                if have is None:
                    e["fields"][f] = {"old": base.get(f), "new": new}
                elif "conflict" in have:
                    e["fields"][f] = {"old": have["conflict"], "new": new}
                elif have["new"] != new:
                    # The value this field wanted until now may already be on the camera — applied and
                    # reported, but not yet read back here. Remember it: a camera holding it holds a value the
                    # domain itself sent, which is a base for the next edit and not somebody else's change.
                    via = have.get("via", [])
                    have["via"] = via if have["new"] in via else via + [have["new"]]
                    have["new"] = new
            e.update(rev=e["rev"] + 1, subject=subject, since=self.wall())
            e.pop("refused", None)                           # a new edit is a new decision; the old refusal was about the old one
            entries[camera] = e
            try:
                self.vars.put(self._path(cluster), _dump(entries), cas=idx)
                return e
            except Conflict:                                 # somebody kept another edit meanwhile: read again
                continue
        raise RuntimeError(f"could not keep an edit for {camera} in {cluster}: {retries} CAS conflicts")

    # Fold one cluster's outcomes into what is waiting for it. Applied and already-there fields go; a
    # conflict stays, annotated with what the camera holds, until a person decides; a refusal stays with its
    # reason; a camera that is no longer in the cluster takes its edit with it.
    def reconcile(self, cluster: str, outcomes: dict[str, dict]) -> None:
        items, idx = self.vars.get(self._path(cluster))
        entries, changed = _load(items), False
        for camera, out in outcomes.items():
            e = entries.get(camera)
            if e is None or out.get("rev") != e.get("rev"):
                continue                                     # about an edit that is gone, or an older one
            changed = True
            if out.get("gone"):
                entries.pop(camera)
                continue
            if out.get("refused"):
                e["refused"] = out["refused"]
                continue
            for f in out.get("applied", []) + out.get("already", []):
                e["fields"].pop(f, None)
            for f, c in out.get("conflicts", {}).items():
                if f in e["fields"]:
                    e["fields"][f]["conflict"] = c["current"]
            if not e["fields"]:
                entries.pop(camera)
        if changed:
            self.vars.put(self._path(cluster), _dump(entries), cas=idx)

    # Read every cluster that has something waiting, the way the directory reads a snapshot, and fold in what
    # it says happened. A cluster that does not answer keeps its edits waiting — which is what they were
    # doing anyway.
    def collect(self, fed) -> None:
        for name, c in fed.clusters.items():
            if not self.of(name):
                continue
            try:
                items, _ = c.vars.get(OUTCOMES_PATH)
            except Unreachable:
                continue
            self.reconcile(name, _load(items))


# The cluster's side, run by its agent: apply what was carried home, per field, through the cluster's own
# console, as the operator who made the edit. `current(ref)` gives `(cluster id, row)` for the camera the
# domain calls `ref`, or None if the cluster no longer holds it.
#
# The console's refusal is the grant check, made NOW — an edit can wait a week and an operator can lose
# the right to make it within it — and it is recorded as a refusal, not retried and not forced. Any other
# failure is not a refusal and is left to raise: the agent's loop logs it and tries again next pass.
def apply_pending(entries: dict[str, dict], current, console, now: float) -> dict[str, dict]:
    outcomes: dict[str, dict] = {}
    for camera, e in entries.items():
        found = current(camera)
        if found is None:
            outcomes[camera] = {"rev": e["rev"], "at": now, "gone": True}
            continue
        cid, row = found
        apply, already, conflicts = {}, [], {}
        for f, d in e["fields"].items():
            cur = row.get(f)
            if cur == d["new"]:
                already.append(f)                            # carried home twice before the domain cleared it
            elif cur == d["old"] or cur in d.get("via", []):
                apply[f] = d["new"]                          # what the edit was based on, or a value the domain sent before
            else:
                conflicts[f] = {"old": d["old"], "new": d["new"], "current": cur}
        out = {"rev": e["rev"], "at": now, "applied": [], "already": sorted(already), "conflicts": conflicts}
        if apply:
            try:
                console.update_camera(cid, apply, e.get("subject"))
                out["applied"] = sorted(apply)
            except ApiError as err:
                out["refused"] = err.detail
        outcomes[camera] = out
    return outcomes
