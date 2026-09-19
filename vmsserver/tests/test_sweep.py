"""Collecting blobs nothing names any more — the one thing in the platform that deletes an object.

A blob key is the digest of its bytes, so every edit of a `blob` field makes a
NEW permanent object. That is what makes blobs different from everything else
in the object store: a heartbeat's key is reused by the next instance of the
slot, so stale heartbeats are bounded by the number of slot names and are kept
on purpose (a worker reads the previous instance's heartbeat to measure its own
failover). Blobs grow with the number of EDITS over the system's lifetime, and
until now nothing ever reclaimed them.

The obvious implementation is wrong: "delete every blob no row names" races
with `put_blob`, which writes the object BEFORE the row that names it. These
tests are mostly about that race.
"""
import json

from vms.config import DET_SPEC
from w2cplatform.console import SpecConsole
from w2cplatform.spec import SpecController
from w2cplatform.variables import Conflict
from tests.conftest import Box

MASK_A, MASK_B, MASK_C = b"a" * 900, b"b" * 900, b"c" * 900


def _det(box):
    ctl = SpecController(DET_SPEC, box.vars, box.objects, wall=box.wall)
    return ctl, SpecConsole(ctl)


def _blobs(box):
    return sorted(k.rsplit("/", 1)[1] for k in box.objects.list("det/blobs/"))


def test_a_blob_nothing_names_is_collected_but_never_on_the_pass_that_noticed_it():
    """The shape of the whole thing: noticing and deleting are different passes,
    because a blob created between them must survive, and the only way to know it
    was created between them is that it is not on the list."""
    box = Box()
    ctl, _ = _det(box)
    ctl.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    ctl.update("7-linecross", {"mask": ctl.put_blob(MASK_A)})
    ctl.update("7-linecross", {"mask": ctl.put_blob(MASK_B)})     # A is now an orphan
    assert len(_blobs(box)) == 2

    first = ctl.sweep_blobs()
    assert first == {"marked": 1, "deleted": 0, "waiting": 0}
    assert len(_blobs(box)) == 2, "the pass that noticed also deleted"

    assert ctl.sweep_blobs() == {"marked": 0, "deleted": 0, "waiting": 1}   # inside the grace period

    box.wall.advance(301)
    assert ctl.sweep_blobs() == {"marked": 0, "deleted": 1, "waiting": 0}
    assert _blobs(box) == [ctl.put_blob(MASK_B)], "the referenced blob went too, or the orphan stayed"
    assert ctl.blob(ctl.put_blob(MASK_B)) == MASK_B                        # and it still reads


def test_a_blob_written_between_the_two_passes_survives():
    """The race the design exists for. `put_blob` writes the object first and the
    row second; a sweep that decided in one breath would delete the bytes a row is
    about to name. It cannot: the digest was not on the list."""
    box = Box()
    ctl, _ = _det(box)
    ctl.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    ctl.update("7-linecross", {"mask": ctl.put_blob(MASK_A)})
    ctl.update("7-linecross", {"mask": ctl.put_blob(MASK_B)})
    ctl.sweep_blobs()                                              # marks A

    box.wall.advance(301)
    ctl.create({"name": "8-linecross", "cam": "8", "kind": "linecross"})
    fresh = ctl.put_blob(MASK_C)                                   # object written; the row not yet
    ctl.sweep_blobs()                                              # …and the sweep runs right here
    assert box.objects.get(f"det/blobs/{fresh}") == MASK_C, "the sweep ate a blob whose row was in flight"
    ctl.update("8-linecross", {"mask": fresh})                     # the row lands, late and intact
    assert ctl.blob(fresh) == MASK_C


def test_re_uploading_a_marked_blob_calls_the_whole_sweep_off():
    """The residual race, closed. The same bytes can be uploaded for a second unit
    while the first unit's copy is marked — and then the marked digest is exactly
    the one a row is about to name. `put_blob` takes it off the list, which makes
    the sweep's own CAS fail, and a sweep that loses that CAS deletes NOTHING."""
    box = Box()
    ctl, _ = _det(box)
    ctl.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    ctl.update("7-linecross", {"mask": ctl.put_blob(MASK_A)})
    ctl.update("7-linecross", {"mask": ctl.put_blob(MASK_B)})      # A orphaned
    ctl.sweep_blobs()
    assert json.loads(box.vars.get("det/sweep")[0]["digests"]) != []

    box.wall.advance(301)
    ctl.create({"name": "8-linecross", "cam": "8", "kind": "linecross"})
    again = ctl.put_blob(MASK_A)                                   # the marked digest, uploaded again
    assert json.loads(box.vars.get("det/sweep")[0]["digests"]) == [], "put_blob did not take it off the list"
    ctl.update("8-linecross", {"mask": again})

    ctl.sweep_blobs()
    assert box.objects.get(f"det/blobs/{again}") == MASK_A, "the sweep deleted a blob a row names"


def test_the_sweep_is_bounded_because_its_own_bookkeeping_is_a_row():
    """`det/sweep` is a Variable, and a Variable has the store's ceiling over it
    (Lesson 26). So the sweep collects at most `limit` per pass — the rule applies
    to the thing that was written under it."""
    box = Box()
    ctl, _ = _det(box)
    ctl.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    for i in range(10):
        ctl.update("7-linecross", {"mask": ctl.put_blob(bytes([i]) * 500)})
    assert len(_blobs(box)) == 10                                  # nine orphans and the current one

    assert ctl.sweep_blobs(limit=4)["marked"] == 4
    box.wall.advance(301)
    assert ctl.sweep_blobs(limit=4)["deleted"] == 4
    assert len(_blobs(box)) == 6
    # …and the rest goes in later passes, four at a time
    for _ in range(3):
        ctl.sweep_blobs(limit=4); box.wall.advance(301); ctl.sweep_blobs(limit=4)
    assert len(_blobs(box)) == 1, _blobs(box)


def test_a_subsystem_with_no_blob_field_is_not_swept_at_all():
    """The VMS has no `blob` field. A sweep there must not list, not decide and not
    write a row — nothing to collect is not the same as nothing collected."""
    from vms.config import SPEC
    box = Box()
    ctl = SpecController(SPEC, box.vars, box.objects, wall=box.wall)
    assert ctl.sweep_blobs() == {"marked": 0, "deleted": 0, "waiting": 0}
    assert box.vars.get("vms/sweep") == (None, 0), "a subsystem with nothing to sweep wrote bookkeeping"


def test_nothing_but_the_sweep_leans_on_objects_going_away():
    """The invariant Lesson 4 stated as "nothing deletes" is now narrower, and the
    narrower one has to be checked: the READERS still assume an object stays. A
    worker reads the heartbeat its previous instance left to measure failover, and
    `builds()` answers "what is running here" from heartbeats of any age — sweep
    those and both go quiet. So the sweep touches `blobs/` and nothing else."""
    box = Box()
    ctl, _ = _det(box)
    box.objects.put("det/heartbeats/d-1", b'{"worker": "d-1", "ts": 0, "status": []}')
    box.objects.put("det/snapshot/d-1", b'{"cluster": "c", "worker": "d-1", "ts": 0, "units": []}')
    ctl.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    ctl.put_blob(MASK_A)                                           # an orphan from the first breath

    ctl.sweep_blobs(); box.wall.advance(301); ctl.sweep_blobs()
    assert _blobs(box) == []
    assert box.objects.get("det/heartbeats/d-1") is not None, "the sweep took a heartbeat"
    assert box.objects.get("det/snapshot/d-1") is not None, "the sweep took a snapshot shard"


def test_the_backlog_is_on_metrics_and_costs_two_reads():
    """A gauge scraped every fifteen seconds must not walk every unit's row, so the
    backlog is reported as what IS there and what is marked — a prefix listing and
    one row — and not as `blobs_referenced()`, which is a full scan."""
    box = Box()
    ctl, con = _det(box)
    ctl.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    ctl.update("7-linecross", {"mask": ctl.put_blob(MASK_A)})
    ctl.update("7-linecross", {"mask": ctl.put_blob(MASK_B)})
    assert "det_blobs_total 2" in con.metrics_text()
    assert "det_blobs_marked 0" in con.metrics_text()

    ctl.sweep_blobs()
    assert "det_blobs_marked 1" in con.metrics_text()
    box.wall.advance(301); ctl.sweep_blobs()
    assert "det_blobs_total 1" in con.metrics_text() and "det_blobs_marked 0" in con.metrics_text()


def test_a_subsystem_without_blobs_has_no_blob_gauges():
    """The VMS has no `blob` field: a gauge that is always zero is noise on every
    screen it reaches."""
    from vms.config import SPEC
    from vms.controller import VmsController
    box = Box()
    text = SpecConsole(VmsController(box.vars, box.objects, wall=box.wall)).metrics_text()
    assert "vms_blobs_total" not in text and "vms_blobs_marked" not in text


def test_the_decision_is_cleared_before_anything_is_deleted():
    """The ordering argument, exercised where it actually lives: INSIDE one pass.

    The sweeper reads the list, checks, clears the row by CAS, and only then removes
    the bytes. If somebody re-uploads a marked blob after that read, the CAS fails —
    and because the clear comes FIRST, nothing has been deleted when it does. Delete
    first and the same interleaving destroys bytes a row is already naming.

    The interleaving is made deterministic by acting from inside `blobs_referenced`,
    which the sweeper calls after reading the row and before writing it."""
    box = Box()
    ctl, _ = _det(box)
    other, _ = _det(box)                                  # a second console, same store
    ctl.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    ctl.update("7-linecross", {"mask": ctl.put_blob(MASK_A)})
    ctl.update("7-linecross", {"mask": ctl.put_blob(MASK_B)})
    ctl.sweep_blobs()                                     # A is marked
    box.wall.advance(301)

    real = ctl.blobs_referenced
    def racing():
        ctl.blobs_referenced = real                       # once, in the middle of the pass
        # The real interleaving: the OBJECT is written and the row naming it is still in flight, so the
        # digest is genuinely unreferenced at the re-check — and the only thing between it and deletion
        # is that `put_blob` took it off the list, which the CAS is about to notice.
        other.put_blob(MASK_A)
        return real()
    ctl.blobs_referenced = racing

    try:
        ctl.sweep_blobs()
        raise AssertionError("the sweeper wrote over a decision something had contradicted")
    except Conflict:
        pass
    from w2cplatform.blobs import digest
    assert box.objects.get(f"det/blobs/{digest(MASK_A)}") == MASK_A, \
        "the CAS was lost AFTER the delete: the bytes of a row still in flight are gone"


def test_the_console_process_actually_runs_the_sweep():
    """The bug this catches is the one that was actually made: `sweep_blobs` was
    written, tested, and left UNCALLED in the Go port for a day — every test green,
    and not one blob collected on a running cluster.

    It reads the source, which is a weak kind of test and worth being honest about:
    it would not notice a loop that starts and immediately returns. What it does
    notice is the whole class of "we built it and forgot to plug it in", and that
    class is not hypothetical here."""
    import inspect
    from vms import __main__ as entry
    src = inspect.getsource(entry.console)
    assert "_sweep_loop" in src, "the console process does not start the blob sweep"
    # and every subsystem the console fronts is handed to it: the recorder has blobs too
    assert "det_ctl" in src and "rec_ctl" in src, "the sweep was started for some subsystems, not all"
    loop = inspect.getsource(entry._sweep_loop)
    assert "sweep_blobs()" in loop and "except Exception" in loop, \
        "the sweep runs without its own failure handling — Lesson 28, made twice"
