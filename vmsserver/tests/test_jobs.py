"""The reaper: a job's row follows the worker that finished it — and only that
worker. Plus the question this project has now got wrong twice: is it called?"""
import inspect

from w2cplatform.console import Heartbeat
from w2cplatform.spec import SpecController
from vms.config import DETJOB_SPEC
from vms.jobs import reap
from tests.conftest import Box


def _ctls(box):
    """Two controllers over one store, as the box really has them: the console's
    token writes the operator's rows, the controller's token writes placement.
    The reaper runs with the CONSOLE's, which is the whole question this file asks."""
    con = SpecController(DETJOB_SPEC, box.vars.as_writer("console", DETJOB_SPEC.acl_console()), box.objects, wall=box.wall)
    adm = SpecController(DETJOB_SPEC, box.vars.as_writer("detjobcontroller", DETJOB_SPEC.acl_controller()), box.objects, wall=box.wall)
    return con, adm


def _worker(box, worker, server, *status):
    box.objects.put(DETJOB_SPEC.sub.heartbeat_key(worker),
                    Heartbeat(worker, box.wall(), list(status),
                              {"server": server, "capacity": 2, "headroom": 2, "labels": "gpu"}).to_bytes())


def _job(ctl, name="7-lpr-1"):
    ctl.create({"name": name, "cam": "7", "rec": "7", "kind": "lpr", "from": 100.0, "to": 200.0})
    return name


def test_a_finished_job_is_finished_in_the_row_the_operator_created():
    box = Box(); ctl, adm = _ctls(box)
    _worker(box, "j-1", "srv-1")
    job = _job(ctl); adm.ensure_placed()
    assert ctl.unit(job)["state"] == "queued"

    _worker(box, "j-1", "srv-1", {"id": job, "phase": "done", "covered": 600.0, "events": 3})
    assert reap(ctl) == {"done": 1, "failed": 0}
    assert ctl.unit(job)["state"] == "done"


def test_a_job_still_running_is_left_alone():
    box = Box(); ctl, adm = _ctls(box)
    _worker(box, "j-1", "srv-1")
    job = _job(ctl); adm.ensure_placed()
    _worker(box, "j-1", "srv-1", {"id": job, "phase": "running", "done_through": 150.0})
    assert reap(ctl) == {"done": 0, "failed": 0} and ctl.unit(job)["state"] == "queued"


def test_a_failure_is_recorded_as_a_failure_and_not_as_done():
    box = Box(); ctl, adm = _ctls(box)
    _worker(box, "j-1", "srv-1")
    job = _job(ctl); adm.ensure_placed()
    _worker(box, "j-1", "srv-1", {"id": job, "phase": "failed", "why": "the model would not load"})
    assert reap(ctl) == {"done": 0, "failed": 1} and ctl.unit(job)["state"] == "failed"


def test_only_the_worker_the_job_is_placed_on_is_believed():
    """A heartbeat object outlives its worker. `j-9` finished this job yesterday,
    before it moved; taking its word now would end the scan that `j-1` is running
    right this second, from the beginning, and call it done."""
    box = Box(); ctl, adm = _ctls(box)
    _worker(box, "j-1", "srv-1")
    job = _job(ctl); adm.ensure_placed()
    assert adm.placement(job).worker == "j-1"

    _worker(box, "j-9", "srv-9", {"id": job, "phase": "done", "covered": 600.0})     # yesterday's slot, still in the store
    _worker(box, "j-1", "srv-1", {"id": job, "phase": "running", "done_through": 150.0})
    assert reap(ctl) == {"done": 0, "failed": 0} and ctl.unit(job)["state"] == "queued"


def test_the_row_is_moved_once_and_then_left_alone():
    """The pass runs every thirty seconds and the worker keeps saying `done`
    until something un-places it. A row that moves every pass is a revision that
    moves every pass, and every reader downstream re-reads it for nothing."""
    box = Box(); ctl, adm = _ctls(box)
    _worker(box, "j-1", "srv-1")
    job = _job(ctl); adm.ensure_placed()
    _worker(box, "j-1", "srv-1", {"id": job, "phase": "done", "covered": 600.0})
    reap(ctl)
    rev = ctl.unit(job)["revision"]
    assert reap(ctl) == {"done": 0, "failed": 0}
    assert ctl.unit(job)["revision"] == rev


def test_the_consoles_token_may_actually_write_that_row():
    """The reaper runs with the console's grant. If `detjob` were missing from it
    the pass would raise Forbidden every thirty seconds and no job would ever
    close — which is the kind of thing that is found in production, not here."""
    from vms.config import DET_SPEC, LIVE_SPEC, REC_SPEC, SPEC
    acl = SPEC.acl_console() + LIVE_SPEC.acl_console() + DET_SPEC.acl_console() + REC_SPEC.acl_console() + DETJOB_SPEC.acl_console()
    box = Box()
    ctl = SpecController(DETJOB_SPEC, box.vars.as_writer("console", acl), box.objects, wall=box.wall)
    adm = SpecController(DETJOB_SPEC, box.vars.as_writer("detjobcontroller", DETJOB_SPEC.acl_controller()), box.objects, wall=box.wall)
    _worker(box, "j-1", "srv-1")
    job = _job(ctl); adm.ensure_placed()
    _worker(box, "j-1", "srv-1", {"id": job, "phase": "done"})
    assert reap(ctl)["done"] == 1                                    # no Forbidden

    src = inspect.getsource(__import__("vms.__main__", fromlist=["console"]).console)
    assert "DETJOB_SPEC.acl_console()" in src, "the console process does not ask for the grant the reaper needs"


def test_the_console_process_actually_runs_the_reaper():
    """Twice in this project a pass was written, tested and never called. The
    source is weak evidence and this test says so — but it is the evidence that
    catches exactly that."""
    import vms.__main__ as m
    console = inspect.getsource(m.console)
    assert "_reap_loop" in console, "the console process does not start the reaper — no job will ever close"
    assert "job_ctl" in console and "detjob" in console
    assert "from .jobs import reap" in inspect.getsource(m._reap_loop)
