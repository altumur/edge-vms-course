"""The scenario language, and where it is checked.

`auto.subsystem.yaml` says a scenario has a `when`, a `within` and a `then`.
The platform checks that those are JSON and small and stops there, because a
generic loader that validated a trigger would be a generic loader that knows
what a trigger is. Everything else is `vms/auto.py`, and it runs at the door —
while the operator is still looking at what they typed, which is the only
moment the answer is cheap.
"""
import json

from w2cplatform.spec import Refused, SubsystemSpec
from vms.auto import ACTIONS, AutoController, fires, refuse_scenario
from vms.config import AUTO_SPEC
from tests.conftest import Box


DOOR = {"name": "door-on-badge",
        "when": [{"sub": "vms", "kind": "io.input", "unit": "12", "match": {"port": "1", "value": "closed"}},
                 {"sub": "det", "kind": "motion", "unit": "7"}],
        "within": 30,
        "then": [{"sub": "vms", "action": "output", "unit": "12", "port": 2, "pulse_ms": 500},
                 {"sub": "rec", "action": "record", "cam": "7", "minutes": 10, "archive": "cold"}]}


def _con(box):
    return AutoController(box.vars.as_writer("console", AUTO_SPEC.acl_console()), box.objects, wall=box.wall)


def test_a_scenario_is_a_row_and_its_shapes_survive_the_round_trip():
    """JSON in the row, structure out of it. The fifth subsystem needed the type
    and none of the four before it did: a scenario's triggers are a LIST OF
    SHAPES, and flattening that into fields would cap it at one trigger or
    invent `when_1_sub` — a schema pretending not to be one."""
    box = Box()
    con = _con(box)
    row = con.create(DOOR)

    assert row["id"] == "door-on-badge"
    back = con.unit("door-on-badge")
    assert back["when"][0]["match"] == {"port": "1", "value": "closed"}      # parsed, not a string
    assert back["then"][1]["minutes"] == 10
    assert back["within"] == 30 and back["rate_per_minute"] == 6             # the default ceiling is on

    stored, _ = box.vars.get("auto/scenarios/door-on-badge")                 # …and in the store it is text
    assert json.loads(stored["when"])[1]["kind"] == "motion"


def test_what_the_platform_checks_and_what_it_does_not():
    """The line, in two assertions. JSON and a ceiling are the platform's; what
    a trigger MEANS is the subsystem's."""
    box = Box(); con = _con(box)

    try:
        con.create({**DOOR, "name": "broken", "when": "{not json"})
        raise AssertionError("a `json` field took something that is not JSON")
    except Refused as e:
        assert "not JSON" in str(e)

    try:
        con.create({**DOOR, "name": "huge", "when": [{"sub": "vms", "kind": "x", "match": {"a": "b" * 5000}}]})
        raise AssertionError("a single field ate the row's ceiling")
    except Refused as e:
        assert "ceiling" in str(e)

    # …and the platform would happily have taken this one: it is valid JSON and small
    assert AUTO_SPEC.fields["when"].type == "json"
    try:
        con.create({**DOOR, "name": "nonsense", "when": [{"sub": "vms", "kind": "io.input", "colour": "red"}],
                    "within": 0})
        raise AssertionError("a trigger with an invented key was accepted")
    except Refused as e:
        assert "no key 'colour'" in str(e)


def test_every_refusal_says_what_would_be_right():
    """A refusal that names only what is wrong makes the operator guess. These
    are read by somebody mid-edit, so each one carries the alternative."""
    box = Box(); con = _con(box)
    cases = [
        ({"when": DOOR["when"], "within": 0}, "within how many seconds"),
        ({"when": [DOOR["when"][0]], "within": 30}, "with one trigger there is nothing to window"),
        ({"when": [], "within": 0}, "non-empty list"),
        ({"then": [{"sub": "vms", "action": "reboot", "unit": "1"}]}, "this course files"),
        ({"then": [{"sub": "rec", "action": "record", "cam": "7"}]}, "needs 'minutes'"),
        ({"then": [{"sub": "vms", "action": "output", "unit": "1", "port": 1, "volume": "x"}]}, "no field 'volume'"),
        ({"when": [{"sub": "vms"}], "within": 0}, "names the subsystem it watches"),
        ({"rate_per_minute": 9999}, "between 1 and 600"),
    ]
    for i, (patch, want) in enumerate(cases):
        try:
            con.create({**DOOR, "name": f"bad{i}", **patch})
            raise AssertionError(f"accepted {patch}")
        except Refused as e:
            assert want in str(e), (patch, str(e))


def test_an_edit_is_checked_as_the_scenario_it_would_become():
    """The likelier of the two doors: the scenario that runs the site was
    written months ago and is being adjusted at speed. Validating the half being
    sent would pass anything; the check is on the row as it would end up."""
    box = Box(); con = _con(box)
    con.create(DOOR)

    try:
        con.update("door-on-badge", {"when": [DOOR["when"][0]]})       # one trigger, and `within` still 30
        raise AssertionError("an edit left the scenario unrunnable")
    except Refused as e:
        assert "nothing to window" in str(e)

    con.update("door-on-badge", {"when": [DOOR["when"][0]], "within": 0})   # both together: fine
    assert con.unit("door-on-badge")["within"] == 0


def test_matching_is_equality_and_nothing_else():
    """The evaluator's half of the language, beside the validation on purpose:
    two files would drift, and the drift would look like a scenario that never
    fires — the hardest kind of bug to see, because nothing happens."""
    ev = {"subsystem": "vms", "unit": "12", "kind": "io.input", "t": 100.0, "port": "1", "value": "closed"}
    assert fires({"sub": "vms", "kind": "io.input"}, ev)
    assert fires({"sub": "vms", "kind": "io.input", "unit": "12"}, ev)
    assert fires({"sub": "vms", "kind": "io.input", "match": {"port": 1}}, ev)       # numbers compare as text
    assert not fires({"sub": "det", "kind": "io.input"}, ev)
    assert not fires({"sub": "vms", "kind": "motion"}, ev)
    assert not fires({"sub": "vms", "kind": "io.input", "unit": "13"}, ev)
    assert not fires({"sub": "vms", "kind": "io.input", "match": {"value": "open"}}, ev)
    assert not fires({"sub": "vms", "kind": "io.input", "match": {"nothing": "x"}}, ev)


def test_the_catalogue_is_the_boundary():
    """What automation may ask for is narrower than what the subsystems can do,
    and the narrow list is the point: every entry is a request somebody has to
    have written a performer for."""
    assert set(ACTIONS) == {("vms", "output"), ("vms", "preset"), ("rec", "record")}
    for (sub, action), spec in ACTIONS.items():
        assert spec["need"], f"{sub}.{action} names no required field"

    # the snapshot leaves the cluster, and what a site pulses is nobody's business up there
    assert AUTO_SPEC.snapshot == ["name", "enabled"]
    assert "when" not in AUTO_SPEC.snapshot and "then" not in AUTO_SPEC.snapshot


def test_the_spec_says_nothing_about_doors():
    """The subsystem is domain from the first word, and the platform stays where
    it was: the spec it loads has fields, types and a placement policy, and not
    one line of it means anything about sensors."""
    d = SubsystemSpec.load(AUTO_SPEC.path) if hasattr(AUTO_SPEC, "path") else AUTO_SPEC
    assert d.requires == "resource" and d.servers == "shared"
    assert [f.type for f in (d.fields[n] for n in ("when", "then"))] == ["json", "json"]
