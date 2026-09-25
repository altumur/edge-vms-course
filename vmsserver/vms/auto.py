"""The scenario as data: what a trigger may say, what an action may ask for,
and the refusal that happens at the door instead of at three in the morning."""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # auto.py — the seventh subsystem's controller, and the whole of the scenario language
#
# **Role in the module.** `auto.subsystem.yaml` says a scenario has a `when`, a `within` and a `then`; the
# platform checks that those are JSON and small, and stops there, because a generic loader that validated a
# trigger would be a generic loader that knows what a trigger is. This file is where the shapes mean
# something: `AutoController.create` refuses a scenario the evaluator could not run, at the moment the
# operator presses the button.
#
# **Why the language is fixed and not an expression.** An expression language is an interpreter: a parser,
# a grammar, precedence, errors at evaluation time, and a page that cannot offer a form because it cannot
# know what the operator will type. A fixed shape is a dozen checks, a form the console can build from the
# catalogue, and every refusal arriving while somebody is still looking at what they wrote. When the shape
# stops fitting, the honest move is another field — not a parser.
#
# **The catalogue is the boundary.** `ACTIONS` names the pairs `(subsystem, action)` this course knows how
# to file a request for, with the fields each one needs. It lives here and not in the platform for the same
# reason the trigger shapes do; it lives here and not in each target subsystem because it is a statement
# about what AUTOMATION may ask for, which is narrower than what those subsystems can do.
#
# ## Public API
# - `TRIGGER_KEYS`, `ACTIONS` — the language, as data.
# - `refuse_scenario(fields)` — raises `Refused` with a sentence an operator can act on.
# - `AutoController` — `SpecController` over `AUTO_SPEC`, refusing on create and on update.
# - `fires(trigger, event)` — does this event match this trigger. The evaluator's half of the language,
#   here beside the validation so the two cannot drift.
# ================================================================================================
from __future__ import annotations

import time

from w2cplatform.objects import ObjectStore
from w2cplatform.spec import Refused, SpecController
from w2cplatform.variables import Variables

from .config import AUTO_SPEC

# A trigger names the subsystem whose events it watches and the kind of event, and may narrow it to one
# unit and to fields of the event. Four keys and no more: anything a fifth key would express is either a
# second trigger or a thing the evaluator cannot know.
TRIGGER_KEYS = ("sub", "kind", "unit", "match")

# What automation may ask for. `(subsystem, action)` -> the fields the request needs, and the ones it may
# have. Every one of these becomes a row in `<sub>/requests/<id>`, performed by whoever holds the unit.
ACTIONS = {
    ("vms", "output"): {"need": ("unit", "port"), "may": ("state", "pulse_ms")},
    ("vms", "preset"): {"need": ("unit", "n"), "may": ()},
    ("rec", "record"): {"need": ("cam", "minutes"), "may": ("archive",)},
}

# A scenario may not ask for the world. The ceiling is on the SHAPE — how many triggers, how long a window
# — because a scenario with forty triggers and an hour-long window is a query over the whole log run every
# pass, and it would be the operator's own console that got slow.
MAX_TRIGGERS = 4
MAX_WITHIN = 3600
MAX_ACTIONS = 4


def _dicts(v, what: str) -> list[dict]:
    if not isinstance(v, list) or not v:
        raise Refused(f"`{what}` is a non-empty list")
    if len(v) > (MAX_TRIGGERS if what == "when" else MAX_ACTIONS):
        raise Refused(f"`{what}` takes at most {MAX_TRIGGERS if what == 'when' else MAX_ACTIONS} entries")
    for e in v:
        if not isinstance(e, dict):
            raise Refused(f"every entry of `{what}` is an object, not {type(e).__name__}")
    return v


# The whole language, checked. Every message names the thing that is wrong and what would be right: this
# runs while the operator is still looking at what they typed, which is the only moment the answer is cheap.
def refuse_scenario(fields: dict) -> None:
    for t in _dicts(fields.get("when"), "when"):
        bad = [k for k in t if k not in TRIGGER_KEYS]
        if bad:
            raise Refused(f"a trigger has no key {bad[0]!r} — it takes {', '.join(TRIGGER_KEYS)}")
        if not str(t.get("sub", "")) or not str(t.get("kind", "")):
            raise Refused("a trigger names the subsystem it watches and the kind of event: {sub, kind}")
        if "match" in t and not isinstance(t["match"], dict):
            raise Refused("`match` is an object of field: value, compared against the event's fields")

    when, within = fields.get("when") or [], int(fields.get("within") or 0)
    if len(when) > 1 and within <= 0:
        raise Refused("two triggers need `within`: within how many seconds do they count as together")
    if len(when) == 1 and within:
        raise Refused("`within` is the window between triggers; with one trigger there is nothing to window")
    if within > MAX_WITHIN:
        raise Refused(f"`within` is at most {MAX_WITHIN} seconds")

    for a in _dicts(fields.get("then"), "then"):
        key = (str(a.get("sub", "")), str(a.get("action", "")))
        spec = ACTIONS.get(key)
        if spec is None:
            known = ", ".join(f"{s}.{n}" for s, n in sorted(ACTIONS))
            raise Refused(f"this course files {known} — not {key[0] or '?'}.{key[1] or '?'}")
        allowed = {"sub", "action", *spec["need"], *spec["may"]}
        bad = [k for k in a if k not in allowed]
        if bad:
            raise Refused(f"{key[0]}.{key[1]} has no field {bad[0]!r} — it takes {', '.join(sorted(allowed))}")
        missing = [k for k in spec["need"] if a.get(k) in (None, "")]
        if missing:
            raise Refused(f"{key[0]}.{key[1]} needs {missing[0]!r}")

    rate = int(fields.get("rate_per_minute") or 0)
    if rate and not 1 <= rate <= 600:
        raise Refused("`rate_per_minute` is between 1 and 600 — automation without a ceiling can ring")


# Does this event set off this trigger? The evaluator's half of the language, and it lives beside the
# validation deliberately: two files would drift, and the drift would look like a scenario that never
# fires — the hardest kind of bug to see, because nothing happens.
#
# An event is what the console's merge hands over, and the key names are ITS, not ours: `subsystem`, not
# `sub` (`eventdatabase.py`, the row built in `query`). A trigger says `sub` because that is what an
# operator writes; the comparison reads what the log actually carries. Getting this wrong is invisible in
# a test with a hand-built event and total on a box: the scenario simply never fires.
#
# Matching is equality on strings and nothing else. No ranges, no negation, no substring: each of those is
# a question about what the operator meant, and the answer belongs in another trigger or in another field.
#
# A line carrying `repeats` is the summary of a window the WRITER suppressed (`Suppressor`, М10A урок 12),
# and it never fires. It is not a new observation: the first line of that window was, and it fired this
# scenario already. Acting on the summary too would open the door a second time for one continuous event —
# and would do it worse the longer the storm ran, because the louder the sensor, the more summaries.
# The summary exists for the operator reading the timeline and for whoever reconstructs the incident.
def fires(trigger: dict, event: dict) -> bool:
    if "repeats" in event:
        return False
    if str(trigger.get("sub", "")) != str(event.get("subsystem", "")):
        return False
    if str(trigger.get("kind", "")) != str(event.get("kind", "")):
        return False
    if trigger.get("unit") not in (None, "") and str(trigger["unit"]) != str(event.get("unit", "")):
        return False
    for k, v in (trigger.get("match") or {}).items():
        if str(event.get(k, "")) != str(v):
            return False
    return True


class AutoController(SpecController):
    """The platform's controller over `auto.subsystem.yaml`, plus the one thing
    the platform cannot do: refuse a scenario that says nothing runnable."""

    def __init__(self, vars_: Variables, objects: ObjectStore, capacity: int = 50, wall=time.time,
                 cluster: str | None = None):
        super().__init__(AUTO_SPEC, vars_, objects, capacity, wall, cluster)

    # Both doors, because an edit can break a scenario exactly as a create can — and an edit is the likelier
    # of the two: the scenario that runs the site was written months ago and is being adjusted at speed.
    # The platform's check runs FIRST, and the order is not tidiness: it answers "is this JSON, and does it
    # fit", and everything below assumes the answer is yes. Ask what a trigger means before knowing it
    # parsed and the operator gets a sentence about triggers for a missing brace.
    def create(self, fields: dict) -> dict:
        self.spec.refuse(fields)
        refuse_scenario(fields)
        return super().create(fields)

    def update(self, uid, fields: dict) -> dict:
        row = self.unit(uid)
        if row is None:
            raise Refused(f"no scenario {uid}")
        self.spec.refuse(fields)                    # the patch: shapes and sizes
        refuse_scenario({**row, **fields})          # the scenario as it WOULD be, not the half being sent
        return super().update(uid, fields)
