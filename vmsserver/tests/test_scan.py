"""Reading an interval of the archive: which segments cover it, which epoch owns
each stretch, and what a restarted worker still has to do."""
import json
import os

from vms.archive import Manifest, Segment
from vms.scan import Scan, ScanLog, covered, plan, remaining
from tests.conftest import Box

T = 1_757_500_000.0          # a round unix second to build minutes from


def m(n):                    # minute `n` of the hour this file measures in
    return T + n * 60


def seg(box, unit, epoch, a, b, size=1000):
    s = Segment(str(unit), epoch, m(a), m(b), f"rec/{unit}/e{epoch}/{int(m(a))}.mp4", size)
    Manifest(box.archive, unit).append(s)
    return s


def test_the_plan_is_the_segments_the_interval_touches():
    box = Box()
    seg(box, 7, 1, 0, 10); seg(box, 7, 1, 10, 20); seg(box, 7, 1, 20, 30)
    p = plan(box.archive, 7, m(10), m(20))
    assert [s.seg.path.split("/")[-1] for s in p] == [f"{int(m(10))}.mp4"]      # the one it touches, and no neighbour
    assert covered(p) == 600


def test_a_stretch_is_clipped_to_what_was_asked_and_the_rest_is_refused():
    """The operator asked from 10:05; the segment opened at 10:00. The file is read
    from its start — there is no other way in — so the model sees 10:00 too, and
    what it sees there is not part of the answer."""
    box = Box()
    s = seg(box, 7, 1, 0, 10)
    p = plan(box.archive, 7, m(5), m(8))
    assert len(p) == 1 and p[0].seg == s
    assert (p[0].t0, p[0].t1) == (m(5), m(8)) and p[0].seconds == 180
    assert not p[0].accepts(m(4)) and p[0].accepts(m(5)) and p[0].accepts(m(7)) and not p[0].accepts(m(8))


def test_two_epochs_over_the_same_minutes_the_later_one_owns_them():
    """A recorder was fenced with footage in flight: e1 wrote 0–20, e2 took over at
    10 and wrote 10–30. Minutes 0–10 exist only under e1 and are good footage;
    minutes 10–20 exist twice and belong to the writer that won."""
    box = Box()
    old = seg(box, 7, 1, 0, 20); new = seg(box, 7, 2, 10, 30)
    p = plan(box.archive, 7, m(0), m(30))
    assert [(s.seg.epoch, s.t0, s.t1) for s in p] == [(1, m(0), m(10)), (2, m(10), m(30))]
    assert covered(p) == 1800                                                   # thirty minutes, counted once
    assert sum(1 for s in p if s.seg == old) == 1 and sum(1 for s in p if s.seg == new) == 1


def test_a_gap_is_a_gap_and_the_scan_does_not_invent_it():
    """Nothing was recorded between 10 and 20. A finished scan that found nothing
    must be able to say which of the two nothings it was."""
    box = Box()
    seg(box, 7, 1, 0, 10); seg(box, 7, 1, 20, 30)
    p = plan(box.archive, 7, m(0), m(30))
    assert covered(p) == 1200 and len(p) == 2                                   # twenty of the thirty minutes asked for


def test_one_segment_can_come_back_in_two_pieces():
    """e2 owns the middle. The long segment keeps the ends and loses the middle —
    which is why the log is keyed by the stretch and not by the file."""
    box = Box()
    long = seg(box, 7, 1, 0, 30); seg(box, 7, 2, 10, 20)
    p = plan(box.archive, 7, m(0), m(30))
    assert [(s.seg.epoch, s.t0, s.t1) for s in p] == [(1, m(0), m(10)), (2, m(10), m(20)), (1, m(20), m(30))]
    pieces = [s for s in p if s.seg == long]
    assert len(pieces) == 2 and pieces[0].key() != pieces[1].key()


def test_adjacent_stretches_of_one_segment_are_one_stretch():
    """A zombie under the OLDER epoch wrote 10-20 while the survivor wrote 0-30.
    Its edges are boundaries all the same, and the survivor wins on both sides of
    both of them — so without merging the worker opens one file three times and
    reports it as three stretches."""
    box = Box()
    seg(box, 7, 2, 0, 30); seg(box, 7, 1, 10, 20)                               # the loser wrote inside the winner
    p = plan(box.archive, 7, m(0), m(30))
    assert len(p) == 1 and p[0].seg.epoch == 2 and (p[0].t0, p[0].t1) == (m(0), m(30))


def test_what_is_logged_is_not_done_again():
    box = Box()
    seg(box, 7, 1, 0, 10); seg(box, 7, 1, 10, 20); seg(box, 7, 1, 20, 30)
    p = plan(box.archive, 7, m(0), m(30))
    log = ScanLog(box.archive, "job-1")
    assert remaining(p, log) == p                                               # nothing logged: everything to do
    log.append(p[0], events=2, at=m(31)); log.append(p[1], events=0, at=m(32))
    assert remaining(p, log) == [p[2]] and log.events() == 2


def test_the_log_is_on_the_disk_not_in_the_process():
    """A worker restarting on this server resumes; that is the whole reason the
    progress is a file beside the footage and not a field in the row it may not write."""
    box = Box()
    seg(box, 7, 1, 0, 10); seg(box, 7, 1, 10, 20)
    p = plan(box.archive, 7, m(0), m(20))
    ScanLog(box.archive, "job-1").append(p[0], events=1, at=m(21))
    on_disk = os.path.join(box.archive, "detjob", "job-1", "manifest.jsonl")
    assert json.loads(open(on_disk).read().strip())["key"] == p[0].key()        # a file, readable by a process that never ran this scan
    assert remaining(p, ScanLog(box.archive, "job-1")) == [p[1]]


def test_progress_is_not_a_resume_point():
    """`done_through` is the far end of the furthest stretch recorded. Stretch 1
    failed and stretch 2 succeeded: resuming from `done_through` would skip the
    failure silently, which is why `remaining` is what the worker reads."""
    box = Box()
    seg(box, 7, 1, 0, 10); seg(box, 7, 1, 10, 20)
    p = plan(box.archive, 7, m(0), m(20))
    log = ScanLog(box.archive, "job-1")
    log.append(p[1], events=0, at=m(21))                                        # the second one only
    assert log.done_through() == m(20)                                          # says twenty minutes
    assert remaining(p, log) == [p[0]]                                          # the first ten are still to do


def test_a_scan_of_a_recording_that_was_never_made_is_empty_not_an_error():
    box = Box()
    assert plan(box.archive, 7, m(0), m(30)) == [] and covered([]) == 0.0
