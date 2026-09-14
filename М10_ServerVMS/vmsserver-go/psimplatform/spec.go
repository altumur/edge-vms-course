package psimplatform

// The controller as data. A subsystem gives the platform a spec — one YAML
// file — and the platform runs the controller from it:
//
//	name          the prefix: <name>/*
//	unit          the rows: where they live (<name>/<rows>/<id>), how an id is made (numeric | <field>),
//	              the operator's fields with types and defaults, and derived rows (a second row the
//	              platform keeps beside the unit — the VMS's <name>/retention/<id> that the resource reads)
//	placement     capacity and headroom as heartbeat fields; a constraint and a tie-break BY NAME from
//	              the catalogue — never an expression; the rebalance dead band
//	snapshot      the fields that leave the cluster, as one object for the layer above
//
// The catalogue is deliberately short: `labels-subset` and `most-free-capacity`.
// A subsystem that needs another rule registers a function under a name
// (RegisterConstraint) — code, named, not YAML pretending to be code.

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
)

// PlatformFields are never the operator's.
var PlatformFields = []string{"worker", "placement", "epoch", "revision", "observed_revision", "phase", "id"}

// Refused is the controller's answer to a client that asked for something it may not have.
type Refused struct{ Msg string }

func (r *Refused) Error() string { return r.Msg }

var ErrNoSuchUnit = errors.New("no such unit")

// Row is a unit as the controller reasons about it: "id" (int or string),
// the spec's fields typed, "revision".
type Row map[string]any

func (r Row) ID() string         { return Str(r["id"]) }
func (r Row) Int(k string) int   { return int(ToFloat(r[k])) }
func (r Row) Bool(k string) bool { b, _ := r[k].(bool); return b }
func (r Row) String(k string) string {
	if r[k] == nil {
		return ""
	}
	return Str(r[k])
}
func (r Row) List(k string) []string {
	switch x := r[k].(type) {
	case []string:
		return x
	case []any:
		out := []string{}
		for _, v := range x {
			out = append(out, Str(v))
		}
		return out
	}
	return []string{}
}

type Placement struct {
	Unit   string
	Worker string
	Reason string
	At     float64
	Rev    int
}

type Move struct {
	Unit     string
	From, To string
}

type FieldSpec struct {
	Name     string
	Type     string // string | int | float | bool | list
	Default  any
	Required bool
}

func (f FieldSpec) DefaultValue() any {
	if f.Default != nil {
		return f.Parse(f.Default)
	}
	switch f.Type {
	case "int":
		return 0
	case "float":
		return 0.0
	case "bool":
		return false
	case "list":
		return []string{}
	}
	return ""
}

func (f FieldSpec) Parse(v any) any {
	if v == nil {
		return f.DefaultValue()
	}
	switch f.Type {
	case "int":
		switch x := v.(type) {
		case string:
			n, _ := strconv.Atoi(x)
			return n
		case bool:
			if x {
				return 1
			}
			return 0
		}
		return int(ToFloat(v))
	case "float":
		if s, ok := v.(string); ok {
			f, _ := strconv.ParseFloat(s, 64)
			return f
		}
		return ToFloat(v)
	case "bool":
		if b, ok := v.(bool); ok {
			return b
		}
		return strings.ToLower(Str(v)) == "true"
	case "list":
		switch x := v.(type) {
		case []string:
			return append([]string{}, x...)
		case []any:
			out := []string{}
			for _, e := range x {
				out = append(out, Str(e))
			}
			return out
		}
		out := []string{}
		for _, e := range strings.Split(Str(v), ",") {
			if e != "" {
				out = append(out, e)
			}
		}
		return out
	}
	return Str(v)
}

func (f FieldSpec) ToItem(v any) string {
	switch f.Type {
	case "bool":
		if b, _ := v.(bool); b {
			return "true"
		}
		return "false"
	case "list":
		return strings.Join(Row{"x": v}.List("x"), ",")
	}
	return Str(v)
}

type DerivedSpec struct {
	Row      string            // under the subsystem's prefix, with {id}
	Items    map[string]string // item -> field
	OnDelete map[string]string // what the row becomes when the unit is deleted; nil: left alone
}

type SubsystemSpec struct {
	Name             string
	Rows             string
	ID               string // "numeric", or the field whose value is the id
	Fields           map[string]FieldSpec
	FieldOrder       []string
	Derived          []DerivedSpec
	CapacityFrom     string
	CapacityFallback int
	HeadroomFrom     string
	Constraint       string
	Requires         string // "resource": a worker is eligible only while its server's resource is not silent
	TieBreak         string
	DeadBand         float64
	Snapshot         []string
	RunningGauge     string // the console's gauge for units in phase "running": <name>_<RunningGauge>
}

func asMap(v any) map[string]any {
	m, _ := v.(map[string]any)
	if m == nil {
		return map[string]any{}
	}
	return m
}

func SpecFromMap(d map[string]any) (*SubsystemSpec, error) {
	name, _ := d["name"].(string)
	if name == "" {
		return nil, fmt.Errorf("spec: no name")
	}
	unit, pl := asMap(d["unit"]), asMap(d["placement"])
	s := &SubsystemSpec{Name: name, Rows: "units", ID: "numeric", Fields: map[string]FieldSpec{}, CapacityFrom: "capacity",
		CapacityFallback: 50, HeadroomFrom: "headroom", Constraint: "none", TieBreak: "most-free-capacity", DeadBand: 0.10, RunningGauge: "units_running"}
	if r, ok := unit["rows"].(string); ok {
		s.Rows = r
	}
	if id, ok := unit["id"].(string); ok {
		s.ID = id
	}
	fields := asMap(unit["fields"])
	for n := range fields {
		s.FieldOrder = append(s.FieldOrder, n)
	}
	sort.Strings(s.FieldOrder)
	for _, n := range s.FieldOrder {
		f := asMap(fields[n])
		fs := FieldSpec{Name: n, Type: "string", Default: f["default"]}
		if t, ok := f["type"].(string); ok {
			fs.Type = t
		}
		fs.Required, _ = f["required"].(bool)
		s.Fields[n] = fs
	}
	if ds, ok := unit["derived"].([]any); ok {
		for _, x := range ds {
			m := asMap(x)
			d := DerivedSpec{Row: Str(m["row"]), Items: map[string]string{}}
			for k, v := range asMap(m["items"]) {
				d.Items[k] = Str(v)
			}
			if od, ok := m["on_delete"]; ok && od != nil {
				d.OnDelete = map[string]string{}
				for k, v := range asMap(od) {
					d.OnDelete[k] = Str(v)
				}
			}
			s.Derived = append(s.Derived, d)
		}
	}
	if c := asMap(pl["capacity"]); len(c) > 0 {
		if f, ok := c["from"].(string); ok {
			s.CapacityFrom = f
		}
		if fb, ok := c["fallback"]; ok {
			s.CapacityFallback = int(ToFloat(fb))
		}
	}
	if h := asMap(pl["headroom"]); len(h) > 0 {
		if f, ok := h["from"].(string); ok {
			s.HeadroomFrom = f
		}
	}
	if c, ok := pl["constraint"].(string); ok {
		s.Constraint = c
	}
	if rq, ok := pl["requires"].(string); ok {
		s.Requires = rq
	}
	if tb, ok := pl["tie_break"].(string); ok {
		s.TieBreak = tb
	}
	if rb := asMap(pl["rebalance"]); rb["dead_band"] != nil {
		s.DeadBand = ToFloat(rb["dead_band"])
	}
	if snap, ok := d["snapshot"].([]any); ok {
		for _, f := range snap {
			s.Snapshot = append(s.Snapshot, Str(f))
		}
	} else {
		s.Snapshot = append([]string{}, s.FieldOrder...)
	}
	if con := asMap(d["console"]); con["running"] != nil {
		s.RunningGauge = Str(con["running"])
	}
	if _, ok := Constraints[s.Constraint]; !ok {
		return nil, fmt.Errorf("spec: unknown constraint %q", s.Constraint)
	}
	return s, nil
}

func LoadSpec(path string) (*SubsystemSpec, error) {
	src, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	v, err := ParseYAML(string(src))
	if err != nil {
		return nil, err
	}
	return SpecFromMap(asMap(v))
}

func (s *SubsystemSpec) Sub() Subsystem { return Subsystem{Name: s.Name} }

// ACLConsole: the operator's rows — what a console (one per server, any of them) may write; never placement.
func (s *SubsystemSpec) ACLConsole() []string {
	out := []string{s.Name + "/" + s.Rows + "/*", s.Name + "/next_id", s.Name + "/idem/*", // idem: a retried POST answered the same by ANY instance
		s.Name + "/policy"} // the administrator's knobs: servers shared | distinct
	for _, d := range s.Derived {
		out = append(out, s.Name+"/"+strings.Split(d.Row, "/")[0]+"/*")
	}
	return out
}

// ACLController: placement — what the controller (count = 1) may write; never a unit's row.
func (s *SubsystemSpec) ACLController() []string {
	return []string{s.Name + "/workers/*", s.Name + "/placement/*", s.Name + "/slots/*"}
}
func (s *SubsystemSpec) Numeric() bool { return s.ID == "numeric" }

func (s *SubsystemSpec) parseID(v string) any {
	if s.Numeric() {
		n, _ := strconv.Atoi(v)
		return n
	}
	return v
}

func (s *SubsystemSpec) RowOf(items Items) Row {
	r := Row{"id": s.parseID(items["id"])}
	for _, n := range s.FieldOrder {
		f := s.Fields[n]
		if v, ok := items[n]; ok {
			r[n] = f.Parse(v)
		} else {
			r[n] = f.DefaultValue()
		}
	}
	rev, err := strconv.Atoi(items["revision"])
	if err != nil {
		rev = 1
	}
	r["revision"] = rev
	return r
}

func (s *SubsystemSpec) ItemsOf(r Row) Items {
	out := Items{"id": r.ID(), "revision": strconv.Itoa(r.Int("revision"))}
	for _, n := range s.FieldOrder {
		f := s.Fields[n]
		v, ok := r[n]
		if !ok {
			v = f.DefaultValue()
		}
		out[n] = f.ToItem(v)
	}
	return out
}

func (s *SubsystemSpec) Refuse(fields map[string]any) error {
	var bad, unknown []string
	for k := range fields {
		if contains(PlatformFields, k) {
			bad = append(bad, k)
		} else if _, ok := s.Fields[k]; !ok {
			unknown = append(unknown, k)
		}
	}
	sort.Strings(bad)
	sort.Strings(unknown)
	if len(bad) > 0 {
		return &Refused{fmt.Sprintf("a client may not set %v: placement is decided and stored by the controller with a reason; revision, epoch and phase are not the operator's", bad)}
	}
	if len(unknown) > 0 {
		return &Refused{fmt.Sprintf("unknown field(s) %v", unknown)}
	}
	return nil
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

func (s *SubsystemSpec) NewRow(uid any, fields map[string]any) (Row, error) {
	r := Row{"id": uid, "revision": 1}
	for _, n := range s.FieldOrder {
		f := s.Fields[n]
		v, given := fields[n]
		if f.Required && (!given || v == nil || Str(v) == "") {
			return nil, &Refused{fmt.Sprintf("a %s unit needs a %s", s.Name, n)}
		}
		if given && v != nil {
			r[n] = f.Parse(v)
		} else {
			r[n] = f.DefaultValue()
		}
		if str, ok := r[n].(string); ok && strings.Contains(str, "{id}") {
			r[n] = strings.ReplaceAll(str, "{id}", Str(uid))
		}
	}
	return r, nil
}

// -- the catalogue --------------------------------------------------------------------
type Constraint func(row Row, workerLabels map[string]bool) bool

var Constraints = map[string]Constraint{
	"none": func(Row, map[string]bool) bool { return true },
	"labels-subset": func(row Row, labels map[string]bool) bool {
		for _, l := range row.List("labels") {
			if !labels[l] {
				return false
			}
		}
		return true
	},
}

// RegisterConstraint: a subsystem's own rule, as code under a name — never as YAML.
func RegisterConstraint(name string, c Constraint) { Constraints[name] = c }

func unitLess2(a, b string) bool {
	ai, errA := strconv.Atoi(a)
	bi, errB := strconv.Atoi(b)
	if errA == nil && errB == nil {
		return ai < bi
	}
	if errA == nil {
		return true
	}
	if errB == nil {
		return false
	}
	return a < b
}

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
		if it, _, _ := c.Vars.Get(c.rowKey(name)); it != nil {
			return nil, &Refused{fmt.Sprintf("%s unit %s exists", c.Spec.Name, name)}
		}
		uid = name
	}
	r, err := c.Spec.NewRow(uid, fields)
	if err != nil {
		return nil, err
	}
	if _, err := c.Vars.Put(c.rowKey(Str(uid)), c.Spec.ItemsOf(r), 0); err != nil {
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
	out := []string{}
	for _, w := range workers {
		if rule(r, c.LabelsOf(w)) {
			out = append(out, w)
		}
	}
	return out
}

// The administrator's knobs: one row, <name>/policy, written by the console.
// servers: "shared" (default) — every worker is a place to put units, two on
// one server included (a box IS several workers on one server); a dead
// server's slot is Nomad's to reschedule onto a neighbour. "distinct" — one
// worker per server carries units; a second worker Nomad put on the same
// server idles by policy, and a server whose worker and resource both fall
// silent is gone (GoneServers) — its units move.
var PolicyDefaults = map[string]string{"servers": "shared"}
var PolicyChoices = map[string][]string{"servers": {"distinct", "shared"}}

func (c *SpecController) Policy() map[string]string {
	out := map[string]string{}
	for k, v := range PolicyDefaults {
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
func (c *SpecController) pool(workers []string) []string {
	all := c.seen(workers)
	gone := map[string]bool{}
	for _, w := range c.WithoutResource(all) {
		gone[w] = true
	}
	for _, w := range c.IdleByPolicy(all) {
		gone[w] = true
	}
	out := []string{}
	for _, w := range all {
		if !gone[w] {
			out = append(out, w)
		}
	}
	return out
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
	best, free := c.best(pool)
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
			best, bestFree := "", -1<<30
			for _, w := range pool {
				if f := c.CapacityOf(w) - c.Load(w); f > bestFree {
					best, bestFree = w, f
				}
			}
			if best == "" || c.Load(best) >= c.CapacityOf(best) {
				break // the system is full; the unit waits, listed where it was
			}
			c.MoveTo(uid, best, fmt.Sprintf("%s; most free capacity (%d); on %s", g.why, bestFree, c.ServerOf(best)))
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
