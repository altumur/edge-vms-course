"""An archive that stops answering — the network volume behind a recording, gone for a minute or an hour.

The recorder already has the right buffer: the pipeline writes into a LOCAL spool and never touches the
network, and every pass `promote_closed` moves what has closed into the archive. So an outage should cost
nothing but a queue: keep recording into the spool, and move the backlog across when the link returns.

These tests pin that, starting with the part that was not true: that the recorder keeps LIVING while its
archive is unreachable. A recorder that stops renewing its leases loses its epochs, and one that stops
heartbeating is reassigned by its controller — either way the recording ends, and it ends because of the
one component that was supposed to be optional for a while.
"""
import threading

from vms.archive import ArchiveResource
from vms.recworker import RecWorker
from tests.conftest import Box


class _Passes:
    """A `stop` for `run()` that lets it make exactly `n` passes, moving both clocks `poll` seconds each
    time — so the loop's own timers (lease, heartbeat) fire as they would in a minute of real running,
    with no thread and no sleeping. `run` asks only `is_set()` and `wait(poll)` of it."""

    def __init__(self, n: int, box: Box):
        self.n, self.box = n, box

    def is_set(self) -> bool:
        return self.n <= 0

    def wait(self, poll: float) -> None:
        self.n -= 1
        self.box.clock.advance(poll)
        self.box.wall.advance(poll)


def _recorder_over_an_unreachable_archive(box: Box) -> tuple[RecWorker, dict]:
    """A recorder with one closed segment waiting in its spool, whose archive refuses every promote the
    way a network volume refuses when the link is down — and counters on the two things a living
    recorder must keep doing."""
    r = RecWorker("r-1", box.vars, box.objects, archive=ArchiveResource(box.spool, box.archive, wall=box.wall),
                  clock=box.clock, wall=box.wall, server="srv-1")
    r.archive.closed_in_spool = lambda *a: ["rec/7/e1/20250910T100000Z.mkv"]

    def unreachable(*a, **k):
        raise OSError("network is unreachable")
    r.archive.promote = unreachable

    calls = {"heartbeat": 0, "lease": 0}
    heartbeat, lease = r.heartbeat_once, r.lease_pass

    def counted_heartbeat():
        calls["heartbeat"] += 1
        return heartbeat()

    def counted_lease():
        calls["lease"] += 1
        return lease()
    r.heartbeat_once, r.lease_pass = counted_heartbeat, counted_lease
    return r, calls


def test_an_unreachable_archive_does_not_stop_the_recorder():
    """A minute of the loop with the archive unreachable throughout.

    Leases are renewed every `(ttl − margin) / 3` ≈ 8 s and the heartbeat goes every 10 s, so a minute
    holds several of each. Both must keep happening: the recorder is healthy, its pipeline is writing
    into the spool, and only the place the spool drains into is away. If a failed promote takes the rest
    of the pass down with it, neither happens after the first pass — the leases lapse at 30 s, the
    controller calls the recorder dead at 45 s, and the recording that the spool could have carried
    through the outage is stopped by the outage instead."""
    box = Box()
    r, calls = _recorder_over_an_unreachable_archive(box)

    r.run(poll=2.0, stop=_Passes(30, box))                       # a minute, archive away the whole time

    assert calls["lease"] >= 5, \
        f"leases renewed {calls['lease']} times in a minute: a failed promote took the rest of the pass with it"
    assert calls["heartbeat"] >= 5, \
        f"heartbeat sent {calls['heartbeat']} times in a minute: the controller will call this recorder dead"

    # …and the outage is SAID, not just survived: a recorder quietly queueing into its spool looks, from
    # outside, exactly like one that is fine.
    hb = r.heartbeat_extra()
    assert hb["archive_error"] == "network is unreachable" and hb["archive_away_since"] > 0
    assert hb["volume_error"] == "", "an archive that went away is not one that will not open — it is not handed back"


def test_when_the_archive_answers_again_the_queue_drains_and_the_outage_is_over():
    """The other end of the outage. A segment that could not go across stays in the spool, first in line;
    when the archive answers, it goes, and `archive_error` clears — but only because a segment actually
    went. An empty spool would prove nothing about an archive that was away: nothing was asked of it."""
    box = Box()
    r, _ = _recorder_over_an_unreachable_archive(box)
    moved = []
    r.promote_closed()
    assert r.archive_error and r.archive_away_since
    away_since = r.archive_away_since

    box.wall.advance(600)
    r.promote_closed()                                            # still away: the start of the outage is kept
    assert r.archive_away_since == away_since

    r.archive.promote = lambda p, *a, **k: moved.append(p)        # the link is back
    assert r.promote_closed() == 1 and moved == ["rec/7/e1/20250910T100000Z.mkv"]
    assert r.archive_error == "" and r.archive_away_since == 0.0

    r.archive.closed_in_spool = lambda *a: []                     # nothing left to send…
    r.archive_error = "left over"
    r.promote_closed()
    assert r.archive_error == "left over", "an empty spool proved the archive was back — nothing was asked of it"


def test_no_failure_in_the_work_can_stop_the_recorder_living():
    """The structural half, and why fixing `promote_closed` alone was not enough.

    `pump_once` is where a recorder reaches across the network, and it does so from more than one place:
    promoting the spool, and backfill — which fetches a range from the device (`record_range`) and then
    promotes it — with `BACKFILL_BUDGET` defaulting to one range a pass. Each of those can fail during
    the very outage the spool is for. Catching them one call site at a time is a list that is complete
    only until the next call site.

    So the rule is about the loop, not about any one caller: STAYING ALIVE — renewing leases, sending the
    heartbeat — never shares a fate with DOING THE WORK. A pass that failed is logged and retried; the
    process that ran it is still the one holding its units, and saying so is not optional. The error
    here is deliberately not an `OSError`: this is the guarantee for whatever nobody anticipated."""
    box = Box()
    r, calls = _recorder_over_an_unreachable_archive(box)

    def something_nobody_anticipated(*a, **k):
        raise RuntimeError("backfill fell over")
    r.backfill_budget, r.backfill = 1, something_nobody_anticipated

    r.run(poll=2.0, stop=_Passes(30, box))

    assert calls["lease"] >= 5, f"leases renewed {calls['lease']} times: a failure in the work stopped the recorder living"
    assert calls["heartbeat"] >= 5, f"heartbeat sent {calls['heartbeat']} times: the controller will call it dead"


# -- the worse failure: a call that does not fail, it does not RETURN --------------------------------------
#
# A network mount that goes quiet does not raise. `rename` and `write` into it wait in the kernel, with no
# timeout to give them, for as long as the mount is gone. The two-`try` loop above cannot help: there is no
# exception to catch, only a call that never comes back — and while it has not, the loop is not renewing
# anything. So promotion runs in a thread of its own, and a pass waits on it for `PROMOTE_WAIT` and no more.

def _hung_archive(box: Box) -> tuple[RecWorker, dict, threading.Event]:
    """A recorder whose archive has stopped answering the worse way — `promote` blocks. `release` is the
    link coming back. The wait is bounded so that a failing test cannot hang the suite."""
    r, calls = _recorder_over_an_unreachable_archive(box)
    release = threading.Event()

    def hangs(*a, **k):
        release.wait(10)
    r.archive.promote = hangs
    r.PROMOTE_WAIT = 0.05                                          # how long a pass waits on a promotion
    r.PROMOTE_STUCK = 20.0                                         # how long before it is called stuck
    return r, calls, release


def _run_a_minute_in_the_background(r: RecWorker, box: Box) -> threading.Thread:
    t = threading.Thread(target=r.run, kwargs={"poll": 2.0, "stop": _Passes(30, box)}, daemon=True)
    t.start()
    t.join(timeout=3)                                              # a living loop runs its minute in far less
    return t


def test_an_archive_that_hangs_does_not_stop_the_recorder_living():
    """A promote that never returns, for the whole minute. A loop that waits on it is a loop that renews
    nothing: the same thirty seconds to losing its epochs as an archive that raises, with nothing in the
    log to say why, because nothing failed."""
    box = Box()
    r, calls, release = _hung_archive(box)
    t = _run_a_minute_in_the_background(r, box)
    seen = dict(calls)
    release.set(); t.join(timeout=10)

    assert seen["lease"] >= 5, f"leases renewed {seen['lease']} times while a promotion hung: the loop waited on it"
    assert seen["heartbeat"] >= 5, f"heartbeat sent {seen['heartbeat']} times while a promotion hung"


def test_a_promotion_that_does_not_return_is_reported_stuck():
    """A hung call cannot be killed — a thread waiting in the kernel on a dead mount waits for as long as
    the mount is dead. What CAN be done is to say so. Nothing raised, so there is no error to report
    unless the recorder notices the silence itself: a promotion running longer than `PROMOTE_STUCK` is
    reported, in the same field an outage that raises uses."""
    box = Box()
    r, _, release = _hung_archive(box)
    t = _run_a_minute_in_the_background(r, box)
    reported = r.heartbeat_extra()["archive_error"]
    release.set(); t.join(timeout=10)

    assert "has not returned" in reported, f"a promotion hung for a minute and the heartbeat said {reported!r}"


def test_backfill_waits_while_the_archive_is_not_taking_segments():
    """Backfill exists to bring MORE into the archive. While the archive is taking nothing — away, or a
    promotion still in flight — a fetched range would only pile into the spool behind the queue, and its
    own promote would be one more call waiting on the dead mount, on the loop's own thread. Same reasoning
    as `under_pressure`: an archive that cannot take what it has cannot be given more."""
    box = Box()
    r, _ = _recorder_over_an_unreachable_archive(box)
    tried = []
    r.backfill_budget = 1
    r.backfill = lambda *a, **k: tried.append(1) or []

    r.pump_once()
    r.pump_once()
    assert tried == [], "backfill fetched into a spool whose archive is taking nothing"
