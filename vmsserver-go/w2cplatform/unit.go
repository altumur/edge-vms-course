package w2cplatform

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
	"errors"
	"fmt"
	"net/url"
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
	Name string
	// string | int | float | bool | list | url | blob. A `blob` holds a DIGEST (`sha256-<hex>`); the
	// bytes live in the object store under `<name>/blobs/<digest>` and the platform never looks inside
	// them — see blobs.go.
	Type     string
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
	Servers          string // the default of the `servers` policy knob: shared | distinct (the console may change it)
	Near             string // a subsystem whose worker holding the same unit id this one prefers to be beside (an affinity, never a filter)
	// WHICH unit of that subsystem. "id" — the same unit id, which is what `near: <sub>` means and all the
	// VMS needed while a camera and its recording were the same string. A subsystem whose units are named
	// for something else has to say so: a detector's unit is `7-linecross`, no recorder ever reports that
	// id, and `near: rec` on it would match nothing — an affinity that reads as followed and is not. So
	// `near: {sub: rec, by: cam}` — follow the recorder holding the recording my `cam` field names.
	NearBy string
	// `spread_by: <field>` — units sharing a value of that field go on DIFFERENT servers. Unlike Near this
	// is a FILTER, not a preference: the whole point of a second copy is that it is not where the first one
	// is, and a second copy on the same server is not a second copy. Unplaceable while no other server
	// qualifies, and that is the honest answer — /unplaceable says so rather than quietly co-locating.
	SpreadBy string
	// home: <field> — the server named in that field of the unit's own row is where it prefers to run;
	// home: near — wherever the subsystem this one follows is. A PREFERENCE and not a label: a label is a
	// filter, and a unit whose home is down would become unplaceable — the one thing it must not be,
	// because the home being down is exactly when the work has to continue somewhere else.
	Home     string
	// place_by: <field> — WHAT the policy and the home are counted in: the heartbeat field naming the
	// place a worker occupies. "server" by default, and for everything whose unit of storage is a server
	// that is the truth. A recorder's is not: a box with three disks runs three recorders, one per volume,
	// and servers: distinct has to mean one per DISK. Only the policy and home follow this field —
	// reachability, draining and spread_by stay on the server, because those are about a machine.
	PlaceBy  string
	TieBreak string
	DeadBand float64
	// `retire_when: {field: state, in: [done, failed]}` — a unit whose row says one of those values is
	// FINISHED, and finished work is not placed. The first subsystem to need it is `detjob`, whose unit
	// ends; everything before it ran until an operator said stop.
	//
	// Not `enabled`. That field is read by workers, never here: a disabled camera keeps its assignment and
	// its line in the console's list, and a unit that has to STAY VISIBLE while doing nothing is a
	// different thing from one that is over. And it is the ROW that says it, not a heartbeat: un-placing a
	// finished unit makes its worker drop it, which makes the heartbeat stop mentioning it, which would
	// erase the only evidence it was ever finished — and it would be placed again, and start over.
	RetireField  string
	RetireValues []string
	Snapshot     []string
	RunningGauge string // the console's gauge for units in phase "running": <name>_<RunningGauge>
	// `events: {older_epochs: fenced | earlier-run}` — what it MEANS that a unit's events were written
	// under an epoch that is not the current one. "fenced" (the default, and right for everything that
	// runs until stopped): a writer that lost the race. "earlier-run": a unit that ENDS takes a new epoch
	// every time the operator runs it again, so an older epoch is a finished earlier result — a run to
	// compare against, not a loser to strike through.
	OlderEpochs string
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
		CapacityFallback: 50, HeadroomFrom: "headroom", Constraint: "none", Servers: "shared", Near: "none", NearBy: "id",
		TieBreak: "most-free-capacity", DeadBand: 0.10, RunningGauge: "units_running", OlderEpochs: "fenced"}
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
	if sv, ok := pl["servers"].(string); ok {
		s.Servers = sv
	}
	if nr, ok := pl["near"].(string); ok {
		s.Near = nr
	} else if nr := asMap(pl["near"]); len(nr) > 0 {
		if sub, ok := nr["sub"].(string); ok {
			s.Near = sub
		}
		if by, ok := nr["by"].(string); ok {
			s.NearBy = by
		}
	}
	if rw := asMap(pl["retire_when"]); len(rw) > 0 {
		if f, ok := rw["field"].(string); ok {
			s.RetireField = f
		}
		if vals, ok := rw["in"].([]any); ok {
			for _, v := range vals {
				s.RetireValues = append(s.RetireValues, Str(v))
			}
		}
	}
	if sb, ok := pl["spread_by"].(string); ok {
		s.SpreadBy = sb
	}
	if hm, ok := pl["home"].(string); ok {
		s.Home = hm
	}
	s.PlaceBy = "server"
	if pb, ok := pl["place_by"].(string); ok && pb != "" {
		s.PlaceBy = pb
	}
	if tb, ok := pl["tie_break"].(string); ok {
		s.TieBreak = tb
	}
	if rb := asMap(pl["rebalance"]); rb["dead_band"] != nil {
		s.DeadBand = ToFloat(rb["dead_band"])
	}
	// `snapshot:` LEFT OUT means "every field that may go" — a convenience. `snapshot: []` means "no field
	// of the row leaves the cluster", which is a decision. `snapshot:` with nothing after it is neither, so
	// it is refused rather than guessed: the author meant one of the two and the file does not say which.
	if v, present := d["snapshot"]; present && v == nil {
		return nil, fmt.Errorf("spec %s: `snapshot:` with nothing after it says neither — write `snapshot: []` for no fields, or leave the key out for every field", s.Name)
	}
	if snap, ok := d["snapshot"].([]any); ok {
		for _, f := range snap {
			s.Snapshot = append(s.Snapshot, Str(f))
		}
	} else {
		// The DEFAULT — "every field" — leaves secrets out rather than refusing: a spec that said nothing
		// made no mistake, and the safe reading of silence is the one that keeps the secret in.
		for _, n := range s.FieldOrder {
			if !IsSecretField(n) && s.Fields[n].Type != "blob" {
				s.Snapshot = append(s.Snapshot, n)
			}
		}
	}
	if con := asMap(d["console"]); con["running"] != nil {
		s.RunningGauge = Str(con["running"])
	}
	if ev := asMap(d["events"]); ev["older_epochs"] != nil {
		s.OlderEpochs = Str(ev["older_epochs"])
	}
	// A secret in the snapshot is a secret leaving the cluster: the snapshot is what М12's directory
	// reads. Refused at LOAD time, not watched for at review time — a subsystem written a year from now
	// cannot make the mistake, and nobody has to remember the rule to be protected by it.
	for _, n := range s.Snapshot {
		if IsSecretField(n) {
			return nil, fmt.Errorf("spec %s: a secret may not be in the snapshot: %q — the snapshot is what leaves the cluster", s.Name, n)
		}
		if _, ok := s.Fields[n]; !ok {
			return nil, fmt.Errorf("spec %s: snapshot names no field: %q", s.Name, n)
		}
		// A blob is the one field that is certainly too big for the snapshot, and the snapshot is one
		// object per worker with a ceiling over it. Refused at LOAD time for the same reason a secret is.
		if s.Fields[n].Type == "blob" {
			return nil, fmt.Errorf("spec %s: a blob may not be in the snapshot: %q — the snapshot is one object per worker under a ceiling, and a blob is what does not fit in a row in the first place", s.Name, n)
		}
	}
	if _, ok := Constraints[s.Constraint]; !ok {
		return nil, fmt.Errorf("spec: unknown constraint %q", s.Constraint)
	}
	// Following nothing cannot say it follows, and a home that names no field is a typo — let both fail
	// at start rather than turn quietly into "there is no home".
	if s.NearBy != "id" {
		if _, ok := s.Fields[s.NearBy]; !ok {
			return nil, fmt.Errorf("spec %s: near.by names no field: %q", s.Name, s.NearBy)
		}
		if s.Near == "none" || s.Near == "" {
			return nil, fmt.Errorf("spec %s: near.by needs a near to follow", s.Name)
		}
	}
	if s.RetireField != "" {
		if _, ok := s.Fields[s.RetireField]; !ok {
			return nil, fmt.Errorf("spec %s: retire_when names no field: %q", s.Name, s.RetireField)
		}
	}
	if (s.RetireField != "") != (len(s.RetireValues) > 0) {
		return nil, fmt.Errorf("spec %s: retire_when needs both a field and a non-empty `in` — a predicate that matches nothing retires nothing, silently", s.Name)
	}
	if s.OlderEpochs != "fenced" && s.OlderEpochs != "earlier-run" {
		return nil, fmt.Errorf("spec %s: events.older_epochs is fenced or earlier-run, not %q — the page draws one of the two", s.Name, s.OlderEpochs)
	}
	if s.Home == "near" && (s.Near == "none" || s.Near == "") {
		return nil, fmt.Errorf("spec %s: home: near needs a near to follow", s.Name)
	}
	if s.Home != "" && s.Home != "near" {
		if _, ok := s.Fields[s.Home]; !ok {
			return nil, fmt.Errorf("spec %s: home names no field: %q", s.Name, s.Home)
		}
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
		s.Name + "/policy",     // the administrator's knobs: servers shared | distinct
		s.Name + "/sweep",      // what the blob sweep marked, and when
		s.Name + "/requests/*", // bounded work an operator asked a worker for, outside its ordinary pass
		DrainKey}               // "this machine is about to stop": the operator's, and the same row for every subsystem
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
	// A `blob` field holds the digest of the bytes, never the bytes. Without this, the obvious thing for
	// a client to do — paste the lump into the row — is also the thing that puts a row over the store's
	// ceiling, and the refusal it gets says "too big" rather than what to do instead.
	blobNames := make([]string, 0, len(s.Fields))
	for n := range s.Fields {
		blobNames = append(blobNames, n)
	}
	sort.Strings(blobNames)
	for _, n := range blobNames {
		if s.Fields[n].Type != "blob" {
			continue
		}
		if v, ok := fields[n]; ok && Str(v) != "" && !IsDigest(Str(v)) {
			return &Refused{fmt.Sprintf("%s takes a digest, not the bytes (%d of them): PUT the bytes to /%s/<id>/%s and the row gets the digest back", n, len(Str(v)), s.Rows, n)}
		}
	}
	// A `url` field may not carry a userinfo. `rtsp://root:hunter2@10.0.0.5/…` is how a password reaches a
	// row that is in the SNAPSHOT — out of the cluster, into М12's directory, and onto every console
	// screen, past a mask that only looks at `*_secret`. Refused rather than moved quietly into the
	// credential fields: an operator who pasted a URL from a browser should learn that the two are kept
	// apart here.
	names := make([]string, 0, len(s.Fields))
	for n := range s.Fields {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		if s.Fields[n].Type != "url" {
			continue
		}
		v, ok := fields[n]
		if !ok || Str(v) == "" {
			continue
		}
		u, err := url.Parse(Str(v))
		if err == nil && u.User != nil {
			return &Refused{fmt.Sprintf("%s may not carry a login: put it in cred_username / cred_secret — a url field is in the snapshot, and the snapshot leaves the cluster", n)}
		}
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
