package cluster

// Lesson 5 — the directory you already built, and placement with its property tests.

import (
	"fmt"
	"math/rand"
	"sort"
	"strings"
	"testing"
)

func publishNode(t *testing.T, v *FakeVariables, objs ObjectStore, node string, ids ...int64) {
	t.Helper()
	cams := make([]CameraRow, 0, len(ids))
	for _, id := range ids {
		cams = append(cams, Cam(id, 1))
	}
	if !NewPublisher(node, NewFakeClusterStore(cams...), v, objs, 0, (&fakeClock{}).now).PublishOnce() {
		t.Fatal("publish")
	}
}

func where(t *testing.T, d *Directory, cam int64) string {
	t.Helper()
	n, err := d.Where(cam)
	must(t, err)
	return n
}

func TestWhereIsCamera7(t *testing.T) {
	v, objs := world(t)
	clk := &fakeClock{1000}
	publishNode(t, v, objs, "node-1", 1, 2, 7)
	publishNode(t, v, objs, "node-2", 3, 4)
	publishNode(t, v, objs, "node-3", 5)
	d := NewDirectory(v, 5, clk.now)
	if where(t, d, 7) != "node-1" || where(t, d, 5) != "node-3" || where(t, d, 99) != "" {
		t.Fatal("where")
	}
	if h, _ := d.Holdings("node-2"); fmt.Sprint(h) != "[3 4]" {
		t.Fatal(h)
	}
	// a move: node-1 publishes without 7, node-2 with it. Cached until the ttl, then current.
	publishNode(t, v, objs, "node-1", 1, 2)
	publishNode(t, v, objs, "node-2", 3, 4, 7)
	if where(t, d, 7) != "node-1" { // cached
		t.Fatal("cache")
	}
	clk.advance(6)
	if where(t, d, 7) != "node-2" { // one raft: one answer, current
		t.Fatal("current")
	}
}

func TestTworecordersClaimingOneCameraIsAnErrorNotAGuess(t *testing.T) {
	v, objs := world(t)
	publishNode(t, v, objs, "node-1", 7)
	publishNode(t, v, objs, "node-2", 7)
	_, err := NewDirectory(v, 0, (&fakeClock{}).now).Where(7)
	if err == nil || !strings.Contains(err.Error(), "one-writer-per-key") {
		t.Fatalf("must raise: %v", err)
	}
}

var vlans = []string{"vlan:a", "vlan:b", "vlan:c"}

func pworld(seed int64) (map[string]PNode, map[int64]PCamera) {
	r := rand.New(rand.NewSource(seed))
	nodes := map[string]PNode{}
	for i := 1; i <= 4; i++ {
		id := fmt.Sprintf("node-%d", i)
		perm := r.Perm(3)[:1+r.Intn(3)]
		ls := Labels{}
		for _, p := range perm {
			ls[vlans[p]] = true
		}
		nodes[id] = PNode{id, []float64{48, 64, 80}[r.Intn(3)], ls}
	}
	cams := map[int64]PCamera{}
	for c := int64(1); c <= 120; c++ {
		ls := Labels{}
		if r.Float64() < 0.5 {
			ls[vlans[r.Intn(3)]] = true
		}
		cams[c] = PCamera{c, []float64{1, 1, 1, 2, 8}[r.Intn(5)], ls}
	}
	return nodes, cams
}

func placeAll(t *testing.T, p *Placer, cams map[int64]PCamera) (refused int) {
	ids := make([]int64, 0, len(cams))
	for c := range cams {
		ids = append(ids, c)
	}
	sortInt64s(ids)
	for _, c := range ids {
		pl, err := p.Place(cams[c], cams)
		must(t, err)
		if pl == nil {
			refused++
		}
	}
	return refused
}

func snapshot(p *Placer) string {
	var parts []string
	for c, pl := range p.Placed {
		parts = append(parts, fmt.Sprintf("%d:%s", c, pl.recorder))
	}
	sort.Strings(parts)
	return strings.Join(parts, " ")
}

func copyrecorders(n map[string]PNode) map[string]PNode {
	out := map[string]PNode{}
	for k, v := range n {
		out[k] = v
	}
	return out
}

func TestEveryCameraOnOneEligibleNodeOrRefusedAndPlacementIsStored(t *testing.T) {
	for seed := int64(0); seed < 20; seed++ {
		nodes, cams := pworld(seed)
		v := NewFakeVariables()
		p, err := NewPlacer(nodes, v)
		must(t, err)
		refused := placeAll(t, p, cams)
		must(t, CheckInvariants(p, cams))
		if len(p.Placed)+refused != len(cams) {
			t.Fatal("accounted")
		}
		p2, err := NewPlacer(copyrecorders(nodes), v) // a fresh process reads the SAME placement back
		must(t, err)
		if snapshot(p2) != snapshot(p) {
			t.Fatalf("seed %d: stored placement differs", seed)
		}
	}
}

func TestAddingANodeMovesNothing(t *testing.T) {
	for seed := int64(0); seed < 20; seed++ {
		nodes, cams := pworld(seed)
		p, _ := NewPlacer(nodes, NewFakeVariables())
		placeAll(t, p, cams)
		before := snapshot(p)
		p.AddNode(PNode{"node-9", 80, NewLabels(vlans...)})
		if snapshot(p) != before {
			t.Fatalf("seed %d: something moved", seed)
		}
	}
}

func TestTidyRebalanceFailsTheStabilityRule(t *testing.T) {
	nodes, cams := pworld(7)
	p, _ := NewPlacer(nodes, NewFakeVariables())
	placeAll(t, p, cams)
	before := map[int64]string{}
	for c, pl := range p.Placed {
		before[c] = pl.recorder
	}
	p.Placed = map[int64]Placement{} // "re-place everything optimally, big first"
	ids := make([]int64, 0, len(cams))
	for c := range cams {
		ids = append(ids, c)
	}
	sort.Slice(ids, func(i, j int) bool {
		if cams[ids[i]].Load != cams[ids[j]].Load {
			return cams[ids[i]].Load > cams[ids[j]].Load
		}
		return ids[i] < ids[j]
	})
	for _, c := range ids {
		p.Place(cams[c], cams)
	}
	must(t, CheckInvariants(p, cams)) // every invariant holds...
	moved := 0
	for c, n := range before {
		if pl, ok := p.Placed[c]; ok && pl.recorder != n {
			moved++
		}
	}
	if moved == 0 { // ...and cameras moved for no reason
		t.Fatal("expected moves")
	}
}

func TestBudgetedRebalanceHasAReasonAndARevision(t *testing.T) {
	nodes, cams := pworld(3)
	p, _ := NewPlacer(nodes, NewFakeVariables())
	placeAll(t, p, cams)
	_, err := p.RetireNode("node-2", cams)
	must(t, err)
	p.AddNode(PNode{"node-2", 64, NewLabels(vlans...)})
	moves, err := p.Rebalance(cams, 5, 0.10)
	must(t, err)
	if len(moves) == 0 || len(moves) > 5 {
		t.Fatalf("%d moves", len(moves))
	}
	must(t, CheckInvariants(p, cams))
	for _, m := range moves {
		pl := p.Placed[m.Camera]
		if pl.recorder != m.To || !strings.Contains(pl.Reason, "rebalance") || pl.Rev <= 0 {
			t.Fatalf("%+v", pl)
		}
	}
	if none, _ := p.Rebalance(cams, 0, 0.10); len(none) != 0 { // interruptible: no budget, no moves
		t.Fatal("budget 0")
	}
}

func TestCapacityIsTheSystemNotANode(t *testing.T) {
	p, _ := NewPlacer(map[string]PNode{"node-1": {"node-1", 2, nil}, "node-2": {"node-2", 2, nil}}, NewFakeVariables())
	cams := map[int64]PCamera{}
	for i := int64(1); i <= 5; i++ {
		cams[i] = PCamera{i, 1, nil}
	}
	for i := int64(1); i <= 5; i++ {
		pl, _ := p.Place(cams[i], cams)
		if (pl == nil) != (i == 5) {
			t.Fatalf("camera %d: %v", i, pl)
		}
	}
}
