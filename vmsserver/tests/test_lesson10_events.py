"""Lesson 10 — events: the database that is a cache. An event is an
observation, written by the worker holding a unit's epoch into that unit's
bucket on the resource (Lesson 3); the resource PROCESS keeps a database
over its own tree and serves it; the console holds none and asks. Three
subsystems' events land on one camera's timeline, fenced by their own
epochs; a restarted database rebuilds to the same answer from the files;
retention on the resource takes the rows with the file."""
import json
import os
import urllib.error
import urllib.request

from w2cplatform.eventdatabase import EventDatabase, MergedIndex
from w2cplatform.resource import resources_seen, serve as serve_resource
from w2cplatform.spec import SpecController
from vms.archive import ArchiveResource
from vms.config import DET_SPEC, LIVE_SPEC, SPEC
from vms.console import serve
from vms.controller import VmsController
from vms.detworker import DetWorker
from vms.resource import vms_resource, vms_routes
from vms.worker import FakeActuator, VmsWorker
from tests.conftest import Box


def call(base, method, path, body=None, headers=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def _console_over(box, db, per_minute: float = 0.0):
    """A console whose index is this one database — enough to ask it what an
    operator would be shown, without a second process."""
    from w2cplatform.console import SpecConsole
    ctl = VmsController(box.vars.as_writer("console", SPEC.acl_console()), box.objects, wall=box.wall)
    return SpecConsole(ctl, index=db, wall=box.wall, per_minute=per_minute)


def _resource_process(box):
    """What `python3 -m vms resource` does: the platform's Resource with the VMS registered,
    served over HTTP, heartbeating so the console can find it, its database rebuilt from the tree."""
    res = vms_resource(ArchiveResource(box.spool, box.archive, wall=box.wall), "srv-1", "", box.vars, box.objects, wall=box.wall)
    rsrv = serve_resource(res, "127.0.0.1", 0, extra=vms_routes(ArchiveResource(box.spool, box.archive, wall=box.wall)))
    res.url = f"http://127.0.0.1:{rsrv.server_address[1]}"
    res.heartbeat(); res.database.rebuild()
    return res, rsrv


def test_three_subsystems_events_reach_one_timeline_through_the_resource_process_and_the_console():
    box = Box()
    ctl = VmsController(box.vars.as_writer("vmscontroller", SPEC.acl_controller()), box.objects, wall=box.wall)
    con_vars = box.vars.as_writer("console", SPEC.acl_console() + LIVE_SPEC.acl_console() + DET_SPEC.acl_console())
    con = VmsController(con_vars, box.objects, wall=box.wall)
    det_ctl = SpecController(DET_SPEC, box.vars.as_writer("detcontroller", DET_SPEC.acl_controller()), box.objects, wall=box.wall)
    w = VmsWorker("w-1", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall, server="srv-1", archive_root=box.archive)
    w.heartbeat_once(); con.create_camera({"name": "gate", "source": "driverpack://file/gate.mp4"}); ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()
    res, rsrv = _resource_process(box)
    assert list(resources_seen(box.objects)) == ["srv-1"]                                  # the console finds the resource by its heartbeat
    srv = serve(con, ArchiveResource(box.spool, box.archive), port=0, wall=box.wall,
                mounts={"det": SpecController(DET_SPEC, con_vars, box.objects, wall=box.wall)})   # no database here: the default MergedIndex asks srv-1
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        gpu = DetWorker("d-1", box.vars.as_writer("detworker", ["det/epoch/*", "det/slots/*"]), box.objects, capacity=8,
                        clock=box.clock, wall=box.wall, server="srv-1", archive_root=box.archive, env={"LABELS": "gpu"})
        gpu.heartbeat_once()
        call(base, "POST", "/det/units", {"name": "1-linecross", "cam": "1", "kind": "linecross"}, {"Idempotency-Key": "k1"})
        det_ctl.ensure_placed(); gpu.reconcile_once()
        for _ in range(3):
            box.wall.advance(2); gpu.reconcile_once()                                    # one event, on the third pass
        # the operator marks a moment, and the camera goes silent: three subsystems' events on one resource
        call(base, "POST", "/marks", {"cam": 1, "note": "check this"}, {"Idempotency-Key": "m1", "X-User": "murat"})
        w.actuator.dead.append(1); w.pump_once()
        assert res.database.tail()["added"] == 3                                          # the resource's tail; the console was not told
        st, out = call(base, "GET", "/events?cam=1"); ev = out["events"]
        assert st == 200 and out["state"] == "live"
        assert [(e["subsystem"], e["kind"], e["server"], e["fenced"]) for e in ev] == [
            ("det", "linecross", "srv-1", False), ("console", "mark", "srv-1", False), ("vms", "silent", "srv-1", False)]
        assert ev[0]["unit"] == "1-linecross" and ev[0]["pass"] == 3 and ev[0]["epoch"] == 1 and ev[1]["user"] == "murat"
        # the page shows all of it: the events under the timeline, the live feed beside the picture, the Mark button
        page = urllib.request.urlopen(base + "/").read().decode()
        assert 'id="events"' in page and 'id="livefeed"' in page and "/marks" in page and "/servers" in page
        # the same answer under the mount: /det/events fences by det's epochs too
        assert call(base, "GET", "/det/events?cam=1&subsystem=det")[1]["events"][0]["kind"] == "linecross"
        # an open bucket keeps growing: the next event is picked up by the next tail, not left for a rebuild
        for _ in range(3):
            box.wall.advance(2); gpu.reconcile_once()
        assert res.database.tail()["added"] == 1 and len(call(base, "GET", "/events?cam=1&subsystem=det")[1]["events"]) == 2
        # another detector instance takes the unit's epoch: the first one's events are fenced, nobody else's
        box.vars.put("det/epoch/1-linecross", {"epoch": "2"})
        assert [(e["subsystem"], e["fenced"]) for e in call(base, "GET", "/events?cam=1")[1]["events"]] == [
            ("det", True), ("console", False), ("vms", False), ("det", True)]
        # the resource process stops: the console says so by name, and answers with what it has — nothing
        rsrv.shutdown(); rsrv.server_close()
        st, out = call(base, "GET", "/events?cam=1")
        assert st == 200 and out["events"] == [] and out["state"] == "live; srv-1 unreachable"
    finally:
        srv.shutdown(); srv.server_close()


def test_the_database_is_a_cache_and_retention_takes_the_rows_with_the_file():
    """No store between the buckets and the answer: a fresh database over the same
    tree gives the same rows; the resource's retention pass removes a bucket file
    and its rows go with it — the database is told, the console just asks."""
    from vms.archive import event_log
    box = Box(); t = box.wall() - 3 * 86400
    event_log(box.archive, 7, 1).append(t + 10, "motion", zone="gate")                  # three days old: past a 1-day policy
    event_log(box.archive, 7, 1).append(box.wall() - 100, "motion")                      # fresh
    res = vms_resource(ArchiveResource(box.spool, box.archive, wall=box.wall), "srv-1", "http://srv-1", box.vars, box.objects, wall=box.wall)
    res.heartbeat()
    assert res.database.rebuild() == {"added": 2, "segments": 2, "mirrored": []} and res.database.state == "live"
    again = EventDatabase(box.archive, "srv-1", wall=box.wall); again.rebuild()
    assert again.query(0, 1e12)["events"] == res.database.query(0, 1e12)["events"]          # a cache proves it by being rebuilt
    m = MergedIndex(box.objects, fetch=lambda url, p: res.database.query(float(p["from"]), float(p["to"]), int(p["cam"]) if "cam" in p else None), wall=box.wall)
    assert [e["t"] for e in m.query(0, 1e12, cam=7)["events"]] == [t + 10, box.wall() - 100]
    box.vars.put("vms/retention/7", {"days": "1"})                                        # the VMS's policy for its unit, as a row the platform reads
    assert res.retain() == 1                                                              # the file went — and the rows with it
    assert [e["t"] for e in m.query(0, 1e12, cam=7)["events"]] == [box.wall() - 100]
    assert box.vars.list("vms/events") == [] and box.objects.list("vms/events") == []     # nothing about events in any store


def test_a_torn_last_line_loses_the_line_not_the_bucket():
    """`append` writes and flushes without `fsync`, so a crash can leave the last line
    half-written. That is the accepted loss — and it must be the same SHAPE as the one
    for footage: the open thing, not the day. A bucket of ten minutes' observations is
    not thrown away because one record was damaged; the torn line is skipped and
    counted, and the database built over it holds everything that did land."""
    import w2cplatform.events as ev
    from w2cplatform.events import EventLog, read_bucket
    from tests.conftest import Box

    box = Box()
    log = EventLog(box.archive, "vms", "8123", 7, 600)
    for i in range(5):
        p = log.append(1000.0 + i, "motion", score=i)
    with open(p, "a") as f:                                   # the writer died mid-append
        f.write('{"t": 1005.0, "kind": "mot')

    before = ev.torn
    rows = read_bucket(p)
    assert [r["score"] for r in rows] == [0, 1, 2, 3, 4]      # every whole line survives
    assert ev.torn == before + 1                              # and the damage is counted, not silent

    db = EventDatabase(box.archive, "srv-1", ":memory:", lambda: 2000.0, 600)
    db.rebuild()
    assert len(db.query(0, 1e12, cam=None, kind=None, subsystem="vms", unit="8123",
                        current_epochs={("vms", "8123"): 7})["events"]) == 5


def _events(box, n, cam=7):
    """`n` observations a second apart in one bucket, numbered so a test can say WHICH ones came back."""
    from vms.archive import event_log
    log = event_log(box.archive, cam, 1)
    for i in range(n):
        log.append(1000.0 + i, "motion", n=i)
    db = EventDatabase(box.archive, "srv-1", wall=box.wall); db.rebuild()
    return db


def test_an_overflowing_window_keeps_the_newest_end_and_says_it_was_cut():
    """`LIMIT` with no direction is a question nobody asked: "a thousand of the five
    thousand" means nothing until somebody says WHICH thousand. The database answered
    `ORDER BY t LIMIT ?`, so the rows it dropped were the NEWEST — the end a timeline
    and a scenario are both looking at, and it dropped them without a word.

    Both halves are the fix. The default end is the newest, because nearly everything
    asked of an event log is a form of "what just happened"; and the answer says
    `truncated`, because a decision taken on part of a window must not look like one
    taken on all of it."""
    box = Box()
    db = _events(box, 10)

    rep = db.query(0, 1e12, limit=4)
    assert [e["n"] for e in rep["events"]] == [6, 7, 8, 9]        # the newest four…
    assert [e["t"] for e in rep["events"]] == sorted(e["t"] for e in rep["events"])   # …still ascending by time
    assert rep["truncated"] is True

    whole = db.query(0, 1e12, limit=10)                            # exactly `limit` rows is a WHOLE window:
    assert [e["n"] for e in whole["events"]] == list(range(10))    # the fact is read one row past the limit,
    assert whole["truncated"] is False                             # so "cut" is never a guess from a count


def test_which_end_survives_is_the_callers_and_an_unknown_end_is_refused():
    """The reader that wants the other end — paging forward through an archive from a
    cursor — knows that about itself and says so. It is a choice, so it is refused at the
    door like any other field: an unknown `keep` must not quietly mean the opposite end,
    because that failure is invisible and permanent."""
    box = Box()
    db = _events(box, 10)

    assert [e["n"] for e in db.query(0, 1e12, limit=4, keep="oldest")["events"]] == [0, 1, 2, 3]

    try:
        db.query(0, 1e12, keep="middle"); assert False, "an unknown end was accepted"
    except ValueError as e:
        assert "'newest' or 'oldest'" in str(e) and "middle" in str(e)


def test_the_merge_does_not_cut_the_newest_end_twice():
    """The console's index asks every resource for a window and then cuts the union to
    `limit` again. A limit with no direction therefore dropped the newest end TWICE —
    once per resource, once over the merge — and the second cut is the one no single
    resource could have warned about.

    `truncated` is the union's too: any resource that cut its answer makes the merged
    window partial, and the reader is told that, not which server did it."""
    from w2cplatform.resource import RESOURCES
    box = Box()
    rows = [{"subsystem": "vms", "unit": "7", "cam": 7, "epoch": 1, "t": 1000.0 + i, "kind": "motion",
             "server": f"srv-{i % 2 + 1}", "bucket": f"b-{i % 2 + 1}", "n": i} for i in range(8)]
    for s in ("srv-1", "srv-2"):
        box.objects.put(f"{RESOURCES}/{s}/heartbeat",
                        json.dumps({"server": s, "ts": box.wall(), "url": f"http://{s}"}).encode())

    def fetch(url, p):
        mine = sorted([e for e in rows if e["server"] == url.rsplit("/", 1)[1]], key=lambda e: e["t"])
        lim, keep = int(p["limit"]), p.get("keep", "newest")
        cut = len(mine) > lim
        return {"events": (mine[-lim:] if keep == "newest" else mine[:lim]) if cut else mine,
                "state": "live", "truncated": cut}

    m = MergedIndex(box.objects, fetch=fetch, wall=box.wall)
    rep = m.query(0, 1e12, limit=4)                                # four each, eight merged, four kept
    assert [e["n"] for e in rep["events"]] == [4, 5, 6, 7]         # the newest of the UNION, not of one server
    assert rep["truncated"] is True                                # cut by the merge, though neither resource cut

    assert [e["n"] for e in m.query(0, 1e12, limit=4, keep="oldest")["events"]] == [0, 1, 2, 3]
    assert m.query(0, 1e12, limit=8)["truncated"] is False         # room for both servers' windows


def test_the_operators_timeline_can_ask_for_its_own_window():
    """The sharpest form of the defect was the operator's: `/events` on the console took
    no `limit` at all, so a busy hour came back as its first thousand rows with nothing
    saying so. The timeline now sets its own window, gets the newest end of it, and is
    told when the hour did not fit."""
    box = Box()
    _events(box, 10)
    res, rsrv = _resource_process(box)
    con = VmsController(box.vars.as_writer("console", SPEC.acl_console()), box.objects, wall=box.wall)
    srv = serve(con, ArchiveResource(box.spool, box.archive), port=0, wall=box.wall)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        st, rep = call(base, "GET", "/events?from=0&to=1e12&limit=3")
        assert st == 200 and [e["n"] for e in rep["events"]] == [7, 8, 9] and rep["truncated"] is True
        st, rep = call(base, "GET", "/events?from=0&to=1e12&limit=3&keep=oldest")
        assert st == 200 and [e["n"] for e in rep["events"]] == [0, 1, 2]
        st, rep = call(base, "GET", "/events?from=0&to=1e12&keep=middle")   # refused at the door, not read as an end
        assert st == 400 and "middle" in rep["error"]
        st, rep = call(f"{res.url}", "GET", "/events?from=0&to=1e12&keep=middle")
        assert st == 400 and "middle" in rep["error"]               # the resource's door says the same thing
    finally:
        srv.shutdown(); rsrv.shutdown()


def test_a_suppression_rule_is_refused_at_load_when_it_would_drop_more_than_it_says():
    """Suppression drops observations, so its declaration is read strictly rather
    than leniently. Every refusal here is a typo that would otherwise leave a
    subsystem believing it had suppression, or having far more than it asked for —
    and both failures are invisible, because what they produce is a log that looks
    calm."""
    from w2cplatform.spec import SubsystemSpec
    base = {"name": "panel", "unit": {"rows": "panels", "fields": {"host": {"type": "string"}}}}

    spec = SubsystemSpec.from_dict({**base, "events": {"suppress": {"io.input": {"window": 30}}}})
    assert spec.suppress["io.input"].window == 30 and spec.suppress["io.input"].by is None   # every field: the safe default
    assert SubsystemSpec.from_dict(base).suppress == {}                                      # and nothing at all by default

    def refused(rule):
        try:
            SubsystemSpec.from_dict({**base, "events": {"suppress": {"io.input": rule}}})
            raise AssertionError(f"accepted {rule}")
        except ValueError as e:
            return str(e)

    assert "must be positive" in refused({"window": 0})          # the shape a typo takes, read as "no suppression"
    assert "must be positive" in refused({})                     # a rule with no window is a kind not listed here
    assert "would then be the same thing" in refused({"window": 30, "by": []})   # empty `by`: every line collapses into one
    assert "the summary line writes itself" in refused({"window": 30, "by": ["repeats"]})


def test_an_overflowing_window_drops_observations_before_alarms():
    """The class earns its existence here, and nowhere else it could be checked.

    A limit is a budget for a screenful. Spending it on the thousand statistics
    lines that crowded out the one contact that opened is exactly the failure the
    class exists to prevent — and it is the failure the previous fix could not
    reach: keeping the NEWEST end is right when every line is worth the same, and
    the whole point of a class is that they are not.

    Without this ordering `class` would be a word in a file: written, indexed,
    filterable, and changing nothing about what anybody is shown."""
    from w2cplatform.events import ALARM
    from vms.archive import event_log
    box = Box()
    log = event_log(box.archive, 7, 1)
    for i in range(20):
        log.append(1000.0 + i, "stats", n=i)                      # the noise, filling the window
    log.append(1001.5, "io.input", ALARM, port="1", value="open")  # one alarm, and an OLD one at that
    db = EventDatabase(box.archive, "srv-1", wall=box.wall); db.rebuild()

    rep = db.query(0, 1e12, limit=5)
    assert rep["truncated"] is True
    kinds = [e["kind"] for e in rep["events"]]
    assert "io.input" in kinds, "the newest five buried the alarm the window existed for"
    assert kinds.count("stats") == 4 and len(kinds) == 5           # the alarm took one seat, the newest noise the rest
    assert [e["t"] for e in rep["events"]] == sorted(e["t"] for e in rep["events"])   # still a timeline
    assert rep["events"][0]["class"] == ALARM                      # …and it is the oldest of the five

    assert all(e["class"] == "observation" for e in db.query(0, 1e12, kind="stats")["events"])
    only = db.query(0, 1e12, cls=ALARM)
    assert [e["kind"] for e in only["events"]] == ["io.input"] and only["truncated"] is False


def test_the_merge_keeps_an_alarm_a_busier_neighbour_would_have_crowded_out():
    """The merge cuts the union a second time, so the policy has to be carried both
    ways: an alarm that survived its own server's window is dropped when it meets a
    louder neighbour's, and no single resource could have warned about that."""
    from w2cplatform.events import ALARM
    from w2cplatform.resource import RESOURCES
    box = Box()
    rows = [{"subsystem": "vms", "unit": "7", "cam": 7, "epoch": 1, "t": 1000.0 + i, "kind": "stats",
             "server": "srv-1", "bucket": "b-1", "class": "observation", "n": i} for i in range(6)]
    rows += [{"subsystem": "vms", "unit": "8", "cam": 8, "epoch": 1, "t": 1000.5, "kind": "io.input",
              "server": "srv-2", "bucket": "b-2", "class": ALARM, "port": "1"}]
    for s in ("srv-1", "srv-2"):
        box.objects.put(f"{RESOURCES}/{s}/heartbeat",
                        json.dumps({"server": s, "ts": box.wall(), "url": f"http://{s}"}).encode())

    def fetch(url, p):
        mine = sorted([e for e in rows if e["server"] == url.rsplit("/", 1)[1]], key=lambda e: e["t"])
        lim = int(p["limit"])
        return {"events": mine[-lim:] if len(mine) > lim else mine, "state": "live", "truncated": len(mine) > lim}

    m = MergedIndex(box.objects, fetch=fetch, wall=box.wall)
    rep = m.query(0, 1e12, limit=3)
    assert rep["truncated"] is True
    assert [e["kind"] for e in rep["events"]].count("io.input") == 1, "the alarm was crowded out by a neighbour"
    assert len(rep["events"]) == 3


def test_a_traffic_class_is_a_declared_value_and_not_a_convention_on_kind():
    """The whole argument for making this a field: a convention refuses nothing.
    "Kinds beginning with io. are alarms" reads whatever is written, so the first
    `IO.input` typed where `io.input` was meant leaves the class in silence and
    stays out of it until somebody reads the file by hand.

    An unknown class is refused where the line is written, and there is exactly one
    spelling of it — a `class` field beside a `cls` argument would be two names for
    one thing, and they drift."""
    from w2cplatform.events import EventLog
    box = Box()
    log = EventLog(box.archive, "vms", "7", 1)

    try:
        log.append(1000.0, "io.input", "Alarm")
        raise AssertionError("an unknown class was written")
    except ValueError as e:
        assert "'Alarm'" in str(e) and "alarm, observation" in str(e)

    try:
        log.append(1000.0, "io.input", **{"class": "alarm"})
        raise AssertionError("a second spelling was accepted")
    except ValueError as e:
        assert "two spellings" in str(e)

    from w2cplatform.events import read_bucket
    p = log.append(1000.0, "silent")
    assert "class" not in read_bucket(p)[0], "an observation says nothing: it is nearly every line"


def test_past_the_norm_the_timeline_counts_instead_of_listing():
    """A norm nobody acts on is a comment. Written down and never consulted, it
    prevents nothing: the storm still turns every alarm into wallpaper until the
    operator stops reading, which is the failure the whole event path exists to
    avoid, arriving through the front door instead of through a lost write.

    So the number does something. Past it the screen stops showing lines and
    starts showing counts — `(subsystem, unit, kind, class)` with how many and
    between when and when, the same three numbers a suppressed window reports one
    layer down, and alarms in their own groups so they still come first."""
    from w2cplatform.events import ALARM
    from vms.archive import event_log
    box = Box(); t = box.wall() - 60
    log = event_log(box.archive, 7, 1)
    for i in range(90):
        log.append(t + i * 0.5, "stats", n=i)
    for i in range(3):
        log.append(t + 10 + i, "io.input", ALARM, port="1", value="open")
    db = EventDatabase(box.archive, "srv-1", wall=box.wall); db.rebuild()
    con = _console_over(box, db)

    quiet = con.timeline(db.query(0, t + 5), 0, t + 5)             # a handful over a long window
    assert quiet["aggregated"] is False and quiet["events"] and "groups" not in quiet

    busy = con.timeline(db.query(t, t + 60), t, t + 60)            # ninety-three in a minute, norm sixty
    assert busy["aggregated"] is True and busy["events"] == []
    assert busy["rate_per_minute"] > busy["per_minute"]
    assert [g["kind"] for g in busy["groups"]] == ["io.input", "stats"], "the alarm group is not first"
    assert busy["groups"][0]["count"] == 3 and busy["groups"][1]["count"] == 90
    assert busy["groups"][0]["until"] - busy["groups"][0]["since"] == 2.0


def test_the_norm_is_the_operators_and_a_process_still_gets_every_line():
    """The line worth naming: the index answers processes, and a process reads a
    thousand lines as easily as ten. The evaluator asks the same merge and must
    keep getting every one of them — a scenario that missed its event is the
    failure this path exists to prevent, and it would be a strange way to fail, by
    protecting a machine's attention.

    Only the reader who tires gets counts. So the aggregation lives on the console
    and not in the index, and the norm is a number rather than a constant: a
    control room with four screens and a guard with a phone are not one reader."""
    from vms.archive import event_log
    box = Box(); t = box.wall() - 60
    log = event_log(box.archive, 7, 1)
    for i in range(90):
        log.append(t + i * 0.5, "stats", n=i)
    db = EventDatabase(box.archive, "srv-1", wall=box.wall); db.rebuild()

    assert len(db.query(t, t + 60)["events"]) == 90                # the index never aggregates
    assert "aggregated" not in db.query(t, t + 60)

    patient = _console_over(box, db, per_minute=1000)
    assert patient.timeline(db.query(t, t + 60), t, t + 60)["aggregated"] is False


def test_an_alarm_is_written_down_and_an_observation_is_only_flushed():
    """The trade the course made for observations — flushed, not synced: survives
    the process dying, not the power going — was made on purpose, and it is the
    same shape as the accepted loss for footage. It is not the right trade for an
    alarm: a line that only reached the page cache is not written down, and losing
    it is losing the thing the system is for.

    Two things this test pins that "just add fsync" misses. `os.fsync` on macOS
    returns without the drive flushing its own cache, so the durable path has to
    ask for `F_FULLFSYNC` and fall back rather than assume. And a file is not
    durable until the DIRECTORY ENTRY naming it is — otherwise a power cut leaves
    the bytes with nothing pointing at them, which is the torn line one level up."""
    import w2cplatform.events as ev
    from w2cplatform.events import ALARM, EventLog
    box = Box()
    log = EventLog(box.archive, "vms", "7", 1)
    synced, dirs = [], []
    real_durably, real_dir = ev.durably, ev.durable_dir
    ev.durably = lambda f: synced.append(f.name)
    ev.durable_dir = lambda d: dirs.append(d)
    try:
        p = log.append(1000.0, "stats", n=1)                       # an observation pays nothing…
        assert synced == [] and dirs == []                         # …including for the file it just created
        log.append(1001.0, "io.input", ALARM, port="1")
        assert synced == [p] and dirs == [os.path.dirname(p)]      # the alarm pays for both, the entry included:
        log.append(1002.0, "io.input", ALARM, port="1")            # the unsynced observation left it in the cache
        assert synced == [p, p] and dirs == [os.path.dirname(p)]   # …and once per bucket is enough
    finally:
        ev.durably, ev.durable_dir = real_durably, real_dir

    from w2cplatform.events import read_bucket
    assert [r["kind"] for r in read_bucket(p)] == ["stats", "io.input", "io.input"]


def test_the_durable_write_reaches_the_medium_or_says_it_could_not():
    """`durably` is one line only if you do not look at it. Plain `fsync` is the
    call everybody reaches for and the one that, on this platform, returns before
    the drive has flushed anything — so the strongest available barrier is asked
    for first and the fallback is what makes the code portable, not what makes it
    correct."""
    import w2cplatform.events as ev
    box = Box()
    p = os.path.join(box.archive, "sync-probe")
    os.makedirs(box.archive, exist_ok=True)
    with open(p, "w") as f:
        f.write("x"); f.flush()
        ev.durably(f)                                              # whichever path, it must not raise
    assert open(p).read() == "x"

    class _NoFcntl:
        def fileno(self): raise ValueError("closed")
    try:
        ev.durably(_NoFcntl())                                     # a fallback that cannot work is not silent
        raise AssertionError("a file that cannot be synced was reported as synced")
    except ValueError:
        pass


def test_the_timeline_endpoint_hands_the_page_counts_and_says_why():
    """The shape the page depends on, checked through HTTP rather than on the
    method, because the bug this guards against was in the WIRING: a console that
    starts returning `events: []` to a page reading `d.events` blanks the operator's
    timeline exactly when it is busiest, which is worse than the flood it replaced.

    So the reply says three things at once — the counts, the rate, and the norm —
    and the page has something to draw and a sentence to show for why it changed
    shape."""
    from vms.archive import event_log
    box = Box(); t = box.wall() - 60
    log = event_log(box.archive, 7, 1)
    for i in range(90):
        log.append(t + i * 0.5, "stats", n=i)
    res, rsrv = _resource_process(box)
    con = VmsController(box.vars.as_writer("console", SPEC.acl_console()), box.objects, wall=box.wall)
    srv = serve(con, ArchiveResource(box.spool, box.archive), port=0, wall=box.wall)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        st, rep = call(base, "GET", f"/events?from={t}&to={t + 60}")
        assert st == 200 and rep["aggregated"] is True
        assert rep["events"] == [] and [g["count"] for g in rep["groups"]] == [90]
        assert rep["rate_per_minute"] > rep["per_minute"] == 60.0
        st, quiet = call(base, "GET", f"/events?from=0&to={t + 60}")     # the same rows over a long window
        assert st == 200 and quiet["aggregated"] is False and len(quiet["events"]) == 90
    finally:
        srv.shutdown(); rsrv.shutdown()


def test_the_consoles_records_outlive_what_they_refer_to():
    """The one rule that has to exist BEFORE the records it protects.

    An operator's record refers to events — a mark names a unit, and an
    acknowledgement, when there is one, names the alarm it answers. The
    reference is one-way, and the asymmetry decides everything: a record that
    outlives what it refers to is harmless clutter, while an event that outlives
    the record ABOUT it goes quietly back to looking unanswered.

    Swept on its own clock, the console's bucket would do exactly that — a year
    later, to the one class of line somebody is going to be asked about. So its
    days are a floor and not a setting: whatever anybody keeps longest, the
    records keep too. It costs almost nothing, because an operator writes a
    handful of lines a day against a worker's thousands."""
    from w2cplatform.events import EventLog
    from w2cplatform.resource import console_floor
    box = Box(); old = box.wall() - 400 * 86400
    event_log = __import__("vms.archive", fromlist=["event_log"]).event_log
    event_log(box.archive, 7, 1).append(old, "motion")             # a camera that keeps two years…
    marks = EventLog(box.archive, "console", "c-1", 1)
    mark = marks.append(old, "mark", user="anna", note="checked")  # …and the record written the same day
    box.vars.put("vms/retention/7", {"days": "730"})

    res = vms_resource(ArchiveResource(box.spool, box.archive, wall=box.wall), "srv-1", "", box.vars, box.objects, wall=box.wall)
    assert res.retain() == 0                                       # neither is old enough yet
    assert os.path.exists(mark)

    box.vars.put("vms/retention/7", {"days": "500"})               # still longer than the console's year
    assert res.retain() == 0 and os.path.exists(mark), "the record was swept while the events it names stayed"

    assert console_floor({("vms", "7"): 500.0, ("console", "c-1"): 365.0}) == 500.0
    assert console_floor({("console", "c-1"): 365.0}) == 0.0       # nothing to outlive: its own days stand
