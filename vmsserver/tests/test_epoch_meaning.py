"""What an older epoch MEANS. Same comparison, two answers: a writer that lost
the race, or a finished earlier run of work that ends."""
from w2cplatform.epoch import current_epoch, next_epoch
from w2cplatform.eventdatabase import EventDatabase
from w2cplatform.events import EventLog
from w2cplatform.spec import SubsystemSpec
from vms.config import DET_SPEC, DETJOB_SPEC, SPEC
from tests.conftest import Box


def _run(box, sub, unit, t, epoch):
    EventLog(box.archive, sub, unit, epoch).append(t, "lpr", cam=7)
    next_epoch(box.vars, f"{sub}/epoch/{unit}")


def _events(box, sub, unit, policy):
    db = EventDatabase(box.archive, "srv-1"); db.rebuild()
    cur = {(sub, unit): current_epoch(box.vars, f"{sub}/epoch/{unit}")}
    out = db.query(0, 1e12, current_epochs=cur, epoch_policy=policy)["events"]
    return sorted(((e["epoch"], e["epoch_is"], e["fenced"]) for e in out))


def test_one_finished_run_is_not_older_than_anything():
    """The thing I expected to be broken and is not: a job that ran once keeps the
    epoch it took, so its events are under the current one."""
    box = Box(); _run(box, "detjob", "7-lpr-1", 1000.0, 1)
    assert _events(box, "detjob", "7-lpr-1", {"detjob": "earlier-run"}) == [(1, "current", False)]


def test_a_second_run_does_not_strike_the_first_one_through():
    """The operator runs the same search again — a new model, a wider interval.
    Both results exist, both are real, and the page can offer to compare them."""
    box = Box()
    _run(box, "detjob", "7-lpr-1", 1000.0, 1)
    _run(box, "detjob", "7-lpr-1", 1100.0, 2)
    assert _events(box, "detjob", "7-lpr-1", {"detjob": "earlier-run"}) == \
        [(1, "earlier-run", False), (2, "current", False)]


def test_a_live_detector_still_strikes_its_zombie_through():
    """The same shape, under the default. Here an older epoch really is a writer
    that lost the race and kept writing, and nothing about this changed."""
    box = Box()
    _run(box, "det", "7-linecross", 1000.0, 1)
    _run(box, "det", "7-linecross", 1000.5, 2)
    assert _events(box, "det", "7-linecross", {"det": "fenced"}) == \
        [(1, "fenced", True), (2, "current", False)]


def test_a_subsystem_nobody_declared_keeps_the_old_answer():
    """An empty policy is the behaviour this code had before it could be asked."""
    box = Box()
    _run(box, "det", "7-linecross", 1000.0, 1)
    _run(box, "det", "7-linecross", 1000.5, 2)
    assert _events(box, "det", "7-linecross", {}) == [(1, "fenced", True), (2, "current", False)]


def test_the_default_is_what_every_subsystem_but_the_scan_says():
    assert DETJOB_SPEC.older_epochs == "earlier-run"
    for spec in (SPEC, DET_SPEC):
        assert spec.older_epochs == "fenced"


def test_a_meaning_the_page_cannot_draw_is_refused_at_load():
    unit = {"rows": "u", "id": "name", "fields": {"name": {"type": "string"}}}
    try:
        SubsystemSpec.from_dict({"name": "x", "unit": unit, "events": {"older_epochs": "superseded"}})
        raise AssertionError("accepted an unknown meaning")
    except ValueError as e:
        assert "fenced or earlier-run" in str(e)


def test_the_console_hands_its_mounts_meanings_to_the_query():
    """`/events` merges across subsystems, so the console answering the request
    has to know what an older epoch means in a subsystem it does not own."""
    from w2cplatform.console import Mount, SpecConsole
    from w2cplatform.spec import SpecController
    box = Box()
    root = SpecConsole(SpecController(SPEC, box.vars, box.objects, wall=box.wall))
    jobs = SpecConsole(SpecController(DETJOB_SPEC, box.vars, box.objects, wall=box.wall))
    m = Mount(root, {"detjob": jobs})
    assert m.epoch_policy == {"vms": "fenced", "detjob": "earlier-run"}
    assert root.epoch_policy is m.epoch_policy and jobs.epoch_policy is m.epoch_policy   # one dict, shared

    det = SpecConsole(SpecController(DET_SPEC, box.vars, box.objects, wall=box.wall))
    m.mount("det", det)
    assert root.epoch_policy["det"] == "fenced"          # a console mounted later is known to the ones before it
