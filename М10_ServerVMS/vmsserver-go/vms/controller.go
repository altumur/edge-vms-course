package vms

// vmscontroller — the only writer of vms/*.
//
//	cameras       CRUD by CAS; revision bumps on every operator edit; refuses controller-owned fields
//	placement     which worker runs a camera — by capacity, stored with a reason, never derived;
//	              adding a worker moves nothing; rebalance only when asked, budgeted
//	assignment    vms/workers/<worker> — what each worker reads
//
// It holds nothing. Two instances are harmless: every write is
// read-modify-write by CAS, and the loser re-reads. It is never on the
// recovery path.

import (
	"errors"
	"fmt"
	"sort"
	"strconv"

	p "vmsserver/vmsplatform"
)

// Refused is the controller's answer to a client that asked for something it may not have.
type Refused struct{ Msg string }

func (r *Refused) Error() string { return r.Msg }

var ErrNoSuchCamera = errors.New("no such camera")

type Placement struct {
	Camera int
	Worker string
	Reason string
	At     float64
	Rev    int
}

type Move struct {
	Camera   int
	From, To string
}

type VmsController struct {
	*p.Controller
	Capacity int // the FALLBACK for a worker whose heartbeat carries no capacity
}

func NewVmsController(vars p.Variables, objects p.ObjectStore, capacity int, wall p.Clock) *VmsController {
	if capacity == 0 {
		capacity = 50
	}
	return &VmsController{p.NewController(VMS, vars, objects, wall), capacity}
}

// CapacityOf: what the worker said it can carry, from its last heartbeat.
func (c *VmsController) CapacityOf(worker string) int {
	if hb, ok := c.WorkersSeen(1e12)[worker]; ok {
		if _, has := hb.Extra["capacity"]; has {
			return hb.ExtraInt("capacity", c.Capacity)
		}
	}
	return c.Capacity
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

func (c *VmsController) refuse(fields map[string]any) error {
	var bad, unknown []string
	for k := range fields {
		if contains(ForbiddenFields, k) {
			bad = append(bad, k)
		} else if !contains(OperatorFields, k) {
			unknown = append(unknown, k)
		}
	}
	if len(bad) > 0 {
		sort.Strings(bad)
		return &Refused{fmt.Sprintf("a client may not set %v: placement is decided and stored by the controller with a reason; revision, epoch and phase are not the operator's", bad)}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		return &Refused{fmt.Sprintf("unknown field(s) %v", unknown)}
	}
	return nil
}

func (c *VmsController) nextID() (int, error) {
	items, err := c.Write(VMS.Config("next_id"), func(it p.Items) p.Items {
		return p.Items{"n": strconv.Itoa(atoiDef(it["n"], 0) + 1)}
	})
	if err != nil {
		return 0, err
	}
	return atoiDef(items["n"], 0), nil
}

func toInt(v any, def int) int {
	switch x := v.(type) {
	case nil:
		return def
	case bool:
		if x {
			return 1
		}
		return 0
	case string:
		return atoiDef(x, def)
	}
	return int(p.ToFloat(v))
}

func toBool(v any, def bool) bool {
	switch x := v.(type) {
	case bool:
		return x
	case string:
		return x == "true"
	case nil:
		return def
	}
	return p.ToFloat(v) != 0
}

func toLabels(v any) []string {
	out := []string{}
	switch x := v.(type) {
	case []string:
		out = append(out, x...)
	case []any:
		for _, l := range x {
			out = append(out, p.Str(l))
		}
	case string:
		for _, l := range splitComma(x) {
			out = append(out, l)
		}
	}
	return out
}

func splitComma(s string) []string {
	var out []string
	start := 0
	for i := 0; i <= len(s); i++ {
		if i == len(s) || s[i] == ',' {
			if i > start {
				out = append(out, s[start:i])
			}
			start = i + 1
		}
	}
	return out
}

func (c *VmsController) CreateCamera(fields map[string]any) (Camera, error) {
	if err := c.refuse(fields); err != nil {
		return Camera{}, err
	}
	src, _ := fields["source"].(string)
	if src == "" {
		return Camera{}, &Refused{"a camera needs a source (driverpack://file/<name> or driverpack://<vendor>/<host>)"}
	}
	cid, err := c.nextID()
	if err != nil {
		return Camera{}, err
	}
	name, _ := fields["name"].(string)
	if name == "" {
		name = "cam" + strconv.Itoa(cid)
	}
	r := Camera{ID: cid, Name: name, Source: src, Enabled: toBool(fields["enabled"], true),
		RetentionDays: toInt(fields["retention_days"], 30), EventsRetentionDays: toInt(fields["events_retention_days"], 365),
		Priority: toInt(fields["priority"], 100), Revision: 1, Labels: toLabels(fields["labels"]), Ref: p.Str(orEmpty(fields["ref"]))}
	if _, err := c.Vars.Put(VMS.Config("cameras", strconv.Itoa(cid)), ItemsOf(r), 0); err != nil {
		return Camera{}, err
	}
	return r, c.retention(cid, r.EventsRetentionDays)
}

func orEmpty(v any) any {
	if v == nil {
		return ""
	}
	return v
}

// retention: the platform retains buckets by <sub>/retention/<unit>; the
// VMS's policy for a camera's events is one more row it writes. days < 0 clears it.
func (c *VmsController) retention(cid, days int) error {
	key := VMS.Config("retention", strconv.Itoa(cid))
	if days < 0 {
		_, err := c.Write(key, func(it p.Items) p.Items {
			if len(it) == 0 {
				return nil
			}
			return p.Items{"days": "0"}
		})
		return err
	}
	_, err := c.Write(key, func(it p.Items) p.Items {
		if it["days"] == strconv.Itoa(days) {
			return nil
		}
		return p.Items{"days": strconv.Itoa(days)}
	})
	return err
}

func (c *VmsController) UpdateCamera(cid int, fields map[string]any) (Camera, error) {
	if err := c.refuse(fields); err != nil {
		return Camera{}, err
	}
	items, err := c.Write(VMS.Config("cameras", strconv.Itoa(cid)), func(it p.Items) p.Items {
		if len(it) == 0 || it["deleted"] == "true" {
			panic(&p.ErrRow{Msg: ErrNoSuchCamera.Error()})
		}
		r := Row(it)
		for k, v := range fields {
			switch k {
			case "name":
				r.Name = p.Str(v)
			case "source":
				r.Source = p.Str(v)
			case "enabled":
				r.Enabled = toBool(v, true)
			case "retention_days":
				r.RetentionDays = toInt(v, r.RetentionDays)
			case "events_retention_days":
				r.EventsRetentionDays = toInt(v, r.EventsRetentionDays)
			case "priority":
				r.Priority = toInt(v, r.Priority)
			case "labels":
				r.Labels = toLabels(v)
			case "ref":
				r.Ref = p.Str(v)
			}
		}
		r.Revision++ // the trigger from М9 Lesson 5, in the controller
		return ItemsOf(r)
	})
	var rowErr *p.ErrRow
	if errors.As(err, &rowErr) {
		return Camera{}, ErrNoSuchCamera
	}
	if err != nil {
		return Camera{}, err
	}
	r := Row(items)
	if _, ok := fields["events_retention_days"]; ok {
		err = c.retention(cid, r.EventsRetentionDays)
	}
	return r, err
}

func (c *VmsController) DeleteCamera(cid int) error {
	unit := strconv.Itoa(cid)
	if _, err := c.Write(VMS.Config("cameras", unit), func(it p.Items) p.Items {
		if len(it) == 0 {
			return nil
		}
		it["deleted"] = "true"
		return it
	}); err != nil {
		return err
	}
	c.retention(cid, -1) // a deleted camera's buckets go at the next pass
	if pl := c.Placement(cid); pl != nil {
		c.AssignRemove(pl.Worker, unit)
		c.Write(VMS.Config("placement", unit), func(it p.Items) p.Items {
			return p.Items{"worker": "", "reason": "deleted", "at": p.Str(c.Wall()), "rev": strconv.Itoa(atoiDef(it["rev"], 0) + 1)}
		})
	}
	return nil
}

func (c *VmsController) Camera(cid int) *Camera {
	it, _, _ := c.Vars.Get(VMS.Config("cameras", strconv.Itoa(cid)))
	if it == nil || it["deleted"] == "true" {
		return nil
	}
	r := Row(it)
	return &r
}

func (c *VmsController) Cameras() []Camera {
	out := []Camera{}
	paths, _ := c.Vars.List(VMS.Config("cameras") + "/")
	for _, pth := range paths {
		it, _, _ := c.Vars.Get(pth)
		if it != nil && it["deleted"] != "true" {
			out = append(out, Row(it))
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

func (c *VmsController) Placement(cid int) *Placement {
	it, _, _ := c.Vars.Get(VMS.Config("placement", strconv.Itoa(cid)))
	if it == nil || it["worker"] == "" {
		return nil
	}
	at, _ := strconv.ParseFloat(it["at"], 64)
	return &Placement{cid, it["worker"], it["reason"], at, atoiDef(it["rev"], 0)}
}

func (c *VmsController) Load(worker string) int { return len(c.Assignment(worker).Units) }

func (c *VmsController) pool(workers []string) []string {
	if workers == nil {
		for w := range c.WorkersSeen(45) {
			workers = append(workers, w)
		}
	}
	out := append([]string{}, workers...)
	sort.Strings(out)
	return out
}

// StorePlacement writes the row first (CAS decides who won), then the assignment.
func (c *VmsController) StorePlacement(pl Placement) (*Placement, error) {
	unit := strconv.Itoa(pl.Camera)
	written, err := c.Write(VMS.Config("placement", unit), func(it p.Items) p.Items {
		if it["worker"] != "" {
			return nil // the other instance placed it while we thought
		}
		return p.Items{"worker": pl.Worker, "reason": pl.Reason, "at": p.Str(pl.At), "rev": strconv.Itoa(atoiDef(it["rev"], 0) + 1)}
	})
	if err != nil {
		return nil, err
	}
	at, _ := strconv.ParseFloat(written["at"], 64)
	out := Placement{pl.Camera, written["worker"], written["reason"], at, atoiDef(written["rev"], 0)}
	_, err = c.AssignAdd(out.Worker, unit)
	return &out, err
}

// Place ONE camera on the worker with the most free capacity among those
// seen heartbeating (or given). An existing placement is returned
// untouched: adding a worker moves nothing. nil: "the system is full".
func (c *VmsController) Place(cid int, workers []string) (*Placement, error) {
	if have := c.Placement(cid); have != nil {
		return have, nil
	}
	best, free := "", 0
	pool := c.pool(workers)
	for _, w := range pool {
		if f := c.CapacityOf(w) - c.Load(w); f > free {
			best, free = w, f
		}
	}
	if best == "" {
		return nil, nil
	}
	return c.StorePlacement(Placement{cid, best, fmt.Sprintf("most free capacity (%d) among %d worker(s)", free, len(pool)), c.Wall(), 0})
}

// Placer is what EnsurePlaced calls per camera; a cluster controller overrides it.
type Placer interface {
	Place(cid int, workers []string) (*Placement, error)
}

func (c *VmsController) EnsurePlaced(workers []string) ([]Placement, error) {
	return EnsurePlacedWith(c, c, workers)
}

func EnsurePlacedWith(c *VmsController, pl Placer, workers []string) ([]Placement, error) {
	out := []Placement{}
	for _, cam := range c.Cameras() {
		p, err := pl.Place(cam.ID, workers)
		if err != nil {
			return out, err
		}
		if p != nil {
			out = append(out, *p)
		}
	}
	return out, nil
}

func (c *VmsController) Where(cid int) string {
	if pl := c.Placement(cid); pl != nil {
		return pl.Worker
	}
	return ""
}

// MoveTo is the one two-writer operation: the destination takes the next
// epoch when it starts; the source's lease fences on renewal and it stops.
func (c *VmsController) MoveTo(cid int, to, reason string) (Placement, error) {
	unit := strconv.Itoa(cid)
	for w, a := range c.Assignments() { // wherever it is listed, and not only where the row says
		if a.Has(unit) && w != to {
			c.AssignRemove(w, unit)
		}
	}
	at := c.Wall()
	written, err := c.Write(VMS.Config("placement", unit), func(it p.Items) p.Items {
		return p.Items{"worker": to, "reason": reason, "at": p.Str(at), "rev": strconv.Itoa(atoiDef(it["rev"], 0) + 1)}
	})
	if err != nil {
		return Placement{}, err
	}
	c.AssignAdd(to, unit)
	return Placement{cid, to, reason, at, atoiDef(written["rev"], 0)}, nil
}

// Redistribute is the controller's one unasked move: a slot that was
// RELEASED still lists cameras. Move them to the workers that are here. A
// slot that merely lapsed is not touched.
func (c *VmsController) Redistribute(workers []string) []Move {
	moves := []Move{}
	for _, gone := range c.ReleasedSlots() {
		var live []string
		for _, w := range c.pool(workers) {
			if w != gone {
				live = append(live, w)
			}
		}
		units := c.Assignment(gone).Units
		sort.Slice(units, func(i, j int) bool { return atoiDef(units[i], 0) < atoiDef(units[j], 0) })
		for _, unit := range units {
			cid, _ := strconv.Atoi(unit)
			best, bestFree := "", -1<<30
			for _, w := range live {
				if f := c.CapacityOf(w) - c.Load(w); f > bestFree {
					best, bestFree = w, f
				}
			}
			if best == "" || c.Load(best) >= c.CapacityOf(best) {
				break // the system is full; the camera waits, listed where it was
			}
			c.MoveTo(cid, best, fmt.Sprintf("slot %s released; most free capacity (%d)", gone, bestFree))
			moves = append(moves, Move{cid, gone, best})
		}
	}
	return moves
}

// Headroom is the cluster's number for the autoscaler: cameras the live
// workers could still take, from their heartbeats.
func (c *VmsController) Headroom() int {
	n := 0
	for _, hb := range c.WorkersSeen(45) {
		n += hb.ExtraInt("headroom", 0)
	}
	return n
}

func (c *VmsController) Rebalance(budget int, deadBand float64, workers []string) []Move {
	pool := c.pool(workers)
	moves := []Move{}
	for i := 0; i < budget; i++ {
		if len(pool) < 2 {
			break
		}
		loads := map[string]float64{}
		for _, w := range pool {
			loads[w] = float64(c.Load(w)) / float64(c.CapacityOf(w))
		}
		hi, lo := pool[0], pool[0]
		for _, w := range pool {
			if loads[w] > loads[hi] {
				hi = w
			}
			if loads[w] < loads[lo] {
				lo = w
			}
		}
		if loads[hi]-loads[lo] < deadBand {
			break
		}
		units := c.Assignment(hi).Units
		cands := make([]int, 0, len(units))
		for _, u := range units {
			cands = append(cands, atoiDef(u, 0))
		}
		sort.Ints(cands)
		if len(cands) == 0 || c.Load(lo)+1 > c.CapacityOf(lo) {
			break
		}
		cid := cands[0]
		c.MoveTo(cid, lo, fmt.Sprintf("rebalance from %s (spread %.0f%%)", hi, (loads[hi]-loads[lo])*100))
		moves = append(moves, Move{cid, hi, lo})
	}
	return moves
}

// ReadModel: the console's rows, from heartbeats.
func (c *VmsController) ReadModel(lostAfter float64) []map[string]any {
	now := c.Wall()
	rows := []map[string]any{}
	for w, hb := range c.WorkersSeen(1e12) {
		age := now - hb.Ts
		state := "live"
		if age > lostAfter {
			state = "stale"
		}
		for _, s := range hb.Status {
			row := map[string]any{}
			for k, v := range s {
				row[k] = v
			}
			row["worker"], row["server"], row["age"], row["worker_state"] = w, hb.ExtraString("server", "?"), roundTo(age, 1), state
			rows = append(rows, row)
		}
	}
	sort.Slice(rows, func(i, j int) bool { return p.ToFloat(rows[i]["id"]) < p.ToFloat(rows[j]["id"]) })
	return rows
}

func roundTo(x float64, places int) float64 {
	m := 1.0
	for i := 0; i < places; i++ {
		m *= 10
	}
	return float64(int64(x*m+0.5)) / m
}
