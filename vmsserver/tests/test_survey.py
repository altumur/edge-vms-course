"""The survey: watching an archive we do not own, forever, and copying none of it.

The index says where to look, the door says how often, and the frontier is an edge
to keep up with rather than a plan to finish."""
import os

from w2cplatform.console import Heartbeat
from w2cplatform.events import read_bucket
from w2cplatform.spec import SpecController
from vms.config import SURVEY_SPEC
from vms.scan import Frontier
from vms.surveyworker import SurveyWorker, _Busy
from tests.conftest import Box

T = 1_757_500_000.0


def m(n):
    return T + n * 60


class Every:
    def __init__(self, row): self.row, self.looks = row, 0
    def observe(self, now):
        self.looks += 1
        return [(self.row["kind"], {"at": now})]
    def close(self): pass


def _holder(box, cam="7", newest=m(100), spans=((m(0), m(10)), (m(50), m(60))), oldest=None):
    """A VMS worker holding the camera: the summary, and WHERE to ask for the index."""
    box.objects.put("vms/heartbeats/w-1",
                    Heartbeat("w-1", box.wall(), [{"id": str(cam), "phase": "running",
                                                   "coverage": {"from": oldest if oldest is not None else spans[0][0],
                                                                "to": newest, "fragments": 9},
                                                   "index_url": "http://holder/recordings/" + str(cam),
                                                   "playback_url": "http://holder/playback/" + str(cam)}],
                              {"server": "srv-1", "capacity": 50, "headroom": 49}).to_bytes())
    return [(float(a), float(b)) for a, b in spans]


def _worker(box, spans, name="s-1", busy=False, **kw):
    reads = []

    def fetch(url, a, b):
        if busy:
            raise _Busy("the device's two sessions are in use")
        reads.append((a, b))
        return b"\0"

    def index(url, t0, t1):
        return [(max(a, t0), min(b, t1)) for a, b in spans if b > t0 and a < t1]

    w = SurveyWorker(name, box.vars.as_writer("surveyworker", SURVEY_SPEC.sub.acl_worker()), box.objects,
                     models={"lpr": Every}, clock=box.clock, wall=box.wall, server="srv-1",
                     archive_root=box.archive, env={"LABELS": "gpu"}, step=60.0, fetch=fetch, index=index, **kw)
    w.reads = reads
    return w


def _watch(box, name="7-lpr", start="now"):
    ctl = SpecController(SURVEY_SPEC, box.vars, box.objects, wall=box.wall)
    ctl.create({"name": name, "cam": "7", "kind": "lpr", "start": start})
    box.vars.put(SURVEY_SPEC.sub.assignment("s-1"), {"units": name, "rev": 1}, cas=0)
    return ctl


def test_it_watches_only_where_the_device_actually_recorded():
    """Between the device's first and last minute there is mostly nothing. Walking
    that as if it were continuous spends the whole budget on silence."""
    box = Box(); spans = _holder(box); _watch(box, start="earliest")
    w = _worker(box, spans)
    w.SECONDS_PER_PASS = m(100) - m(0)
    w.reconcile_once()
    assert w.reads == [(m(0), m(10)), (m(50), m(60))]                 # the two spans, and not the quiet hour between


def test_the_frontier_moves_over_the_whole_window_and_not_span_to_span():
    """A gap in the device's own recording is nothing to watch and nothing to come
    back for. A frontier left at the edge of the last span parks the survey in
    front of every quiet night for ever."""
    box = Box(); spans = _holder(box); _watch(box, start="earliest")
    w = _worker(box, spans); w.SECONDS_PER_PASS = 1800.0
    w.reconcile_once()
    assert Frontier(box.archive, "7-lpr").read() == m(30)             # the window, not m(10)


def test_a_new_watch_starts_now_unless_the_row_says_otherwise():
    """`earliest` means thirty days of backlog on the day somebody enables it —
    a decision the row makes, not a default this code picks."""
    box = Box(); spans = _holder(box); _watch(box)                    # start: now
    w = _worker(box, spans); w.reconcile_once()
    assert w.reads == [] and Frontier(box.archive, "7-lpr").read() is None
    assert w.status_by_unit["7-lpr"]["phase"] == "running"


def test_the_events_are_ours_and_the_video_is_not_copied():
    box = Box(); spans = _holder(box); _watch(box, start="earliest")
    w = _worker(box, spans); w.SECONDS_PER_PASS = 1800.0
    w.reconcile_once()
    root = os.path.join(box.archive, "survey", "7-lpr", f"e{w.epochs['7-lpr']}")
    lines = [l for f in sorted(os.listdir(root)) for l in read_bucket(os.path.join(root, f))]
    assert lines and all(l["cam"] == 7 and l["source"] == "device" and l["watch"] == "7-lpr" for l in lines)
    assert all(m(0) <= l["t"] < m(60) for l in lines)
    assert not os.path.exists(os.path.join(box.archive, "rec"))       # nothing was copied anywhere


def test_a_restarted_worker_does_not_watch_it_again():
    box = Box(); spans = _holder(box); _watch(box, start="earliest")
    w = _worker(box, spans); w.SECONDS_PER_PASS = 1800.0
    w.reconcile_once()
    at = Frontier(box.archive, "7-lpr").read()

    w2 = _worker(box, spans, name="s-1"); w2.SECONDS_PER_PASS = 1800.0
    w2.reconcile_once()
    assert w2.reads == [(m(50), m(60))]                               # only what is past the frontier
    assert Frontier(box.archive, "7-lpr").read() > at


def test_a_busy_device_is_a_wait_and_the_frontier_does_not_move():
    """Two sessions, and they belong to the operator watching this gap and to the
    recorder saving it. A survey is the one of the three that can wait."""
    box = Box(); spans = _holder(box); _watch(box, start="earliest")
    w = _worker(box, spans, busy=True); w.SECONDS_PER_PASS = 1800.0
    w.reconcile_once()
    st = w.status_by_unit["7-lpr"]
    assert st["phase"] == "waiting" and "session" in st["why"]
    assert Frontier(box.archive, "7-lpr").read() is None              # nothing was watched, nothing is claimed


def test_the_heartbeat_says_how_far_behind_it_is():
    """A survey that cannot keep up is not broken and not finished. It is behind,
    and the only way anyone finds out is if it says so."""
    box = Box(); spans = _holder(box, newest=m(100)); _watch(box, start="earliest")
    w = _worker(box, spans); w.SECONDS_PER_PASS = 600.0
    w.reconcile_once()
    st = w.status_by_unit["7-lpr"]
    assert st["watched_through"] == m(10) and st["lag"] == (m(100) - m(10))
    w.reconcile_once()
    assert w.status_by_unit["7-lpr"]["lag"] < st["lag"]               # it is catching up


def test_the_newest_minutes_are_left_alone():
    """They are being written right now; reading them gets a torn end."""
    box = Box(); spans = _holder(box, newest=m(10), spans=((m(0), m(10)),)); _watch(box, start="earliest")
    w = _worker(box, spans); w.SECONDS_PER_PASS = 1800.0
    w.reconcile_once()
    assert all(b <= m(10) - w.SETTLE for _, b in w.reads)


def test_nobody_holding_the_camera_is_a_wait_not_a_silence():
    box = Box(); _watch(box, start="earliest")                        # no holder heartbeat at all
    w = _worker(box, [])
    w.reconcile_once()
    st = w.status_by_unit["7-lpr"]
    assert st["phase"] == "waiting" and "holds" in st["why"]


def test_the_survey_carries_its_own_budget():
    from vms.config import DET_SPEC
    assert SURVEY_SPEC.sub.heartbeat_key("x") != DET_SPEC.sub.heartbeat_key("x")
    assert SURVEY_SPEC.capacity_fallback != DET_SPEC.capacity_fallback
    assert SURVEY_SPEC.retire_field == ""                             # it does not end
    assert SURVEY_SPEC.near == "vms" and SURVEY_SPEC.near_by == "cam"
    assert SURVEY_SPEC.home == ""


def test_the_box_actually_runs_a_survey():
    """Written, tested and never started is how this project has lost work twice."""
    import inspect
    import vms.__main__ as m
    assert "surveyworker" in inspect.getsource(m).split("__main__")[-1]
    assert "SURVEY_SPEC.acl_console()" in inspect.getsource(m.console), "the console cannot write a watch"
    assert '"survey": SpecController(SURVEY_SPEC' in inspect.getsource(m.console), "no /survey on the console"
    assert "survey_ctl" in inspect.getsource(m.console)


# -- keep: hits — watch everything, copy what a model liked ---------------------------------------
def test_moments_become_stretches():
    """An event is an instant and footage is an interval. `pre`/`post` make the clip
    watchable; `join` keeps a busy minute from becoming three hundred two-second
    files; the watched spans clip the padding so nobody asks for minutes the device
    never recorded."""
    from vms.scan import hit_spans
    w = [(0.0, 100.0)]
    assert hit_spans([10.0, 12.0, 80.0], w, 5, 5, 30) == [(5.0, 17.0), (75.0, 85.0)]   # two joined, one apart
    assert hit_spans([10.0, 80.0], w, 5, 5, 5) == [(5.0, 15.0), (75.0, 85.0)]
    assert hit_spans([98.0], w, 5, 5, 5) == [(93.0, 100.0)]                            # padding clipped to what we watched
    assert hit_spans([], w, 5, 5, 5) == []
    assert hit_spans([10.0], [(0.0, 8.0), (20.0, 30.0)], 5, 5, 5) == [(5.0, 8.0)]      # and to the gaps in it


def test_a_survey_that_keeps_reports_what_to_keep():
    box = Box(); spans = _holder(box, spans=((m(0), m(10)),), newest=m(100))
    ctl = _watch(box, start="earliest")
    ctl.update("7-lpr", {"keep": "hits", "pre": 60.0, "post": 60.0, "join": 120.0})
    w = _worker(box, spans); w.SECONDS_PER_PASS = 1800.0
    w.reconcile_once(); w.heartbeat_once()

    hb = Heartbeat.from_bytes(box.objects.get(SURVEY_SPEC.sub.heartbeat_key("s-1")))
    assert hb.extra["hits"], "the model fired and nobody was told to keep it"
    cams = {h.split("|")[0] for h in hb.extra["hits"].split(",")}
    assert cams == {"7"}


def test_a_survey_that_keeps_nothing_asks_for_nothing():
    """`keep: none` is the default: thirty days of somebody else's NVR is not
    something to start copying because a model was enabled."""
    box = Box(); spans = _holder(box, spans=((m(0), m(10)),), newest=m(100))
    _watch(box, start="earliest")
    w = _worker(box, spans); w.SECONDS_PER_PASS = 1800.0
    w.reconcile_once(); w.heartbeat_once()
    hb = Heartbeat.from_bytes(box.objects.get(SURVEY_SPEC.sub.heartbeat_key("s-1")))
    assert hb.extra["hits"] == "" and w.events_written > 0


def test_the_console_turns_a_kept_stretch_into_a_request():
    from vms.config import REC_SPEC
    from vms.jobs import keep_what_fired
    from w2cplatform.spec import SpecController
    box = Box()
    survey = SpecController(SURVEY_SPEC, box.vars.as_writer("console", SURVEY_SPEC.acl_console()), box.objects, wall=box.wall)
    rec = SpecController(REC_SPEC, box.vars.as_writer("console", REC_SPEC.acl_console()), box.objects, wall=box.wall)
    rec.create({"cam": "7", "enabled": False})              # a tree and a retention; no live recording
    box.objects.put(SURVEY_SPEC.sub.heartbeat_key("s-1"),
                    Heartbeat("s-1", box.wall(), [], {"server": "srv-1", "hits": "7|1000|1120"}).to_bytes())

    assert keep_what_fired(survey, rec) == 1
    it, _ = box.vars.get(REC_SPEC.sub.request_key("7-1000-1120"))
    assert it and it["unit"] == "7" and it["by"] == "survey/7"
    assert keep_what_fired(survey, rec) == 0                # one row, not a queue

    assert rec.unit("7")["enabled"] is False                # …and still nothing is recorded live
    assert rec.unit("7")["retention_days"] == 30            # one archive, one retention: no second lifetime


def test_a_camera_nothing_records_here_has_nowhere_to_keep_it():
    from vms.config import REC_SPEC
    from vms.jobs import keep_what_fired
    from w2cplatform.spec import SpecController
    box = Box()
    survey = SpecController(SURVEY_SPEC, box.vars.as_writer("console", SURVEY_SPEC.acl_console()), box.objects, wall=box.wall)
    rec = SpecController(REC_SPEC, box.vars.as_writer("console", REC_SPEC.acl_console()), box.objects, wall=box.wall)
    box.objects.put(SURVEY_SPEC.sub.heartbeat_key("s-1"),
                    Heartbeat("s-1", box.wall(), [], {"server": "srv-1", "hits": "9|1000|1120"}).to_bytes())
    assert keep_what_fired(survey, rec) == 0
    assert box.vars.list(REC_SPEC.sub.requests_prefix()) == []


def test_the_console_process_keeps_what_fired():
    import inspect
    import vms.__main__ as m
    assert "keep_what_fired(survey_ctl, rec_ctl)" in inspect.getsource(m._reap_loop), \
        "everything a model liked would stay on the device"
    assert "survey_ctl" in inspect.getsource(m.console)
