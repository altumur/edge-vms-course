"""Lesson 2 — shadow mode: the taxonomy, slow versus stuck, the one number,
and the report built from what real clusters publish."""
from domain.shadow import Shadow, WorkerReport, exit_criterion, reports_from
from tests.conftest import Clock, Running, make_domain


def rep(worker, cams, obs, epoch=1, cluster="north", revision=5):
    return WorkerReport(worker, cluster, "srv-1", [(str(c), epoch, revision, obs) for c in cams], 0.0)


def test_the_taxonomy():
    sh = Shadow(grace_seconds=60)
    placed = {"1": "north", "2": "north", "3": "north", "9": "north"}
    epochs = {("north", "9"): 2}                                                  # camera 9's current epoch is 2
    reports = [rep("w-0", [1, 2], 5), rep("w-1", [3, 4], 5),                    # 4 is unmanaged
               rep("w-2", [9], 5, epoch=1),                                     # stale epoch: a fenced writer still reporting
               rep("w-3", [3], 5)]                                              # conflict with w-1 over 3
    r = sh.compare(placed, epochs, reports, now=100.0)
    assert r.count("unmanaged") == 1 and r.count("conflict") == 1 and r.count("stale_epoch") == 1
    assert r.count("orphaned") == 1                                             # 9: its only claimant was fenced
    assert r.count("lagging") == 0 and r.count("stalled") == 0
    assert "unmanaged=1" in r.summary()
    r = sh.compare({"1": "south"}, {}, [rep("w-0", [1], 5)], now=100.0)        # placed in south, running in north
    assert r.count("conflict") == 1 and "placed in south, running in north" in r.findings[0].detail


def test_slow_versus_stuck():
    sh = Shadow(grace_seconds=60)
    placed = {"1": "north"}
    r = sh.compare(placed, {}, [rep("w-0", [1], 5, revision=7)], now=100.0)
    assert r.count("lagging") == 1 and r.findings[0].detail.startswith("behind by 2")     # distance, not "diverged"
    r = sh.compare(placed, {}, [rep("w-0", [1], 5, revision=7)], now=200.0)
    assert r.count("stalled") == 1 and "for 100s" in r.findings[0].detail                # past grace, no progress
    r = sh.compare(placed, {}, [rep("w-0", [1], 6, revision=7)], now=210.0)
    assert r.count("lagging") == 1                                                       # progress moved: slow again
    r = sh.compare(placed, {}, [rep("w-0", [1], 7, revision=7)], now=220.0)
    assert not r.findings


def test_the_report_from_what_clusters_publish():
    """Two real clusters; the domain placed 1 and 2 in north and nothing in
    south. The shadow reads heartbeats and vms/epoch/* — never a controller."""
    fed, links = make_domain({"north": (), "south": ()}, "north"); wall = Clock()
    n = Running(fed.clusters["north"], wall); n.create(1, 2)
    s = Running(fed.clusters["south"], wall); s.create(3)
    reports, epochs, down = reports_from(fed)
    assert down == [] and sorted(r.cluster for r in reports) == ["north", "south"] and epochs[("north", "1")] == 1
    placed = {"1": "north", "2": "north"}                                       # the domain's placements, by its refs
    r = Shadow().compare(placed, epochs, reports, now=wall())
    assert r.count("unmanaged") == 1 and r.findings[-1].where == "south/w-0" and r.findings[-1].camera == "3"   # south runs a camera the domain never placed
    links["south"].up = False
    reports, epochs, down = reports_from(fed)
    assert down == ["south"] and all(r.cluster == "north" for r in reports)       # a silent cluster is named, not counted as clean


def test_exit_criterion_is_written_down():
    sh = Shadow()
    dirty = sh.compare({"1": "north"}, {}, [rep("w-0", [1, 2], 1, revision=1)], now=0)
    ok, why = exit_criterion(dirty, consecutive_clean=10)
    assert not ok and "unmanaged=1" in why
    clean = sh.compare({"1": "north", "2": "north"}, {}, [rep("w-0", [1, 2], 1, revision=1)], now=0)
    assert exit_criterion(clean, 2) == (False, "2/3 consecutive clean reports")
    assert exit_criterion(clean, 3)[0]
