package cluster

// Lesson 5 — placement onto recorders: capacity measured, constraints as
// labels, the placement STORED in Variables (placement/<camera>), and one
// rule with a property test: adding a recorder moves nothing.
//
// Rebalance is explicit, budgeted, observable, interruptible, with a dead
// band — and each move is the one two-writer operation in the module, so
// the caller performs it with the epoch: stop on the old recorder, start on
// the new, never both at once without a fence.

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// Labels is a set: "vlan:cctv-b". A camera's labels must be a subset of
// its recorder's.
type Labels map[string]bool

func NewLabels(ls ...string) Labels {
	out := Labels{}
	for _, l := range ls {
		out[l] = true
	}
	return out
}

func (a Labels) SubsetOf(b Labels) bool {
	for l := range a {
		if !b[l] {
			return false
		}
	}
	return true
}

type PCamera struct {
	ID     int64
	Load   float64 // in units of the probe's per-pipeline increment I
	Labels Labels
}

type PNode struct {
	ID       string
	Capacity float64 // (budget − B) / I from М9's shard-memory-probe
	Labels   Labels
}

type Placement struct {
	recorder   string
	Reason string
	Rev    int64
}

type Move struct {
	Camera   int64
	From, To string
}

// Placer keeps placements in Variables under placement/<camera_id>: small,
// one writer (the placement service), exact lookups only.
type Placer struct {
	recorders  map[string]PNode
	Vars   Variables
	Placed map[int64]Placement
}

func NewPlacer(nodes map[string]PNode, v Variables) (*Placer, error) {
	p := &Placer{recorders: nodes, Vars: v, Placed: map[int64]Placement{}}
	paths, err := v.List("placement/")
	if err != nil {
		return nil, err
	}
	for _, path := range paths {
		items, _, err := v.Get(path)
		if err != nil {
			return nil, err
		}
		if items == nil || items["node"] == "" {
			continue
		}
		id, _ := strconv.ParseInt(strings.SplitN(path, "/", 2)[1], 10, 64)
		p.Placed[id] = Placement{items["node"], items["reason"], atoi(items["rev"])}
	}
	return p, nil
}

func (p *Placer) store(cam int64, pl Placement) error {
	path := fmt.Sprintf("placement/%d", cam)
	_, idx, err := p.Vars.Get(path)
	if err != nil {
		return err
	}
	if _, err := p.Vars.Put(path, Items{"node": pl.recorder, "reason": pl.Reason, "rev": itoa(pl.Rev)}, idx); err != nil {
		return err
	}
	p.Placed[cam] = pl
	return nil
}

func (p *Placer) rev() (int64, error) {
	items, _, err := p.Vars.Get("placement")
	if err != nil {
		return 0, err
	}
	rev := atoi(items["rev"]) + 1
	_, err = p.Vars.Put("placement", Items{"rev": itoa(rev)}, NoCAS)
	return rev, err
}

func (p *Placer) nodeIDs() []string {
	ids := make([]string, 0, len(p.recorders))
	for id := range p.recorders {
		ids = append(ids, id)
	}
	sort.Strings(ids) // deterministic where Python relied on insertion order
	return ids
}

func (p *Placer) LoadOf(node string, cameras map[int64]PCamera) float64 {
	var sum float64
	for c, pl := range p.Placed {
		if cam, ok := cameras[c]; ok && pl.recorder == node {
			sum += cam.Load
		}
	}
	return sum
}

func (p *Placer) Eligible(cam PCamera) []PNode {
	var out []PNode
	for _, id := range p.nodeIDs() {
		if cam.Labels.SubsetOf(p.recorders[id].Labels) {
			out = append(out, p.recorders[id])
		}
	}
	return out
}

// Place places ONE new camera; existing placements are never touched.
// nil means "the system is full" — never "recorder 3 is full".
func (p *Placer) Place(cam PCamera, cameras map[int64]PCamera) (*Placement, error) {
	if pl, ok := p.Placed[cam.ID]; ok {
		return &pl, nil
	}
	var best *PNode
	bestFree := 0.0
	eligible := p.Eligible(cam)
	for i := range eligible {
		n := eligible[i]
		free := n.Capacity - p.LoadOf(n.ID, cameras)
		if free >= cam.Load && free > bestFree {
			best, bestFree = &n, free
		}
	}
	if best == nil {
		return nil, nil
	}
	rev, err := p.rev()
	if err != nil {
		return nil, err
	}
	pl := Placement{best.ID, fmt.Sprintf("most free capacity (%.1f) among %d eligible", bestFree, len(eligible)), rev}
	return &pl, p.store(cam.ID, pl)
}

func (p *Placer) Remove(cam int64) error {
	if _, ok := p.Placed[cam]; !ok {
		return nil
	}
	rev, err := p.rev()
	if err != nil {
		return err
	}
	path := fmt.Sprintf("placement/%d", cam)
	_, idx, err := p.Vars.Get(path)
	if err != nil {
		return err
	}
	if _, err := p.Vars.Put(path, Items{"node": "", "reason": "removed", "rev": itoa(rev)}, idx); err != nil {
		return err
	}
	delete(p.Placed, cam)
	return nil
}

// AddNode: nothing moves. That is the rule.
func (p *Placer) AddNode(n PNode) { p.recorders[n.ID] = n }

// RetireNode is a recorder retired ON PURPOSE (a failover moves the recorder, not
// its cameras). Returns the cameras that found no home.
func (p *Placer) RetireNode(node string, cameras map[int64]PCamera) ([]int64, error) {
	delete(p.recorders, node)
	var orphans []int64
	for c, pl := range p.Placed {
		if pl.recorder == node {
			orphans = append(orphans, c)
		}
	}
	sort.Slice(orphans, func(i, j int) bool { return orphans[i] < orphans[j] })
	var homeless []int64
	for _, c := range orphans {
		delete(p.Placed, c)
		pl, err := p.Place(cameras[c], cameras)
		if err != nil {
			return nil, err
		}
		if pl == nil {
			homeless = append(homeless, c)
		}
	}
	return homeless, nil
}

// Rebalance moves at most budget cameras from the fullest recorder to the
// emptiest, stopping inside the dead band. Every move carries a reason and
// a revision.
func (p *Placer) Rebalance(cameras map[int64]PCamera, budget int, deadBand float64) ([]Move, error) {
	var moves []Move
	for i := 0; i < budget; i++ {
		ids := p.nodeIDs()
		if len(ids) == 0 {
			break
		}
		loads := map[string]float64{}
		hi, lo := ids[0], ids[0]
		for _, id := range ids {
			loads[id] = p.LoadOf(id, cameras) / p.recorders[id].Capacity
			if loads[id] > loads[hi] {
				hi = id
			}
			if loads[id] < loads[lo] {
				lo = id
			}
		}
		if loads[hi]-loads[lo] < deadBand {
			break
		}
		var candidate int64 = -1
		for c, pl := range p.Placed {
			cam, ok := cameras[c]
			if !ok || pl.recorder != hi || !cam.Labels.SubsetOf(p.recorders[lo].Labels) {
				continue
			}
			if candidate < 0 || cam.Load < cameras[candidate].Load || (cam.Load == cameras[candidate].Load && c < candidate) {
				candidate = c
			}
		}
		if candidate < 0 {
			break
		}
		if p.LoadOf(lo, cameras)+cameras[candidate].Load > p.recorders[lo].Capacity {
			break
		}
		rev, err := p.rev()
		if err != nil {
			return moves, err
		}
		if err := p.store(candidate, Placement{lo, fmt.Sprintf("rebalance from %s (spread %.0f%%)", hi, (loads[hi]-loads[lo])*100), rev}); err != nil {
			return moves, err
		}
		moves = append(moves, Move{candidate, hi, lo})
	}
	return moves, nil
}

// CheckInvariants is the property test's oracle.
func CheckInvariants(p *Placer, cameras map[int64]PCamera) error {
	for c, pl := range p.Placed {
		n, ok := p.recorders[pl.recorder]
		if !ok {
			return fmt.Errorf("camera %d placed on a recorder that does not exist", c)
		}
		if !cameras[c].Labels.SubsetOf(n.Labels) {
			return fmt.Errorf("camera %d violates its constraint", c)
		}
	}
	for _, n := range p.recorders {
		if p.LoadOf(n.ID, cameras) > n.Capacity+1e-9 {
			return fmt.Errorf("%s over capacity", n.ID)
		}
	}
	return nil
}
