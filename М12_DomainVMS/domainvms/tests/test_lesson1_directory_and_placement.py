"""Lesson 1 — what a cluster cannot know: lookup across clusters from the
snapshots the clusters publish, incompleteness as a result, placement by
reachability, CAS against two placers, a dead cluster is not a trigger."""
import threading
from domain.federation import DomainDirectory
from domain.placement import CameraSite, ClusterPlacer, Refused
from tests.conftest import Clock, Running, make_domain, snapshot


def test_where_across_three_clusters_from_what_the_clusters_publish():
    """Each cluster's controller placed its cameras on its workers and published
    one snapshot; the domain read three objects and nothing else. The domain
    asks by the ref it gave the cluster — every cluster has its own id 1."""
    fed, links = make_domain({"north": ("vlan:a",), "south": ("vlan:b",), "cloud": ("vlan:c",)}, "north")
    wall = Clock()
    n = Running(fed.clusters["north"], wall, workers=(("w-0", "srv-1"), ("w-1", "srv-2"))); n.create(1, 2)
    s = Running(fed.clusters["south"], wall, workers=(("w-0", "srv-9"),)); s.create(7)
    c = Running(fed.clusters["cloud"], wall); c.create(50)
    d = DomainDirectory(fed, wall=wall)
    a = d.where(7)
    assert a.found and a.complete and (a.cluster, a.worker, a.server) == ("south", "w-0", "srv-9")
    assert d.where(50).cluster == "cloud" and d.where(1).cluster == "north" and d.where(2).worker in ("w-0", "w-1")
    assert fed.clusters["south"].snapshot()["cameras"][0]["id"] == 1                  # the cluster's id; the domain never asks by it
    holdings, down = d.holdings()
    assert down == [] and holdings["south"] == {"w-0": ["7"]} and holdings["cloud"] == {"w-0": ["50"]}
    assert fed.clusters["north"].objects.list("vms/snapshot") == ["vms/snapshot"]           # one object per cluster is what the domain reads
    assert d.ages() == {"cloud": 0.0, "north": 0.0, "south": 0.0}                     # the snapshot's age is the domain's RPO, shown


def test_where_is_answered_with_worker_and_server_and_stays_honest_about_ids():
    fed, _ = make_domain({"north": (), "south": ()}, "north")
    snapshot(fed.clusters["north"], {1: ("w-0", "srv-1"), 2: ("w-1", "srv-2")}, ts=1000.0)
    snapshot(fed.clusters["south"], {7: ("w-0", "srv-9")}, ts=1000.0)
    d = DomainDirectory(fed)
    a = d.where(7)
    assert (a.cluster, a.worker, a.server) == ("south", "w-0", "srv-9") and a.complete
    assert "camera 7 is on w-0 (srv-9) in south" == a.sentence()
    none = d.where(99)
    assert not none.found and none.complete and "2 clusters searched" in none.sentence()


def test_unreachable_cluster_makes_the_answer_incomplete_not_short():
    fed, links = make_domain({"north": ("vlan:a",), "south": ("vlan:b",)}, "north")
    snapshot(fed.clusters["north"], {1: ("w-0", "srv-1")}, ts=1000.0)
    snapshot(fed.clusters["south"], {7: ("w-0", "srv-9")}, ts=1000.0)
    links["south"].up = False
    a = DomainDirectory(fed).where(7)
    assert not a.found and not a.complete and a.unreachable == ["south"] and a.searched == ["north"]
    assert "not 'not anywhere'" in a.sentence()               # never a short list read as complete
    b = DomainDirectory(fed).where(1)
    assert b.found and not b.complete and "could not be asked" in b.sentence()


def test_two_clusters_claiming_a_camera_is_a_fault_not_a_tie():
    fed, _ = make_domain({"north": (), "south": ()}, "north")
    snapshot(fed.clusters["north"], {7: ("w-0", "srv-1")}, ts=0)
    snapshot(fed.clusters["south"], {7: ("w-0", "srv-9")}, ts=0)
    try:
        DomainDirectory(fed).where(7); raise AssertionError("must raise")
    except RuntimeError as e:
        assert "placement failure" in str(e)


def test_placement_is_by_reachability_then_headroom():
    fed, _ = make_domain({"north": ("vlan:a", "vlan:b"), "south": ("vlan:b",), "cloud": ("vlan:c",)}, "north")
    head = {"north": 10.0, "south": 40.0, "cloud": 99.0}      # what each cluster's console exports: vms_headroom
    p = ClusterPlacer(fed, headroom=lambda c: head[c], clock=lambda: 1234.0)
    a = p.place(CameraSite(1, "vlan:a"))
    assert a.cluster == "north" and "only cluster reaching vlan:a" in a.reason        # capacity elsewhere is irrelevant
    b = p.place(CameraSite(2, "vlan:b"))
    assert b.cluster == "south" and "most headroom" in b.reason                       # ties broken by headroom
    try:
        p.place(CameraSite(3, "vlan:z")); raise AssertionError("must refuse")
    except Refused as e:
        assert "no cluster in the domain reaches vlan:z" in str(e)                    # never "cluster X is full"
    stored, _ = fed.domain_cluster.vars.get("domain/placement/2")
    assert stored["cluster"] == "south" and stored["at"] == "1234.0" and stored["reason"]   # stored, with a reason and a time
    assert p.place(CameraSite(2, "vlan:b")).cluster == "south"                        # placing again changes nothing


def test_the_cluster_then_places_on_a_worker_and_the_domain_never_named_one():
    """Two levels, each deciding what it knows: the domain picked south by
    reachability; south's controller picked the worker by capacity and labels."""
    fed, _ = make_domain({"north": ("vlan:a",), "south": ("vlan:b",)}, "north")
    wall = Clock(); s = Running(fed.clusters["south"], wall, workers=(("w-0", "srv-9"), ("w-1", "srv-10")))
    p = ClusterPlacer(fed)
    pl = p.place(CameraSite(7, "vlan:b"))
    assert pl.cluster == "south"
    (cid,) = s.create(7, labels=["vlan:b"])                                           # the domain console forwards the create to south, with ref 7
    where = s.ctl.placement(cid)
    assert where.worker in ("w-0", "w-1") and "reaching vlan:b" in where.reason and "on srv-" in where.reason
    a = DomainDirectory(fed).where(7)
    assert a.cluster == "south" and a.worker == where.worker                          # the snapshot carried the cluster's decision up
    assert fed.domain_cluster.vars.list("domain/placement/") == ["domain/placement/7"]  # the domain stored its level, nothing about workers


def test_two_placers_racing_agree_by_cas():
    fed, _ = make_domain({"north": ("vlan:a",), "south": ("vlan:a",)}, "north")
    results = []
    def race(seed):
        head = {"north": 10.0 + seed, "south": 10.0 + (1 - seed)}      # each placer would pick a different cluster
        p = ClusterPlacer(fed, headroom=lambda c: head[c])
        for cam in range(1, 41):
            results.append((cam, p.place(CameraSite(cam, "vlan:a")).cluster))
    ts = [threading.Thread(target=race, args=(i,)) for i in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    by_cam = {}
    for cam, cl in results:
        by_cam.setdefault(cam, set()).add(cl)
    assert all(len(s) == 1 for s in by_cam.values())               # one camera, one cluster, whoever won
    assert len(by_cam) == 40


def test_a_dead_cluster_is_not_a_trigger():
    fed, links = make_domain({"north": ("vlan:a",), "south": ("vlan:b",)}, "north")
    p = ClusterPlacer(fed)
    assert p.place(CameraSite(7, "vlan:b")).cluster == "south"
    links["south"].up = False
    assert p.place(CameraSite(7, "vlan:b")).cluster == "south"     # already placed: untouched, not re-placed
    try:
        p.place(CameraSite(8, "vlan:b"), unreachable={"south"}); raise AssertionError("must refuse")
    except Refused as e:
        assert "not placing elsewhere" in str(e)                    # nothing else can see it
    try:
        p.rebalance_across_clusters(); raise AssertionError("must refuse")
    except NotImplementedError as e:
        assert "never crosses a cluster" in str(e)


def test_exactly_one_domain_cluster_is_designated():
    fed, _ = make_domain({"north": (), "south": ()}, "nowhere")
    try:
        fed.domain_cluster; raise AssertionError("must raise")
    except RuntimeError as e:
        assert "exactly one domain cluster" in str(e)
