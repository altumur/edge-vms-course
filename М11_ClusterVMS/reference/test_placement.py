"""Lesson 5 — property tests. No cluster, no cameras, milliseconds."""
import random
from placement import Camera, recorder, Placer, check_invariants

VLANS = ["vlan:a", "vlan:b", "vlan:c"]


def world(seed, n_nodes=4, n_cams=200):
    r = random.Random(seed)
    nodes = {f"node-{i}": recorder(f"node-{i}", capacity=r.choice([48, 64, 80]),
                               labels=frozenset(r.sample(VLANS, r.randint(1, 3)))) for i in range(1, n_nodes + 1)}
    cams = {}
    for c in range(1, n_cams + 1):
        load = r.choice([1.0, 1.0, 1.0, 2.0, 8.0])      # mostly 720p, some 1080p, a few 4K
        labels = frozenset([r.choice(VLANS)]) if r.random() < 0.5 else frozenset()
        cams[c] = Camera(c, load, labels)
    return r, nodes, cams


def test_every_camera_on_exactly_one_eligible_node_or_refused():
    for seed in range(30):
        r, nodes, cams = world(seed)
        p = Placer(nodes)
        refused = [c for c in cams.values() if p.place(c, cams) is None]
        check_invariants(p, cams)
        assert len(p.placed) + len(refused) == len(cams)
        for c in refused:                                   # refused only when genuinely nowhere to go
            assert all(n.capacity - p.load_of(n.id, cams) < c.load for n in p.eligible(c))


def test_adding_a_node_moves_nothing():
    for seed in range(30):
        r, nodes, cams = world(seed)
        p = Placer(nodes)
        for c in cams.values():
            p.place(c, cams)
        before = dict(p.placed)
        p.add_node(recorder("node-9", capacity=80, labels=frozenset(VLANS)))
        assert p.placed == before                           # the rule
        # and the NEXT camera may use it
        p.place(Camera(9999, 1.0), cams | {9999: Camera(9999, 1.0)})
        check_invariants(p, cams | {9999: Camera(9999, 1.0)})


def test_the_tidy_rebalance_that_fails_first_time():
    """Someone adds a rebalance that re-places EVERYTHING from scratch
    'to be optimal'. It passes the invariants and fails the stability rule."""
    r, nodes, cams = world(7)
    p = Placer(nodes)
    for c in cams.values():
        p.place(c, cams)
    before = dict(p.placed)

    def tidy_rebalance(p):                                  # the mistake: "optimal" packing, big first
        p.placed.clear()
        for c in sorted(cams.values(), key=lambda c: -c.load):
            p.place(c, cams)
    tidy_rebalance(p)
    check_invariants(p, cams)                               # looks fine...
    moved = sum(1 for c in before if c in p.placed and p.placed[c].node != before[c].node)
    assert moved > 0, "the tidy version moves cameras for no reason"
    print(f"   tidy rebalance moved {moved} of {len(before)} cameras — each one a stop and a start")


def test_budgeted_rebalance_is_bounded_and_explainable():
    r, nodes, cams = world(3)
    p = Placer(nodes)
    for c in cams.values():
        p.place(c, cams)
    # skew it: retire a recorder so its cameras pile onto the others, then add a fresh empty one
    p.remove_node("node-2", cams)
    p.add_node(recorder("node-2", capacity=64, labels=frozenset(VLANS)))
    moves = p.rebalance(cams, budget=5)
    assert len(moves) <= 5
    check_invariants(p, cams)
    for cam, frm, to in moves:
        pl = p.placed[cam]
        assert pl.node == to and "rebalance" in pl.reason and pl.at > 0   # a row with a reason and a revision
    print(f"   budget 5: {len(moves)} moves, e.g. {moves[0] if moves else '-'}; reason: "
          f"'{p.placed[moves[0][0]].reason}'" if moves else "   no move needed")


def test_capacity_is_the_system_not_a_node():
    nodes = {"node-1": recorder("node-1", 2.0), "node-2": recorder("node-2", 2.0)}
    cams = {i: Camera(i, 1.0) for i in range(1, 6)}
    p = Placer(nodes)
    results = [p.place(c, cams) for c in cams.values()]
    assert results[:4] and results[4] is None               # "you cannot add camera 5"
    assert sorted(p.load_of(n, cams) for n in nodes) == [2.0, 2.0]


if __name__ == "__main__":
    import inspect, sys
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for f in fns:
        f(); print(f"{f.__name__} ... OK")
    print(f"\nall {len(fns)} pass")
