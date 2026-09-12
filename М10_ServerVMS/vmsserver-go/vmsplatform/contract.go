package vmsplatform

// The subsystem contract — what the platform knows about any subsystem,
// and it is all of this:
//
//	a config prefix        <name>/*                      writable by the controller only
//	assignment rows        <name>/workers/<worker>       what each worker should run
//	a heartbeat object     <name>/<worker>/heartbeat     {worker, ts, status: [...]} — the worker's own report
//	an epoch prefix        <name>/epoch/<unit>           fencing tokens the workers take by CAS
//	an event log           <resource>/<name>/<unit>/e<epoch>/<start>Z.events.jsonl   (events.go)
//	a slot prefix          <name>/slots/<worker>         identity by claim: a worker's name is a slot it
//	                                                     holds by CAS and renews; a replacement process
//	                                                     takes the lapsed slot and inherits its assignment
//
// Who decides how many workers there are: not the controller. The scheduler
// runs `count` of them and an autoscaler moves `count` from a headroom
// metric the workers export. The platform's part is to give `count`
// interchangeable processes stable names — the slots.

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
)

type Subsystem struct{ Name string }

func (s Subsystem) Config(parts ...string) string {
	return strings.Join(append([]string{s.Name}, parts...), "/")
}
func (s Subsystem) Assignment(worker string) string   { return s.Name + "/workers/" + worker }
func (s Subsystem) HeartbeatKey(worker string) string { return s.Name + "/" + worker + "/heartbeat" }
func (s Subsystem) EpochKey(unit string) string       { return s.Name + "/epoch/" + unit }
func (s Subsystem) SlotKey(worker string) string      { return s.Name + "/slots/" + worker }
func (s Subsystem) ACLController() []string           { return []string{s.Name + "/*"} }
func (s Subsystem) ACLWorker() []string               { return []string{s.Name + "/epoch/*", s.Name + "/slots/*"} }

// Assignment: what a worker should run. Units are whatever the subsystem
// calls its units of work; the platform does not know.
type Assignment struct {
	Worker string
	Units  []string
	Rev    int
}

func (a Assignment) ToItems() Items {
	return Items{"units": strings.Join(a.Units, ","), "rev": strconv.Itoa(a.Rev)}
}

func AssignmentFromItems(worker string, items Items) Assignment {
	if items == nil {
		return Assignment{Worker: worker, Units: []string{}}
	}
	var units []string
	for _, u := range strings.Split(items["units"], ",") {
		if u != "" {
			units = append(units, u)
		}
	}
	if units == nil {
		units = []string{}
	}
	rev, _ := strconv.Atoi(items["rev"])
	return Assignment{worker, units, rev}
}

func (a Assignment) Has(unit string) bool {
	for _, u := range a.Units {
		if u == unit {
			return true
		}
	}
	return false
}

// Heartbeat is the worker's own report: an object, never a Variable.
type Heartbeat struct {
	Worker string
	Ts     float64
	Status []map[string]any
	Extra  map[string]any
}

func (h Heartbeat) ToBytes() []byte {
	m := map[string]any{"worker": h.Worker, "ts": h.Ts, "status": h.Status}
	if h.Status == nil {
		m["status"] = []map[string]any{}
	}
	for k, v := range h.Extra {
		m[k] = v
	}
	b, _ := json.Marshal(m)
	return b
}

func HeartbeatFromBytes(raw []byte) (Heartbeat, error) {
	var d map[string]any
	if err := json.Unmarshal(raw, &d); err != nil {
		return Heartbeat{}, err
	}
	h := Heartbeat{Extra: map[string]any{}}
	h.Worker, _ = d["worker"].(string)
	h.Ts = ToFloat(d["ts"])
	if st, ok := d["status"].([]any); ok {
		for _, s := range st {
			if m, ok := s.(map[string]any); ok {
				h.Status = append(h.Status, m)
			}
		}
	}
	for k, v := range d {
		if k != "worker" && k != "ts" && k != "status" {
			h.Extra[k] = v
		}
	}
	return h, nil
}

// ExtraInt reads an integer out of the heartbeat's extra fields (JSON numbers arrive as float64).
func (h Heartbeat) ExtraInt(key string, def int) int {
	v, ok := h.Extra[key]
	if !ok {
		return def
	}
	return int(ToFloat(v))
}

func (h Heartbeat) ExtraString(key, def string) string {
	if v, ok := h.Extra[key]; ok {
		return Str(v)
	}
	return def
}

func ToFloat(v any) float64 {
	switch x := v.(type) {
	case float64:
		return x
	case int:
		return float64(x)
	case int64:
		return float64(x)
	case string:
		f, _ := strconv.ParseFloat(x, 64)
		return f
	case json.Number:
		f, _ := x.Float64()
		return f
	}
	return 0
}

// Slot is a worker's name, as a row: who holds it, until when (wall clock),
// and whether the last holder let go of it on purpose.
type Slot struct {
	Name     string
	Holder   string
	Until    float64
	Released bool
	Gen      int
}

func (s Slot) ToItems() Items {
	return Items{"holder": s.Holder, "until": Str(s.Until), "released": Str(s.Released), "gen": strconv.Itoa(s.Gen)}
}

func SlotFromItems(name string, items Items) Slot {
	if items == nil {
		return Slot{Name: name, Released: true}
	}
	until, _ := strconv.ParseFloat(items["until"], 64)
	gen, _ := strconv.Atoi(items["gen"])
	return Slot{name, items["holder"], until, items["released"] == "true", gen}
}

func (s Slot) Lapsed(now float64) bool    { return !s.Released && s.Holder != "" && now > s.Until }
func (s Slot) Claimable(now float64) bool { return s.Released || s.Holder == "" || now > s.Until }

func SlotNumber(name string) int {
	i := strings.LastIndex(name, "-")
	n, err := strconv.Atoi(name[i+1:])
	if err != nil {
		return 0
	}
	return n
}

// Mutate is a read-modify-write step: given the current items (never nil),
// return the new ones, or nil to leave the row as it is.
type Mutate func(items Items) Items

// ErrRow is returned by a Mutate that found the row in a state it refuses
// (a deleted row); it carries through Write unchanged.
type ErrRow struct{ Msg string }

func (e *ErrRow) Error() string { return e.Msg }

// Controller is the only writer of <name>/*. Holds nothing: every method
// reads the store, decides, and writes by CAS. Two instances are harmless.
type Controller struct {
	Sub     Subsystem
	Vars    Variables
	Objects ObjectStore
	Wall    Clock
}

func NewController(sub Subsystem, vars Variables, objects ObjectStore, wall Clock) *Controller {
	if wall == nil {
		wall = Wall()
	}
	return &Controller{sub, vars, objects, wall}
}

// Write is read-modify-write by CAS. A conflict means another instance
// wrote; re-read and go again. A Mutate that returns nil leaves the row and
// returns what it holds. A Mutate may panic with *ErrRow to refuse.
func (c *Controller) Write(path string, mutate Mutate) (out Items, err error) {
	defer func() {
		if r := recover(); r != nil {
			if e, ok := r.(*ErrRow); ok {
				out, err = nil, e
				return
			}
			panic(r)
		}
	}()
	for i := 0; i < 10; i++ {
		items, idx, err := c.Vars.Get(path)
		if err != nil {
			return nil, err
		}
		cur := Items{}
		for k, v := range items {
			cur[k] = v
		}
		newItems := mutate(cur)
		if newItems == nil {
			return cur, nil
		}
		if _, err := c.Vars.Put(path, newItems, idx); errors.Is(err, ErrConflict) {
			continue
		} else if err != nil {
			return nil, err
		}
		return newItems, nil
	}
	return nil, fmt.Errorf("%s: 10 conflicts", path)
}

// WorkersSeen: which workers exist — those that heartbeat recently. Never a
// list the controller keeps; a fact it reads.
func (c *Controller) WorkersSeen(maxAge float64) map[string]Heartbeat {
	out := map[string]Heartbeat{}
	now := c.Wall()
	keys, _ := c.Objects.List(c.Sub.Name + "/")
	for _, key := range keys {
		if !strings.HasSuffix(key, "/heartbeat") {
			continue
		}
		raw, _ := c.Objects.Get(key)
		if len(raw) == 0 {
			continue
		}
		hb, err := HeartbeatFromBytes(raw)
		if err == nil && now-hb.Ts <= maxAge {
			out[hb.Worker] = hb
		}
	}
	return out
}

func (c *Controller) Assignment(worker string) Assignment {
	items, _, _ := c.Vars.Get(c.Sub.Assignment(worker))
	return AssignmentFromItems(worker, items)
}

func sortedUnits(units []string) []string {
	set := map[string]bool{}
	for _, u := range units {
		set[u] = true
	}
	out := make([]string, 0, len(set))
	for u := range set {
		out = append(out, u)
	}
	sort.Strings(out)
	return out
}

func (c *Controller) Assign(worker string, units []string) (Assignment, error) {
	items, err := c.Write(c.Sub.Assignment(worker), func(it Items) Items {
		rev, _ := strconv.Atoi(it["rev"])
		return Assignment{worker, sortedUnits(units), rev + 1}.ToItems()
	})
	return AssignmentFromItems(worker, items), err
}

// AssignAdd: read-modify-write — two controllers adding different units to
// one worker at once both land.
func (c *Controller) AssignAdd(worker, unit string) (Assignment, error) {
	items, err := c.Write(c.Sub.Assignment(worker), func(it Items) Items {
		a := AssignmentFromItems(worker, it)
		if a.Has(unit) {
			return nil
		}
		return Assignment{worker, sortedUnits(append(a.Units, unit)), a.Rev + 1}.ToItems()
	})
	return AssignmentFromItems(worker, items), err
}

func (c *Controller) AssignRemove(worker, unit string) (Assignment, error) {
	items, err := c.Write(c.Sub.Assignment(worker), func(it Items) Items {
		a := AssignmentFromItems(worker, it)
		if !a.Has(unit) {
			return nil
		}
		var keep []string
		for _, u := range a.Units {
			if u != unit {
				keep = append(keep, u)
			}
		}
		if keep == nil {
			keep = []string{}
		}
		return Assignment{worker, keep, a.Rev + 1}.ToItems()
	})
	return AssignmentFromItems(worker, items), err
}

func (c *Controller) Assignments() map[string]Assignment {
	out := map[string]Assignment{}
	paths, _ := c.Vars.List(c.Sub.Name + "/workers/")
	for _, p := range paths {
		w := p[strings.LastIndex(p, "/")+1:]
		out[w] = c.Assignment(w)
	}
	return out
}

// Slots: read them, never hand them out.
func (c *Controller) Slots() map[string]Slot {
	out := map[string]Slot{}
	paths, _ := c.Vars.List(c.Sub.Name + "/slots/")
	for _, p := range paths {
		name := p[strings.LastIndex(p, "/")+1:]
		items, _, _ := c.Vars.Get(p)
		out[name] = SlotFromItems(name, items)
	}
	return out
}

// ReleasedSlots: slots whose holder let go on purpose (scale-in, or Retire)
// and that still have an assignment — what a subsystem redistributes. A slot
// that merely lapsed is NOT here: that is a crash, and the scheduler brings
// its process back under the same name.
func (c *Controller) ReleasedSlots() []string {
	var out []string
	for n, s := range c.Slots() {
		if s.Released && len(c.Assignment(n).Units) > 0 {
			out = append(out, n)
		}
	}
	sort.Slice(out, func(i, j int) bool { return SlotNumber(out[i]) < SlotNumber(out[j]) })
	if out == nil {
		out = []string{}
	}
	return out
}

// Retire is an operator's statement that a slot is gone for good. The
// controller never decides this on its own from a silence.
func (c *Controller) Retire(worker string) (Slot, error) {
	items, err := c.Write(c.Sub.SlotKey(worker), func(it Items) Items {
		s := SlotFromItems(worker, it)
		if s.Released {
			return nil
		}
		return Slot{worker, s.Holder, s.Until, true, s.Gen}.ToItems()
	})
	return SlotFromItems(worker, items), err
}

// Worker runs its assignment and reports. Reads <name>/workers/<me> and the
// units it names; writes its heartbeat object and, when it starts a unit,
// that unit's epoch by CAS. Never writes configuration.
type Worker struct {
	Sub                   Subsystem
	Vars                  Variables
	Objects               ObjectStore
	Clock, Wall           Clock
	LeaseTTL, LeaseMargin float64
	Epochs                map[string]int
	Leases                map[string]*Lease
	Instance              string
	SlotTTL               float64
	Slot                  *Slot
	Name                  string // "" until ClaimSlot
}

// WorkerOptions are the knobs a test turns; zero values mean the defaults.
type WorkerOptions struct {
	Name                  string // a fixed name, no slot claimed; "" until ClaimSlot
	LeaseTTL, LeaseMargin float64
	Clock, Wall           Clock
	Instance              string
	SlotTTL               float64
}

func NewWorker(sub Subsystem, vars Variables, objects ObjectStore, o WorkerOptions) *Worker {
	w := &Worker{Sub: sub, Vars: vars, Objects: objects, Clock: o.Clock, Wall: o.Wall,
		LeaseTTL: o.LeaseTTL, LeaseMargin: o.LeaseMargin, Epochs: map[string]int{}, Leases: map[string]*Lease{},
		Instance: o.Instance, SlotTTL: o.SlotTTL, Name: o.Name}
	if w.Clock == nil {
		w.Clock = Monotonic()
	}
	if w.Wall == nil {
		w.Wall = Wall()
	}
	if w.LeaseTTL == 0 {
		w.LeaseTTL = 30
	}
	if w.LeaseMargin == 0 {
		w.LeaseMargin = 5
	}
	if w.SlotTTL == 0 {
		w.SlotTTL = 45
	}
	if w.Instance == "" {
		host, _ := os.Hostname()
		w.Instance = fmt.Sprintf("%s:%d:%06x", host, os.Getpid(), randomSuffix())
	}
	return w
}

// ClaimSlot: become somebody. With prefer (Nomad's NOMAD_ALLOC_INDEX,
// systemd's %i) take that slot, by CAS, even from a holder that has not
// lapsed — the scheduler is the authority on which process is the current
// one. Without it, take a lapsed slot — its assignment is waiting — before
// an unused number.
func (w *Worker) ClaimSlot(prefer string) (string, error) {
	prefix := w.Sub.Name + "/slots/"
	now := w.Wall()
	for try := 0; try < 50; try++ {
		paths, err := w.Vars.List(prefix)
		if err != nil {
			return "", err
		}
		known := map[string]Slot{}
		var names []string
		for _, p := range paths {
			n := p[len(prefix):]
			items, _, _ := w.Vars.Get(p)
			known[n] = SlotFromItems(n, items)
			names = append(names, n)
		}
		var order []string
		if prefer != "" {
			order = []string{prefer}
		} else {
			var lapsed, free []string
			maxN := 0
			for n, s := range known {
				if s.Lapsed(now) {
					lapsed = append(lapsed, n)
				} else if s.Claimable(now) {
					free = append(free, n)
				}
				if SlotNumber(n) > maxN {
					maxN = SlotNumber(n)
				}
			}
			sort.Slice(lapsed, func(i, j int) bool {
				if known[lapsed[i]].Until != known[lapsed[j]].Until {
					return known[lapsed[i]].Until < known[lapsed[j]].Until
				}
				return SlotNumber(lapsed[i]) < SlotNumber(lapsed[j])
			})
			sort.Slice(free, func(i, j int) bool { return SlotNumber(free[i]) < SlotNumber(free[j]) })
			order = append(append(lapsed, free...), fmt.Sprintf("w-%d", maxN+1))
		}
		for _, cand := range order {
			items, idx, _ := w.Vars.Get(prefix + cand)
			cur := SlotFromItems(cand, items)
			if prefer == "" && !cur.Claimable(now) {
				continue
			}
			ns := Slot{cand, w.Instance, now + w.SlotTTL, false, cur.Gen + 1}
			if _, err := w.Vars.Put(prefix+cand, ns.ToItems(), idx); errors.Is(err, ErrConflict) {
				continue // somebody took it between the read and the write
			} else if err != nil {
				return "", err
			}
			w.Slot, w.Name = &ns, cand
			return cand, nil
		}
	}
	return "", fmt.Errorf("%s: could not claim a slot in 50 tries", w.Instance)
}

// RenewSlot: still me? If another instance holds the slot now, the instance
// is fenced as a whole. Extends Until by CAS otherwise.
func (w *Worker) RenewSlot() bool {
	if w.Slot == nil {
		return true
	}
	items, idx, _ := w.Vars.Get(w.Sub.SlotKey(w.Name))
	cur := SlotFromItems(w.Name, items)
	if cur.Holder != w.Instance {
		return false
	}
	ns := Slot{w.Name, w.Instance, w.Wall() + w.SlotTTL, false, cur.Gen}
	if _, err := w.Vars.Put(w.Sub.SlotKey(w.Name), ns.ToItems(), idx); err != nil {
		return false
	}
	w.Slot = &ns
	return true
}

// ReleaseSlot: an orderly stop. Says so in the row — released — which is
// what tells scale-in from a crash.
func (w *Worker) ReleaseSlot() {
	if w.Slot == nil {
		return
	}
	items, idx, _ := w.Vars.Get(w.Sub.SlotKey(w.Name))
	cur := SlotFromItems(w.Name, items)
	if cur.Holder == w.Instance {
		w.Vars.Put(w.Sub.SlotKey(w.Name), Slot{w.Name, w.Instance, w.Wall(), true, cur.Gen}.ToItems(), idx)
	}
	w.Slot = nil
}

func (w *Worker) Assignment() Assignment {
	items, _, _ := w.Vars.Get(w.Sub.Assignment(w.Name))
	return AssignmentFromItems(w.Name, items)
}

// TakeEpoch is called when the worker STARTS a unit: a new epoch, by CAS,
// and a lease on it.
func (w *Worker) TakeEpoch(unit string) (int, error) {
	epoch, _, err := NextEpoch(w.Vars, w.Sub.EpochKey(unit))
	if err != nil {
		return 0, err
	}
	w.Epochs[unit] = epoch
	w.Leases[unit] = NewLease(w.Vars, w.Sub.EpochKey(unit), epoch, w.LeaseTTL, w.LeaseMargin, w.Clock)
	return epoch, nil
}

func (w *Worker) Release(unit string) {
	delete(w.Epochs, unit)
	delete(w.Leases, unit)
}

func (w *Worker) MayWrite(unit string) bool {
	l, ok := w.Leases[unit]
	return ok && l.MayWrite()
}

// RenewLeases returns the units whose lease was lost — fenced or expired.
func (w *Worker) RenewLeases() []string {
	var lost []string
	var units []string
	for u := range w.Leases {
		units = append(units, u)
	}
	sort.Slice(units, func(i, j int) bool { return unitLess(units[i], units[j]) })
	for _, u := range units {
		if !w.Leases[u].Renew() {
			lost = append(lost, u)
		}
	}
	if lost == nil {
		lost = []string{}
	}
	return lost
}

func unitLess(a, b string) bool {
	ai, errA := strconv.Atoi(a)
	bi, errB := strconv.Atoi(b)
	if errA == nil && errB == nil {
		return ai < bi
	}
	return a < b
}

func (w *Worker) Conflicts() int {
	n := 0
	for _, l := range w.Leases {
		n += l.Conflicts
	}
	return n
}

func (w *Worker) HeartbeatWith(status []map[string]any, extra map[string]any) error {
	if status == nil {
		status = []map[string]any{}
	}
	return w.Objects.Put(w.Sub.HeartbeatKey(w.Name), Heartbeat{w.Name, w.Wall(), status, extra}.ToBytes())
}
