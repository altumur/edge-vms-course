package vmsplatform_test

// Lesson 5 — the second subsystem: a controller and a worker that count
// seconds, through the same platform code, with a different prefix. If this
// works, the VMS is a subsystem and not the platform.

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"

	"vmsserver/testbox"
	p "vmsserver/vmsplatform"
)

var counter = p.Subsystem{Name: "counter"}

// The counter's controller is not written: it is this spec, run by the
// platform's SpecController — the same type the VMS runs from
// vms.subsystem.yaml, with a different prefix, id rule and fields.
const counterSpec = `
name: counter
unit:
  rows: units
  id: name                     # named by the operator, not numbered
  fields:
    name: {type: string, required: true}
    step: {type: int, default: 1}
placement:
  capacity: {from: capacity, fallback: 10}     # no constraint: any worker
snapshot: [name, step]
`

func counterController(box *testbox.Box) *p.SpecController {
	v, err := p.ParseYAML(counterSpec)
	if err != nil {
		panic(err)
	}
	spec, err := p.SpecFromMap(v.(map[string]any))
	if err != nil {
		panic(err)
	}
	return p.NewSpecController(spec, box.Vars, box.Objects, 0, box.Wall.Now, "")
}

type counterWorker struct {
	*p.Worker
	values  map[string]int
	archive string
}

func (w *counterWorker) reconcileOnce() []string {
	a := w.Assignment()
	for _, unit := range a.Units {
		if _, held := w.Epochs[unit]; !held {
			w.TakeEpoch(unit)
		}
		items, _, _ := w.Vars.Get(counter.Config("units", unit))
		step, _ := strconv.Atoi(items["step"])
		w.values[unit] += step
		if w.values[unit]%10 == 0 { // an observation, into the counter's own bucket
			p.NewEventLog(w.archive, counter.Name, unit, w.Epochs[unit], 600).Append(w.Wall(), "round", map[string]any{"value": w.values[unit]})
		}
	}
	for unit := range w.values {
		if !a.Has(unit) {
			delete(w.values, unit)
			w.Release(unit)
		}
	}
	var units []string
	for u := range w.values {
		units = append(units, u)
	}
	sort.Strings(units)
	var status []map[string]any
	for _, u := range units {
		status = append(status, map[string]any{"id": u, "value": w.values[u], "phase": "counting"})
	}
	w.HeartbeatWith(status, nil)
	return units
}

func TestASecondSubsystemThroughTheSamePlatform(t *testing.T) {
	box := testbox.NewBox()
	ctl := counterController(box)
	w := &counterWorker{p.NewWorker(counter, box.Vars, box.Objects, p.WorkerOptions{Name: "c-1", Clock: box.Clock.Now, Wall: box.Wall.Now}), map[string]int{}, box.Archive}
	if r, err := ctl.Create(map[string]any{"name": "a", "step": 2}); err != nil || r.ID() != "a" {
		t.Fatal(r, err)
	}
	if r, err := ctl.Create(map[string]any{"name": "b", "step": 5}); err != nil || r.Int("revision") != 1 {
		t.Fatal(r, err)
	}
	for _, bad := range []map[string]any{{"name": "c", "worker": "c-1"}, {"step": 3}, {"name": "a", "step": 9}} {
		var refused *p.Refused
		if _, err := ctl.Create(bad); !errors.As(err, &refused) { // the platform's refusals, for free
			t.Fatal(bad, err)
		}
	}
	if u := ctl.Units(); len(u) != 2 || u[0].ID() != "a" || u[1].ID() != "b" {
		t.Fatal(u)
	}
	if r, _ := ctl.Update("b", map[string]any{"step": 5}); r.Int("revision") != 2 {
		t.Fatal(r)
	}
	w.HeartbeatWith(nil, map[string]any{"capacity": 10})
	if pls, _ := ctl.EnsurePlaced(nil); len(pls) != 2 || pls[0].Worker != "c-1" || !strings.Contains(ctl.Placement("a").Reason, "on ") { // placed like any unit
		t.Fatal(pls)
	}
	snap := ctl.Snapshot()["units"].([]map[string]any)[0]
	if snap["id"] != "a" || snap["step"] != 2 || snap["worker"] != "c-1" || snap["server"] != "?" {
		t.Fatal(snap)
	}
	ctl.Assign("c-1", []string{"a", "b"})
	if !reflect.DeepEqual(w.reconcileOnce(), []string{"a", "b"}) || !reflect.DeepEqual(w.reconcileOnce(), []string{"a", "b"}) {
		t.Fatal("units")
	}
	ep, _, _ := box.Vars.Get("counter/epoch/a")
	if w.values["a"] != 4 || w.values["b"] != 10 || ep["epoch"] != "1" {
		t.Fatal(w.values, ep)
	}
	st := ctl.WorkersSeen(45)["c-1"].Status
	if len(st) != 2 || st[0]["id"] != "a" || p.ToFloat(st[0]["value"]) != 4 || st[1]["phase"] != "counting" || p.ToFloat(st[1]["value"]) != 10 {
		t.Fatal(st)
	}
	ctl.Assign("c-1", []string{"b"})
	if u := w.reconcileOnce(); !reflect.DeepEqual(u, []string{"b"}) {
		t.Fatal(u)
	}
	if _, held := w.Epochs["a"]; held {
		t.Fatal("a released")
	}
	// its events sit on the same resource under its own prefix, written under its own epoch
	if s := p.SubsystemsUnder(box.Archive); !reflect.DeepEqual(s, map[string][]string{"counter": {"b"}}) {
		t.Fatal(s)
	}
	dir := filepath.Join(box.Archive, "counter", "b", "e1")
	ents, _ := os.ReadDir(dir)
	b := p.ReadBucket(filepath.Join(dir, ents[0].Name()))
	if len(b) != 1 || b[0].T() != box.Wall.Now() || b[0].Kind() != "round" || p.ToFloat(b[0]["value"]) != 10 {
		t.Fatal(b)
	}
	// the two subsystems do not see each other: prefixes, and nothing else
	v, _ := box.Vars.List("vms/")
	all, _ := box.Vars.List("counter/")
	var c []string
	for _, pth := range all {
		if !strings.Contains(pth, "/placement/") {
			c = append(c, pth)
		}
	}
	if len(v) != 0 || !reflect.DeepEqual(c, []string{"counter/epoch/a", "counter/epoch/b", "counter/units/a", "counter/units/b", "counter/workers/c-1"}) {
		t.Fatal(v, c)
	}
}
