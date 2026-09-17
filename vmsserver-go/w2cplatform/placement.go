package w2cplatform

// Where a unit GOES — the other half of unit.go. Everything here is the controller's: the pool, the
// filters (constraint, spread_by), the preferences (near, home), the tie-break, redistribution and
// rebalancing.
//
// The split from unit.go is the spec's own: a `unit` block says what a row IS — fields, types, defaults,
// the id rule — and a `placement` block says where it goes. A worker needs the first and never reads the
// second; it takes its assignment and does what it says, with no opinion about how the assignment was
// arrived at. That is why a controller and a worker can meet in the store and nowhere else, and it is
// worth being able to see in the file list.

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
)

// SpecController is the only writer of <name>/*, from a spec. Holds nothing;
// two instances are harmless; never on the recovery path. The VMS is one
// spec; live and det are others — same code.
type SpecController struct {
	*Controller
	Spec     *SubsystemSpec
	Capacity int    // the FALLBACK for a worker whose heartbeat says nothing
	Cluster  string // the name the snapshot carries; one box is a cluster of one
}

func NewSpecController(spec *SubsystemSpec, vars Variables, objects ObjectStore, capacity int, wall Clock, cluster string) *SpecController {
	if capacity == 0 {
		capacity = spec.CapacityFallback
	}
	if cluster == "" {
		cluster = os.Getenv("CLUSTER")
		if cluster == "" {
			cluster = "cluster-a"
		}
	}
	return &SpecController{NewController(spec.Sub(), vars, objects, wall), spec, capacity, cluster}
}

func (c *SpecController) rowKey(uid string) string { return c.Sub.Config(c.Spec.Rows, uid) }

// -- what the workers say --------------------------------------------------------
func (c *SpecController) CapacityOf(worker string) int {
	if hb, ok := c.WorkersSeen(1e12)[worker]; ok {
		if _, has := hb.Extra[c.Spec.CapacityFrom]; has {
			return hb.ExtraInt(c.Spec.CapacityFrom, c.Capacity)
		}
	}
	return c.Capacity
}

func (c *SpecController) LabelsOf(worker string) map[string]bool {
	out := map[string]bool{}
	if hb, ok := c.WorkersSeen(1e12)[worker]; ok {
		for _, l := range strings.Split(hb.ExtraString("labels", ""), ",") {
			if l != "" {
				out[l] = true
			}
		}
	}
	return out
}

func (c *SpecController) ServerOf(worker string) string {
	if hb, ok := c.WorkersSeen(1e12)[worker]; ok {
		return hb.ExtraString("server", "?")
	}
	return "?"
}

func (c *SpecController) Headroom() int {
	n := 0
	for _, hb := range c.WorkersSeen(45) {
		n += hb.ExtraInt(c.Spec.HeadroomFrom, 0)
	}
	return n
}

// -- units ------------------------------------------------------------------------
func (c *SpecController) nextID() (int, error) {
	items, err := c.Write(c.Sub.Config("next_id"), func(it Items) Items {
		n, _ := strconv.Atoi(it["n"])
		return Items{"n": strconv.Itoa(n + 1)}
	})
	if err != nil {
		return 0, err
	}
	n, _ := strconv.Atoi(items["n"])
	return n, nil
}

func (c *SpecController) derived(r Row, uid string, deleted bool) error {
	for _, d := range c.Spec.Derived {
		path := c.Sub.Config(strings.Split(strings.ReplaceAll(d.Row, "{id}", uid), "/")...)
		if deleted {
			if d.OnDelete != nil {
				want := Items{}
				for k, v := range d.OnDelete {
					want[k] = v
				}
				if _, err := c.Write(path, func(it Items) Items {
					if len(it) == 0 {
						return nil
					}
					return want
				}); err != nil {
					return err
				}
			}
			continue
		}
		want := Items{}
		for k, f := range d.Items {
			want[k] = c.Spec.Fields[f].ToItem(r[f])
		}
		if _, err := c.Write(path, func(it Items) Items {
			same := len(it) == len(want)
			for k, v := range want {
				if it[k] != v {
					same = false
				}
			}
			if same {
				return nil
			}
			return want
		}); err != nil {
			return err
		}
	}
	return nil
}

func (c *SpecController) Create(fields map[string]any) (Row, error) {
	if err := c.Spec.Refuse(fields); err != nil {
		return nil, err
	}
	var uid any
	if c.Spec.Numeric() {
		n, err := c.nextID()
		if err != nil {
			return nil, err
		}
		uid = n
	} else {
		name := Str(fields[c.Spec.ID])
		if fields[c.Spec.ID] == nil || name == "" {
			return nil, &Refused{fmt.Sprintf("a %s unit needs a %s", c.Spec.Name, c.Spec.ID)}
		}
		// A named unit's id comes VERBATIM from the operator's body, and from here
		// it becomes three things: the key <sub>/<rows>/<id>, the prefix an ACL is
		// matched against, and a directory on a resource's disk (UnitDir). So it is
		// a name, not a path. SafePath refuses the same shapes one layer down; this
		// is a 400 to the person who typed it rather than a 500 from the store.
		if strings.Contains(name, "/") || name == "." || name == ".." {
			return nil, &Refused{fmt.Sprintf("a %s %s is a name, not a path: %q", c.Spec.Name, c.Spec.ID, name)}
		}
		if it, _, _ := c.Vars.Get(c.rowKey(name)); it != nil {
			return nil, &Refused{fmt.Sprintf("%s unit %s exists", c.Spec.Name, name)}
		}
		uid = name
	}
	r, err := c.Spec.NewRow(uid, fields)
	if err != nil {
		return nil, err
	}
	if _, err := c.Vars.Put(c.rowKey(Str(uid)), c.Spec.ItemsOf(r), Absent); err != nil { // create-only: a row is written once
		return nil, err
	}
	return r, c.derived(r, Str(uid), false)
}

func (c *SpecController) Update(uid string, fields map[string]any) (Row, error) {
	if err := c.Spec.Refuse(fields); err != nil {
		return nil, err
	}
	items, err := c.Write(c.rowKey(uid), func(it Items) Items {
		if len(it) == 0 || it["deleted"] == "true" {
			panic(&ErrRow{Msg: ErrNoSuchUnit.Error()})
		}
		r := c.Spec.RowOf(it)
		for k, v := range fields {
			r[k] = c.Spec.Fields[k].Parse(v)
		}
		r["revision"] = r.Int("revision") + 1 // the trigger from М9 Lesson 5, in the controller
		return c.Spec.ItemsOf(r)
	})
	var rowErr *ErrRow
	if errors.As(err, &rowErr) {
		return nil, ErrNoSuchUnit
	}
	if err != nil {
		return nil, err
	}
	r := c.Spec.RowOf(items)
	for _, d := range c.Spec.Derived {
		for _, f := range d.Items {
			if _, ok := fields[f]; ok {
				return r, c.derived(r, uid, false)
			}
		}
	}
	return r, nil
}

// Delete is the operator's half: the row is marked. Its placement is the
// controller's half, taken back on the next pass (UnplaceDeleted) — a
// console's token cannot touch an assignment, and does not need to.
func (c *SpecController) Delete(uid string) error {
	if _, err := c.Write(c.rowKey(uid), func(it Items) Items {
		if len(it) == 0 {
			return nil
		}
		it["deleted"] = "true"
		return it
	}); err != nil {
		return err
	}
	return c.derived(nil, uid, true)
}

// UnplaceDeleted is the controller's half of a delete: every placement whose
// unit is gone loses its assignment and its row says so. Runs first in every pass.
func (c *SpecController) UnplaceDeleted() []string {
	gone := []string{}
	paths, _ := c.Vars.List(c.Sub.Config("placement") + "/")
	for _, pth := range paths {
		uid := pth[strings.LastIndex(pth, "/")+1:]
		it, _, _ := c.Vars.Get(pth)
		if it == nil || it["worker"] == "" || c.Unit(uid) != nil {
			continue
		}
		c.AssignRemove(it["worker"], uid)
		c.Write(pth, func(it Items) Items {
			n, _ := strconv.Atoi(it["rev"])
			return Items{"worker": "", "reason": "deleted", "at": Str(c.Wall()), "rev": strconv.Itoa(n + 1)}
		})
		gone = append(gone, uid)
	}
	return gone
}

func (c *SpecController) Unit(uid string) Row {
	it, _, _ := c.Vars.Get(c.rowKey(uid))
	if it == nil || it["deleted"] == "true" {
		return nil
	}
	return c.Spec.RowOf(it)
}

func (c *SpecController) Units() []Row {
	out := []Row{}
	paths, _ := c.Vars.List(c.Sub.Config(c.Spec.Rows) + "/")
	for _, pth := range paths {
		it, _, _ := c.Vars.Get(pth)
		if it != nil && it["deleted"] != "true" {
			out = append(out, c.Spec.RowOf(it))
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return unitLess2(out[i].ID(), out[j].ID()) })
	return out
}

// -- placement ---------------------------------------------------------------------
func (c *SpecController) Placement(uid string) *Placement {
	it, _, _ := c.Vars.Get(c.Sub.Config("placement", uid))
	if it == nil || it["worker"] == "" {
		return nil
	}
	at, _ := strconv.ParseFloat(it["at"], 64)
	rev, _ := strconv.Atoi(it["rev"])
	return &Placement{uid, it["worker"], it["reason"], at, rev}
}

func (c *SpecController) Load(worker string) int { return len(c.Assignment(worker).Units) }

func (c *SpecController) Eligible(r Row, workers []string) []string {
	rule := Constraints[c.Spec.Constraint]
	taken := c.ServersTaken(r)
	out := []string{}
	for _, w := range workers {
		if rule(r, c.LabelsOf(w)) && !taken[c.ServerOf(w)] {
			out = append(out, w)
		}
	}
	return out
}

// The servers already carrying a unit that shares this row's SpreadBy value — where this one may
// therefore NOT go. Empty when the subsystem does not ask to spread, which is every subsystem today.
//
// Read the whole rule in one sentence: two recordings of one camera exist to survive one server, so
// putting them on one server is not a compromise, it is the failure the operator was buying insurance
// against. Near pulls a recorder towards the camera's holder and would otherwise pull BOTH copies to
// the same place — the preference loses to the filter, and the reason says which.
func (c *SpecController) ServersTaken(r Row) map[string]bool {
	field := c.Spec.SpreadBy
	taken := map[string]bool{}
	if field == "" {
		return taken
	}
	value := Str(r[field])
	if r[field] == nil || value == "" {
		return taken
	}
	mine := r.ID()
	for _, other := range c.Units() {
		if other.ID() == mine || Str(other[field]) != value {
			continue
		}
		if pl := c.Placement(other.ID()); pl != nil {
			taken[c.ServerOf(pl.Worker)] = true
		}
	}
	return taken
}

// The administrator's knobs: one row, <name>/policy, written by the console.
// servers: "shared" (default) — every worker is a place to put units, two on
// one server included (a box IS several workers on one server); a dead
// server's slot is Nomad's to reschedule onto a neighbour. "distinct" — one
// worker per server carries units; a second worker Nomad put on the same
// server idles by policy, and a server whose worker and resource both fall
// silent is gone (GoneServers) — its units move.
var PolicyChoices = map[string][]string{"servers": {"distinct", "shared"}}

// PolicyDefaults: the spec's own defaults for the knobs — the VMS says shared (a box IS several workers on
// one server), the recorder says distinct (a second recorder on the same disks is no second place to record).
func (c *SpecController) PolicyDefaults() map[string]string {
	return map[string]string{"servers": c.Spec.Servers}
}

func (c *SpecController) Policy() map[string]string {
	out := map[string]string{}
	for k, v := range c.PolicyDefaults() {
		out[k] = v
	}
	it, _, _ := c.Vars.Get(c.Sub.Config("policy"))
	for k, v := range it {
		if choices, ok := PolicyChoices[k]; ok {
			for _, ch := range choices {
				if ch == v {
					out[k] = v
				}
			}
		}
	}
	return out
}

func (c *SpecController) SetPolicy(changes map[string]string) (map[string]string, error) {
	for k, v := range changes {
		ok := false
		for _, ch := range PolicyChoices[k] {
			ok = ok || ch == v
		}
		if !ok {
			return nil, &Refused{fmt.Sprintf("policy %s must be one of %s", k, strings.Join(PolicyChoices[k], ", "))}
		}
	}
	_, err := c.Write(c.Sub.Config("policy"), func(it Items) Items {
		out := Items{}
		for k, v := range it {
			out[k] = v
		}
		for k, v := range changes {
			out[k] = v
		}
		return out
	})
	if err != nil {
		return nil, err
	}
	return c.Policy(), nil
}

// IdleByPolicy: under servers: distinct, the workers that a server's OTHER
// workers must yield to — per server, the worker with units (the most, ties
// to the first name), else the first by name; the rest idle. Under shared, nobody.
func (c *SpecController) IdleByPolicy(workers []string) []string {
	out := []string{}
	if c.Policy()["servers"] != "distinct" {
		return out
	}
	byServer := map[string][]string{}
	sorted := append([]string{}, workers...)
	sort.Slice(sorted, func(i, j int) bool { return SlotNumber(sorted[i]) < SlotNumber(sorted[j]) })
	for _, w := range sorted {
		s := c.ServerOf(w)
		byServer[s] = append(byServer[s], w)
	}
	for server, ws := range byServer {
		if server == "?" || len(ws) < 2 {
			continue
		}
		keep := ws[0]
		for _, w := range ws[1:] {
			if c.Load(w) > c.Load(keep) {
				keep = w
			}
		}
		for _, w := range ws {
			if w != keep {
				out = append(out, w)
			}
		}
	}
	sort.Strings(out)
	return out
}

// ResourceState: the state of the resource on a server, from
// platform/resources/<server>/heartbeat — "live" (younger than lostAfter),
// "silent" (older), "unknown" (never heartbeaten: a box before its resource
// process starts, a bench). Nomad's meta.archive constraint puts a worker
// where disks are declared; this is the live fact: whether the resource
// there still answers.
func (c *SpecController) ResourceState(server string, lostAfter float64) string {
	hb, ok := ResourcesSeen(c.Objects)[server]
	if !ok {
		return "unknown"
	}
	if c.Wall()-hb.Ts <= lostAfter {
		return "live"
	}
	return "silent"
}

// WithoutResource: workers whose server's resource is silent, when the spec
// requires one — not placed on, and (in Redistribute) moved off. A worker on
// a server whose resource was never seen passes: "silent" is a fact,
// "unknown" is not one.
func (c *SpecController) WithoutResource(workers []string) []string {
	out := []string{}
	if c.Spec.Requires != "resource" {
		return out
	}
	for _, w := range workers {
		if c.ResourceState(c.ServerOf(w), 45) == "silent" {
			out = append(out, w)
		}
	}
	return out
}

// GoneServers: lapsed slots whose server's resource is silent too —
// {slot: server}. A server that is gone, not a process that crashed: the
// slot lapsed and stayed lapsed for another lostAfter (Nomad's chance to
// reschedule it onto a spare server, whose replacement would claim the name
// and inherit the assignment) AND the resource on the slot's last known
// server is silent. One silence is a crash and is left alone; two independent
// silences from the same server are a fact about the server. Only when the
// spec requires a resource.
func (c *SpecController) GoneServers(lostAfter float64) map[string]string {
	out := map[string]string{}
	if c.Spec.Requires != "resource" || c.Policy()["servers"] != "distinct" {
		return out // shared: a dead server's slot is Nomad's to reschedule onto a neighbour
	}
	now := c.Wall()
	for name, slot := range c.Slots() {
		if slot.Lapsed(now) && now > slot.Until+lostAfter && len(c.Assignment(name).Units) > 0 {
			server := c.ServerOf(name)
			if server != "?" && c.ResourceState(server, lostAfter) == "silent" {
				out[name] = server
			}
		}
	}
	return out
}

func (c *SpecController) seen(workers []string) []string {
	if workers == nil {
		for w := range c.WorkersSeen(45) {
			workers = append(workers, w)
		}
	}
	out := append([]string{}, workers...)
	sort.Strings(out)
	return out
}

// pool: the given list, or the workers seen heartbeating in the last 45 s;
// minus those whose resource is silent when the spec requires one; sorted.
// OnDraining: workers on the server being drained. The third kind of "not here" beside a released slot
// and a silent resource — and the only one the operator says BEFORE it is true, which is the whole point
// of an upgrade: being noticed is the slow path, and a planned stop is not a silence.
func (c *SpecController) OnDraining(workers []string) []string {
	server := Draining(c.Vars)
	if server == "" {
		return nil
	}
	out := []string{}
	for _, w := range workers {
		if c.ServerOf(w) == server {
			out = append(out, w)
		}
	}
	return out
}

// WouldStrand is the dry run: which units nothing else could serve if this server went away — asked
// BEFORE it does, with the machinery that will answer for real afterwards. Fifty cameras leaving a
// machine have to land somewhere, and "somewhere" is a fact about headroom and labels, not a hope.
func (c *SpecController) WouldStrand(server string, workers []string) []string {
	var left []string
	for _, w := range c.pool(workers) {
		if c.ServerOf(w) != server {
			left = append(left, w)
		}
	}
	out := []string{}
	for _, row := range c.Units() {
		uid := row.ID()
		if pl := c.Placement(uid); pl != nil && c.ServerOf(pl.Worker) != server {
			continue // it is not on that server: not its business
		}
		if len(c.Eligible(row, left)) == 0 {
			out = append(out, uid)
		}
	}
	return out
}

func (c *SpecController) pool(workers []string) []string {
	all := c.seen(workers)
	gone := map[string]bool{}
	for _, w := range c.OnDraining(all) { // an operator said this machine is about to stop
		gone[w] = true
	}
	for _, w := range c.WithoutResource(all) {
		gone[w] = true
	}
	for _, w := range c.IdleByPolicy(all) {
		gone[w] = true
	}
	// A RELEASED slot is on its way out: Redistribute moves its units off, and
	// until this line it could be handed new ones on the same pass — placed on a
	// process that is already shutting down. Leaving is not a capacity.
	for n, sl := range c.Slots() {
		if sl.Released {
			gone[n] = true
		}
	}
	out := []string{}
	for _, w := range all {
		if !gone[w] {
			out = append(out, w)
		}
	}
	return out
}

// HolderNear: for `near: <sub>`, the worker of that subsystem whose heartbeat status lists this unit's id
// in phase running — (worker, server) — or "". The recorder says `near: vms`: the unit's holder.
func (c *SpecController) HolderNear(uid string) (worker, server string) {
	if c.Spec.Near == "none" || c.Spec.Near == "" {
		return "", ""
	}
	names := []string{}
	hbs := Heartbeats(c.Objects, c.Spec.Near+"/")
	for w := range hbs {
		names = append(names, w)
	}
	sort.Strings(names)
	for _, w := range names {
		hb := hbs[w]
		if c.Wall()-hb.Ts > 45 {
			continue
		}
		for _, st := range hb.Status {
			if Str(st["id"]) == uid && Str(st["phase"]) == "running" {
				return w, hb.ExtraString("server", "?")
			}
		}
	}
	return "", ""
}

// pick: the affinity, then the tie-break — the best worker on the holder's server if one has room, else the
// best anywhere; the note says which ("beside w-1 holding it" / "away from w-1 on srv-1 (no room there)") so
// the reason tells the operator whether the unit reads its holder's shared memory or its RTSP fan-out.
// HomeFor is where a unit belongs, for EnsureHome. home is either the name of a field on the row — the
// server an operator named — or the literal "near", meaning "wherever the thing I follow is".
//
// Exactly one of a following pair may say home: near, and that is not a detail. Two subsystems that each
// follow the other have no anchor: every pass moves each towards where the other WAS, and they swap
// places instead of meeting. The anchor is the one with a real home — for the VMS, the recording,
// because it writes to a disk and a disk does not move.
func (c *SpecController) HomeFor(row Row) string {
	if c.Spec.Home == "near" {
		if _, server := c.HolderNear(row.ID()); server != "" && server != "?" {
			return server
		}
		return ""
	}
	if c.Spec.Home == "" {
		return ""
	}
	return Str(row[c.Spec.Home])
}

// HomeOf is the same, addressed by id — what pick needs before a unit is placed anywhere. The "near"
// form is resolved by pick, which already has the holder.
func (c *SpecController) HomeOf(uid string) string {
	if c.Spec.Home == "" || c.Spec.Home == "near" {
		return ""
	}
	if row := c.Unit(uid); row != nil {
		return Str(row[c.Spec.Home])
	}
	return ""
}

// pick, with the two affinities in order — home first, then near — over a pool the FILTERS have already
// cut (Eligible: the constraint and spread_by). The note says which it was, so the reason tells the
// operator both where the unit reads its source from and whether it is where it belongs.
//
// Home before near, because they disagree exactly when a server is down: near would pin a unit to
// whichever server picked it up, and nothing would ever come back.
func (c *SpecController) pick(pool []string, uid string) (best string, free int, note string) {
	holder, server := c.HolderNear(uid)
	follows := c.Spec.Home == "near"
	home := c.HomeOf(uid)
	if follows && holder != "" {
		home = server
	}
	if home != "" {
		var atHome []string
		for _, w := range pool {
			if c.ServerOf(w) == home {
				atHome = append(atHome, w)
			}
		}
		if b, f := c.best(atHome); b != "" {
			if follows {
				return b, f, ", beside " + holder + " holding it"
			}
			return b, f, ", at home on " + home
		}
	}
	if holder != "" && !follows {
		var beside []string
		for _, w := range pool {
			if c.ServerOf(w) == server {
				beside = append(beside, w)
			}
		}
		if b, f := c.best(beside); b != "" {
			note := ", beside " + holder + " holding it"
			if home != "" {
				note += " (home " + home + " has no room)"
			}
			return b, f, note
		}
	}
	best, free = c.best(pool)
	if best != "" && holder != "" && c.ServerOf(best) != server {
		note = ", away from " + holder + " on " + server + " (no room there)"
	}
	if best != "" && home != "" && !follows && c.ServerOf(best) != home {
		note += "; away from home " + home
	}
	return best, free, note
}

// EnsureHome: units away from the home their row names — or, with near and no home of their own, away
// from the server holding what they follow — moved back, budget a pass. It is the other half of home:
// pick decides where a unit GOES, this is what happens to one already placed somewhere else when its
// home comes back.
//
// Three things worth naming. budget: every move is a new epoch and a seam in the recording, so the
// controller calls it with one. Eligible first: the filters still beat the preference, and a preference
// that could overrule a filter would put a unit on a server that cannot reach it. And silence when the
// home is not back or has no room — the unit is where it can be, which is the point of a preference.
func (c *SpecController) EnsureHome(budget int, workers []string) []Move {
	moves := []Move{}
	if budget <= 0 || c.Spec.Home == "" {
		return moves
	}
	pool := c.pool(workers)
	for _, row := range c.Units() {
		if len(moves) >= budget {
			break
		}
		uid, home := row.ID(), c.HomeFor(row)
		pl := c.Placement(uid)
		if home == "" || pl == nil || c.ServerOf(pl.Worker) == home {
			continue
		}
		var atHome []string
		for _, w := range c.Eligible(row, pool) {
			if c.ServerOf(w) == home {
				atHome = append(atHome, w)
			}
		}
		best, free := c.best(atHome)
		if best == "" {
			continue // home is not back, or has no room: stay put, quietly
		}
		why := "home is"
		if c.Spec.Home == "near" {
			why = "it follows " + c.Spec.Near + " onto"
		}
		if _, err := c.MoveTo(uid, best, fmt.Sprintf("%s %s; most free capacity (%d); on %s", why, home, free, home)); err == nil {
			moves = append(moves, Move{uid, pl.Worker, best})
		}
	}
	return moves
}

func (c *SpecController) best(pool []string) (string, int) {
	best, free := "", 0
	for _, w := range pool { // most-free-capacity: the one tie-break in the catalogue
		if f := c.CapacityOf(w) - c.Load(w); f > free {
			best, free = w, f
		}
	}
	return best, free
}

// Place ONE unit on the worker with the most free capacity among those seen
// heartbeating (or given) that satisfy the constraint. An existing placement
// is returned untouched: adding a worker moves nothing. nil: "the system is
// full" — or nothing that can reach it.
func (c *SpecController) Place(uid string, workers []string) (*Placement, error) {
	if have := c.Placement(uid); have != nil {
		return have, nil
	}
	r := c.Unit(uid)
	if r == nil {
		return nil, nil
	}
	pool := c.Eligible(r, c.pool(workers))
	best, free, near := c.pick(pool, uid)
	if best == "" {
		return nil, nil
	}
	reason := fmt.Sprintf("most free capacity (%d) among %d worker(s)", free, len(pool))
	if c.Spec.Constraint == "labels-subset" && len(r.List("labels")) > 0 {
		ls := append([]string{}, r.List("labels")...)
		sort.Strings(ls)
		reason += " reaching " + strings.Join(ls, ",")
	}
	reason += "; on " + c.ServerOf(best)
	if c.Spec.Requires == "resource" {
		reason += ", whose resource is " + c.ResourceState(c.ServerOf(best), 45)
	}
	reason += near
	pl := Placement{uid, best, reason, c.Wall(), 0}
	// the row first (CAS decides who won), then the assignment
	written, err := c.Write(c.Sub.Config("placement", uid), func(it Items) Items {
		if it["worker"] != "" {
			return nil // the other instance placed it while we thought
		}
		n, _ := strconv.Atoi(it["rev"])
		return Items{"worker": pl.Worker, "reason": pl.Reason, "at": Str(pl.At), "rev": strconv.Itoa(n + 1)}
	})
	if err != nil {
		return nil, err
	}
	at, _ := strconv.ParseFloat(written["at"], 64)
	rev, _ := strconv.Atoi(written["rev"])
	out := Placement{uid, written["worker"], written["reason"], at, rev}
	_, err = c.AssignAdd(out.Worker, uid)
	return &out, err
}

func (c *SpecController) EnsurePlaced(workers []string) ([]Placement, error) {
	c.UnplaceDeleted()
	out := []Placement{}
	for _, r := range c.Units() {
		pl, err := c.Place(r.ID(), workers)
		if err != nil {
			return out, err
		}
		if pl != nil {
			out = append(out, *pl)
		}
	}
	return out, nil
}

type Unplaceable struct {
	ID          any      `json:"id"`
	Labels      []string `json:"labels"`
	WorkersLive int      `json:"workers_live"`
}

// Unplaceable: units nothing live can serve — the console's honest answer, with the labels named.
func (c *SpecController) Unplaceable() []Unplaceable {
	live := c.pool(nil)
	out := []Unplaceable{}
	for _, r := range c.Units() {
		if c.Placement(r.ID()) == nil && len(c.Eligible(r, live)) == 0 {
			out = append(out, Unplaceable{r["id"], r.List("labels"), len(live)})
		}
	}
	return out
}

func (c *SpecController) Where(uid string) string {
	if pl := c.Placement(uid); pl != nil {
		return pl.Worker
	}
	return ""
}

// MoveTo is the one two-writer operation: the destination takes the next
// epoch when it starts; the source's lease fences on renewal and it stops.
func (c *SpecController) MoveTo(uid, to, reason string) (Placement, error) {
	for w, a := range c.Assignments() { // wherever it is listed, and not only where the row says
		if a.Has(uid) && w != to {
			c.AssignRemove(w, uid)
		}
	}
	at := c.Wall()
	written, err := c.Write(c.Sub.Config("placement", uid), func(it Items) Items {
		n, _ := strconv.Atoi(it["rev"])
		return Items{"worker": to, "reason": reason, "at": Str(at), "rev": strconv.Itoa(n + 1)}
	})
	if err != nil {
		return Placement{}, err
	}
	c.AssignAdd(to, uid)
	rev, _ := strconv.Atoi(written["rev"])
	return Placement{uid, to, reason, at, rev}, nil
}

// Redistribute is the controller's one unasked move: a slot that was
// RELEASED still lists units — and, when the spec requires a resource, a live
// worker whose server's resource went silent (it heartbeats, but it has
// nowhere to write), and a slot that lapsed AND whose server's resource is
// silent (the server is gone; with one worker per server nobody will claim
// that slot until it returns). Move their units to the workers that are here.
func (c *SpecController) Redistribute(workers []string) []Move {
	c.UnplaceDeleted()
	moves := []Move{}
	type gone struct{ worker, why string }
	var gones []gone
	for _, g := range c.ReleasedSlots() {
		gones = append(gones, gone{g, "slot " + g + " released"})
	}
	for _, w := range c.WithoutResource(c.seen(workers)) {
		if len(c.Assignment(w).Units) > 0 {
			gones = append(gones, gone{w, "resource on " + c.ServerOf(w) + " silent"})
		}
	}
	for _, w := range c.OnDraining(c.seen(workers)) { // an operator said this machine is about to stop
		if len(c.Assignment(w).Units) > 0 {
			gones = append(gones, gone{w, "server " + c.ServerOf(w) + " draining"})
		}
	}
	gs := c.GoneServers(45) // the server is gone: its slot lapsed and its resource silent
	var gnames []string
	for w := range gs {
		gnames = append(gnames, w)
	}
	sort.Strings(gnames)
	for _, w := range gnames {
		gones = append(gones, gone{w, "server " + gs[w] + " gone: slot " + w + " lapsed and its resource silent"})
	}
	for _, g := range gones {
		var live []string
		for _, w := range c.pool(workers) {
			if w != g.worker {
				live = append(live, w)
			}
		}
		units := append([]string{}, c.Assignment(g.worker).Units...)
		sort.SliceStable(units, func(i, j int) bool { return unitLess2(units[i], units[j]) })
		for _, uid := range units {
			pool := live
			if r := c.Unit(uid); r != nil {
				pool = c.Eligible(r, live)
			}
			best, bestFree, near := c.pick(pool, uid)
			if best == "" {
				break // the system is full; the unit waits, listed where it was
			}
			c.MoveTo(uid, best, fmt.Sprintf("%s; most free capacity (%d); on %s%s", g.why, bestFree, c.ServerOf(best), near))
			moves = append(moves, Move{uid, g.worker, best})
		}
	}
	return moves
}

func (c *SpecController) Rebalance(budget int, deadBand float64, workers []string) []Move {
	if deadBand == 0 {
		deadBand = c.Spec.DeadBand
	}
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
		cands := append([]string{}, c.Assignment(hi).Units...)
		sort.SliceStable(cands, func(i, j int) bool { return unitLess2(cands[i], cands[j]) })
		if len(cands) == 0 || c.Load(lo)+1 > c.CapacityOf(lo) {
			break
		}
		c.MoveTo(cands[0], lo, fmt.Sprintf("rebalance from %s (spread %.0f%%)", hi, (loads[hi]-loads[lo])*100))
		moves = append(moves, Move{cands[0], hi, lo})
	}
	return moves
}

// -- what the console and the layer above read -------------------------------------------
func (c *SpecController) ReadModel(lostAfter float64) []map[string]any {
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
			row["worker"], row["server"], row["age"], row["worker_state"] = w, hb.ExtraString("server", "?"), float64(int64(age*10+0.5))/10, state
			rows = append(rows, row)
		}
	}
	sort.SliceStable(rows, func(i, j int) bool { return unitLess2(Str(rows[i]["id"]), Str(rows[j]["id"])) })
	return rows
}

// Snapshot: units and placement as one object — what the layer above reads.
// A copy with an age; never the rows themselves, which do not leave raft.
func (c *SpecController) Snapshot() map[string]any {
	units := []map[string]any{}
	for _, r := range c.Units() {
		m := map[string]any{"id": r["id"], "revision": r["revision"]}
		for _, f := range c.Spec.Snapshot {
			if v, ok := r[f]; ok {
				m[f] = v
			}
		}
		w := c.Where(r.ID())
		if w != "" {
			m["worker"] = w
		} else {
			m["worker"] = nil
		}
		m["server"] = c.ServerOf(w)
		units = append(units, m)
	}
	return map[string]any{"cluster": c.Cluster, "ts": c.Wall(), c.Spec.Rows: units}
}

func (c *SpecController) PublishSnapshot() error {
	raw, _ := json.Marshal(c.Snapshot())
	return c.Objects.Put(c.Sub.Config("snapshot"), raw)
}

// FailoverSeconds per worker: the gap between the heartbeat before its
// current instance started and that instance's first — measured from what
// the workers wrote, not from the scheduler.
func (c *SpecController) FailoverSeconds() map[string]float64 {
	out := map[string]float64{}
	for w, hb := range c.WorkersSeen(1e12) {
		started := hb.Ts
		if v, ok := hb.Extra["started"]; ok {
			started = ToFloat(v)
		}
		if prev := ToFloat(hb.Extra["previous_hb"]); prev > 0 {
			out[w] = float64(int64((started-prev)*10+0.5)) / 10
		}
	}
	return out
}
