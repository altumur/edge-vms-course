package vms

// The detector worker — the third subsystem's worker. A unit is one model on one camera (`7-linecross`);
// the worker subscribes to the camera's RTSP fan-out the way the gateway does (`live_url` from the VMS
// worker's heartbeat, never by calling it), decodes, runs the model, and writes what the model saw into
// the unit's bucket on the resource — `det/<unit>/e<epoch>/…events.jsonl`, under the epoch it holds, so a
// stale instance's events are identifiable like a stale writer's segments. Capacity is streams a GPU can
// carry; labels say where it may run.
//
//	det/units/<name>      the unit: cam, kind, params, enabled — written by the console with the operator's token
//	det/workers/<d>       the assignment, written by the det controller (SpecController from det.subsystem.yaml)
//	det/<d>/heartbeat     capacity, headroom, labels, url-less; per-unit status: phase, events written

import (
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"

	p "vmsserver/w2cplatform"
)

// DET is the subsystem's name — the keys its slot, epoch, assignment and heartbeat live under.
var DET = DetSpec.Sub()

// Model is what a detector runs: `Observe(now)` per pass, `Close()` at the end. Nothing in it knows where
// the frame behind that timestamp came from, which is what lets the same object serve a live detector, a
// scan of our archive and a survey of somebody else's.
type Model interface {
	Observe(now float64) []Observation
	Close()
}

// Observation is one thing a model saw: its kind, and whatever fields that kind carries.
type Observation struct {
	Kind   string
	Fields map[string]any
}

// FakeModel fires one event every `Every`th pass, with the pass number, so a test can count buckets and
// lines. A real one decodes and infers; this one is what the course runs without a GPU.
type FakeModel struct {
	Row    p.Row
	Every  int
	Passes int
}

func NewFakeModel(row p.Row) Model { return &FakeModel{Row: row, Every: 3} }

func (m *FakeModel) Observe(now float64) []Observation {
	m.Passes++
	if m.Every == 0 {
		m.Every = 3
	}
	if m.Passes%m.Every != 0 {
		return nil
	}
	cam, _ := strconv.Atoi(p.Str(m.Row["cam"]))
	return []Observation{{Kind: p.Str(m.Row["kind"]), Fields: map[string]any{"pass": m.Passes, "cam": cam}}}
}

func (m *FakeModel) Close() {}

// DetWorker is a worker of the `det` subsystem: N models against an assignment, each writing its own
// bucket under its own epoch.
type DetWorker struct {
	*p.Worker
	Models      map[string]func(p.Row) Model
	Capacity    int
	Server      string
	Labels      []string
	ArchiveRoot string

	running       map[string]Model
	statusByUnit  map[string]map[string]any
	EventsWritten int
}

// DetOptions are the knobs a test turns; zero values mean the defaults.
type DetOptions struct {
	Models      map[string]func(p.Row) Model
	Capacity    int
	Server      string
	Labels      []string
	ArchiveRoot string
	Worker      p.WorkerOptions
}

func NewDetWorker(name string, vars p.Variables, objects p.ObjectStore, o DetOptions) (*DetWorker, error) {
	wo := o.Worker
	wo.Name = ""
	w := p.NewWorker(DET, vars, objects, wo)
	if _, err := w.ClaimSlot(slotOr(name, "d")); err != nil {
		return nil, err
	}
	models := o.Models
	if models == nil {
		models = map[string]func(p.Row) Model{"motion": NewFakeModel, "linecross": NewFakeModel, "lpr": NewFakeModel}
	}
	capacity := o.Capacity
	if capacity == 0 {
		capacity = 8
	}
	labels := o.Labels
	if labels == nil {
		labels = []string{"gpu"}
	}
	root := o.ArchiveRoot
	if root == "" {
		root = "/data/archive"
	}
	server := o.Server
	if server == "" {
		server, _ = os.Hostname()
	}
	return &DetWorker{Worker: w, Models: models, Capacity: capacity, Server: server, Labels: labels,
		ArchiveRoot: root, running: map[string]Model{}, statusByUnit: map[string]map[string]any{}}, nil
}

func slotOr(name, prefix string) string {
	if name != "" {
		return name
	}
	return prefix + "-1"
}

// RTPSource: `(server, live_url)` of the worker holding the camera — its RTSP fan-out, on any server.
// Found in the HOLDER's heartbeat and never by calling the worker: one place publishes it, everybody reads.
func (d *DetWorker) RTPSource(cam string) (server, url string, ok bool) {
	held, found := p.HolderOf(d.Objects, "vms/", cam, d.Wall(), p.HolderQuery{Phase: "running", Field: "live_url"})
	if !found {
		return "", "", false
	}
	return held.HB.ExtraString("server", "?"), p.Str(held.Status["live_url"]), true
}

func (d *DetWorker) unitRow(unit string) p.Row {
	items, _, _ := d.Vars.Get(DET.Config("units", unit))
	if items == nil || items["deleted"] == "true" {
		return nil
	}
	return DetSpec.RowOf(items)
}

// ReconcileOnce: the running models equal the assignment's enabled, reachable units.
func (d *DetWorker) ReconcileOnce() []string {
	now := d.Wall()
	wanted := map[string]bool{}
	units := append([]string{}, d.Assignment().Units...)
	sort.Strings(units)
	for _, unit := range units {
		wanted[unit] = true
		row := d.unitRow(unit)
		if row == nil {
			continue
		}
		if !row.Bool("enabled") {
			d.stop(unit)
			d.statusByUnit[unit] = d.status(unit, row, "pending", "")
			continue
		}
		factory, known := d.Models[p.Str(row["kind"])]
		if !known {
			d.statusByUnit[unit] = d.status(unit, row, "unsupported", "")
			continue
		}
		server, source, ok := d.RTPSource(p.Str(row["cam"]))
		if !ok {
			d.stop(unit)
			d.statusByUnit[unit] = d.status(unit, row, "waiting", "camera held by nobody")
			continue
		}
		if _, up := d.running[unit]; !up {
			if _, have := d.Epochs[unit]; !have {
				if _, err := d.TakeEpoch(unit); err != nil { // one writer of det/<unit>/… at a time
					continue
				}
			}
			d.running[unit] = factory(row)
			st := d.status(unit, row, "running", "")
			st["server"], st["source"], st["events"] = server, source, 0
			d.statusByUnit[unit] = st
		}
		if !d.MayWrite(unit) {
			continue
		}
		for _, obs := range d.running[unit].Observe(now) {
			log := p.NewEventLog(d.ArchiveRoot, DET.Name, unit, d.Epochs[unit], 0)
			if _, err := log.Append(now, obs.Kind, obs.Fields); err != nil {
				continue
			}
			n, _ := d.statusByUnit[unit]["events"].(int)
			d.statusByUnit[unit]["events"] = n + 1
			d.EventsWritten++
		}
	}
	for unit := range d.running {
		if !wanted[unit] {
			d.stop(unit)
			d.Release(unit)
		}
	}
	for unit := range d.statusByUnit {
		if !wanted[unit] {
			delete(d.statusByUnit, unit)
		}
	}
	d.RenewLeases()
	out := make([]string, 0, len(d.running))
	for unit := range d.running {
		out = append(out, unit)
	}
	sort.Strings(out)
	return out
}

func (d *DetWorker) status(unit string, row p.Row, phase, why string) map[string]any {
	st := map[string]any{"id": unit, "cam": p.Str(row["cam"]), "kind": p.Str(row["kind"]), "phase": phase}
	if why != "" {
		st["why"] = why
	}
	return st
}

func (d *DetWorker) stop(unit string) {
	if m, ok := d.running[unit]; ok {
		m.Close()
		delete(d.running, unit)
	}
}

func (d *DetWorker) Headroom() int {
	if n := d.Capacity - len(d.running); n > 0 {
		return n
	}
	return 0
}

func (d *DetWorker) HeartbeatOnce() error {
	names := make([]string, 0, len(d.statusByUnit))
	for u := range d.statusByUnit {
		names = append(names, u)
	}
	sort.Strings(names)
	status := make([]map[string]any, 0, len(names))
	for _, u := range names {
		status = append(status, d.statusByUnit[u])
	}
	return d.HeartbeatWith(status, map[string]any{
		"server": d.Server, "instance": d.Instance, "labels": strings.Join(d.Labels, ","),
		"capacity": d.Capacity, "headroom": d.Headroom(), "conflicts": d.Conflicts(),
		"events": d.EventsWritten,
	})
}

// Status is what the tests read: the per-unit entries this worker would publish.
func (d *DetWorker) Status(unit string) map[string]any { return d.statusByUnit[unit] }

func (d *DetWorker) String() string { return fmt.Sprintf("detworker %s", d.Name) }
