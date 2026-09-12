package vms

// vmsworker — DriverPack as the worker. One process, N pipelines, its own
// loop. It reads its assignment (vms/workers/<me>) and the camera rows it
// names, runs М9's reconcile loop over them, takes an epoch per camera by
// CAS when it starts one, holds a lease per camera, and publishes a
// heartbeat carrying its status. It never writes configuration.

import (
	"log"
	"os"
	"sort"
	"strconv"
	"sync"
	"time"

	p "vmsserver/vmsplatform"
)

var VMS = p.Subsystem{Name: "vms"}

// Posted is what an element posted on the bus about a camera.
type Posted struct {
	Cam    int
	Kind   string
	Fields map[string]any
}

// Actuator builds and tears down pipelines and drains their bus.
type Actuator interface {
	Actuate(verb string, cam Camera) bool
	Pump() (dead []int, posted []Posted)
	StopAll()
}

// FakeActuator is М9 Lesson 6's print(), with a memory. Failing says whose
// start fails. Tests push into Dead and Posted directly.
type FakeActuator struct {
	mu      sync.Mutex
	Failing func(cid int) bool
	Calls   []Action
	Running map[int]bool
	Epochs  map[int]int
	Dead    []int
	Posted  []Posted
}

func NewFakeActuator() *FakeActuator {
	return &FakeActuator{Running: map[int]bool{}, Epochs: map[int]int{}}
}

func FailingSet(ids ...int) func(int) bool {
	set := map[int]bool{}
	for _, i := range ids {
		set[i] = true
	}
	return func(cid int) bool { return set[cid] }
}

func (f *FakeActuator) Actuate(verb string, cam Camera) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.Calls = append(f.Calls, Action{verb, cam.ID})
	if verb == "stop" {
		delete(f.Running, cam.ID)
		return true
	}
	if f.Failing != nil && f.Failing(cam.ID) {
		delete(f.Running, cam.ID)
		return false
	}
	f.Running[cam.ID] = true
	f.Epochs[cam.ID] = cam.Epoch
	return true
}

func (f *FakeActuator) Pump() ([]int, []Posted) {
	f.mu.Lock()
	defer f.mu.Unlock()
	dead, posted := f.Dead, f.Posted
	f.Dead, f.Posted = nil, nil
	for _, cid := range dead {
		delete(f.Running, cid)
	}
	return dead, posted
}

// Post is what an element would post on the bus.
func (f *FakeActuator) Post(cid int, kind string, fields map[string]any) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.Posted = append(f.Posted, Posted{cid, kind, fields})
}

func (f *FakeActuator) StopAll() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.Running = map[int]bool{}
}

// RunningIDs is the sorted set of running cameras — the test's view.
func (f *FakeActuator) RunningIDs() []int {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := []int{}
	for cid := range f.Running {
		out = append(out, cid)
	}
	sort.Ints(out)
	return out
}

type Observed struct {
	Cam  int
	T    float64
	Kind string
}

// VmsWorkerOptions: zero values are the defaults.
type VmsWorkerOptions struct {
	p.WorkerOptions
	Server        string
	Capacity      int
	ArchiveRoot   string
	BucketSeconds int
}

// VmsWorker: name is a slot. Given (systemd's %i, Nomad's alloc index) it
// is claimed by that name; "" means whichever slot is free — a lapsed one
// first, so a replacement inherits its assignment.
type VmsWorker struct {
	*p.Worker
	ArchiveRoot      string
	BucketSeconds    int
	ObservedEvents   []Observed
	Capacity         int // cameras this process can carry: М9 Lesson 7's B + n·I, measured on its server
	Act              Actuator
	Rows             []Camera
	AssignmentRev    int
	Reconciler       *Reconciler
	RecordingAllowed bool
	FencedReason     string
	Server           string
	StartedAt        float64
	Passes           int
}

func NewVmsWorker(name string, vars p.Variables, objects p.ObjectStore, act Actuator, o VmsWorkerOptions) (*VmsWorker, error) {
	base := p.NewWorker(VMS, vars, objects, o.WorkerOptions)
	if _, err := base.ClaimSlot(name); err != nil {
		return nil, err
	}
	w := &VmsWorker{Worker: base, ArchiveRoot: o.ArchiveRoot, BucketSeconds: o.BucketSeconds, Capacity: o.Capacity,
		Act: act, RecordingAllowed: true, Server: o.Server}
	if w.ArchiveRoot == "" {
		w.ArchiveRoot = os.Getenv("ARCHIVE")
		if w.ArchiveRoot == "" {
			w.ArchiveRoot = "/data/archive"
		}
	}
	if w.BucketSeconds == 0 {
		w.BucketSeconds = 600
	}
	if w.Capacity == 0 {
		w.Capacity = 50
	}
	if w.Act == nil {
		w.Act = NewFakeActuator()
	}
	if w.Server == "" {
		w.Server = os.Getenv("NOMAD_NODE_ID")
		if w.Server == "" {
			w.Server, _ = os.Hostname()
		}
	}
	w.Reconciler = NewReconciler(w, w.actuate)
	w.StartedAt = w.Clock()
	return w, nil
}

// Desired is the store, as the reconciler sees it.
func (w *VmsWorker) Desired() []Camera { return w.Rows }

// Refresh reads the assignment and the rows it names. A fresh worker knows
// nothing and reads everything.
func (w *VmsWorker) Refresh() {
	a := w.Assignment()
	w.AssignmentRev = a.Rev
	rows := []Camera{}
	for _, unit := range a.Units {
		items, _, _ := w.Vars.Get(VMS.Config("cameras", unit))
		if items != nil && items["deleted"] != "true" {
			rows = append(rows, Row(items))
		}
	}
	w.Rows = rows
}

// the gate
func (w *VmsWorker) actuate(verb string, cam Camera) bool {
	unit := strconv.Itoa(cam.ID)
	if verb == "start" || verb == "restart" {
		if !w.RecordingAllowed {
			return false
		}
		if ep, held := w.Epochs[unit]; verb == "start" || !held {
			e, err := w.TakeEpoch(unit) // a new epoch for a new writer
			if err != nil {
				return false
			}
			cam.Epoch = e
		} else {
			cam.Epoch = ep
		}
		if !w.MayWrite(unit) {
			return false
		}
		return w.Act.Actuate(verb, cam)
	}
	ok := w.Act.Actuate("stop", cam)
	w.Release(unit)
	return ok
}

func (w *VmsWorker) Now() float64 { return w.Clock() - w.StartedAt }

func (w *VmsWorker) ReconcileOnce() []Action {
	return w.ReconcileAt(w.Now())
}

func (w *VmsWorker) ReconcileAt(now float64) []Action {
	w.Refresh()
	actions := w.Reconciler.Reconcile(now)
	w.Passes++
	return actions
}

// LeasePass renews every lease. A lost lease on a camera that is no longer
// assigned to me is a reassignment: let it go. A lost lease on a camera that
// IS still mine means another instance of ME took it: I am the zombie, and
// the whole instance fences.
func (w *VmsWorker) LeasePass() []string {
	if !w.RenewSlot() {
		w.Fence("slot " + w.Name + " is held by another instance now")
		var all []string
		for u := range w.Epochs {
			all = append(all, u)
		}
		sort.Strings(all)
		return all
	}
	lost := w.RenewLeases()
	if len(lost) == 0 {
		return lost
	}
	assigned := w.Assignment()
	for _, unit := range lost {
		if !assigned.Has(unit) {
			cid, _ := strconv.Atoi(unit)
			w.Act.Actuate("stop", Camera{ID: cid})
			delete(w.Reconciler.Actual, cid)
			w.Release(unit)
		} else {
			w.Fence("camera " + unit + ": a newer epoch was issued to another instance of " + w.Name)
			break
		}
	}
	return lost
}

func (w *VmsWorker) Fence(why string) {
	if !w.RecordingAllowed {
		return
	}
	log.Printf("%s: FENCED (%s). Stopping every pipeline.", w.Name, why)
	w.RecordingAllowed, w.FencedReason = false, why
	w.Act.StopAll()
	w.Reconciler.Clear()
}

// Observe: an event, written by this worker, now, into the camera's bucket
// on this server's resource, under the epoch this worker holds for it —
// recording or not. A camera it holds no epoch for is not its to observe.
// Returns "" when it was not.
func (w *VmsWorker) Observe(cid int, kind string, fields map[string]any) string {
	epoch, ok := w.Epochs[strconv.Itoa(cid)]
	if !ok || !w.RecordingAllowed {
		return ""
	}
	t := w.Wall()
	w.ObservedEvents = append(w.ObservedEvents, Observed{cid, t, kind})
	path, err := EventLogFor(w.ArchiveRoot, cid, epoch, w.BucketSeconds).Append(t, kind, fields)
	if err != nil {
		return ""
	}
	return path
}

// PumpOnce: the bus, drained — what elements posted becomes events (if I
// still hold the epoch), and what died becomes Lost and a `silent` event.
func (w *VmsWorker) PumpOnce() {
	dead, posted := w.Act.Pump()
	for _, ps := range posted {
		w.Observe(ps.Cam, ps.Kind, ps.Fields)
	}
	for _, cid := range dead {
		w.Reconciler.Lost(cid, w.Now())
		w.Observe(cid, "silent", nil) // the event with no segment open, by definition
	}
}

func (w *VmsWorker) Status() []map[string]any {
	st := w.Reconciler.Status()
	out := []map[string]any{}
	for _, cam := range w.Rows {
		pos, ok := st[cam.ID]
		if !ok {
			pos = Position{Converged, 0}
		}
		_, running := w.Reconciler.Actual[cam.ID]
		phase := "pending"
		switch {
		case running:
			phase = "running"
		case !cam.Enabled:
			phase = "pending"
		case w.Reconciler.Failures[cam.ID] != nil:
			phase = "failed"
		}
		out = append(out, map[string]any{"id": cam.ID, "ref": cam.Ref, "name": cam.Name, "enabled": cam.Enabled, "phase": phase,
			"position": pos.State, "revision": cam.Revision, "observed_revision": w.Reconciler.Actual[cam.ID],
			"epoch": w.Epochs[strconv.Itoa(cam.ID)]})
	}
	return out
}

// Headroom is what the autoscaler reads: cameras this worker could still take.
func (w *VmsWorker) Headroom() int {
	if h := w.Capacity - len(w.Rows); h > 0 {
		return h
	}
	return 0
}

func (w *VmsWorker) HeartbeatExtra() map[string]any {
	return map[string]any{"server": w.Server, "instance": w.Instance, "assignment_rev": w.AssignmentRev,
		"fenced": !w.RecordingAllowed, "conflicts": w.Conflicts(), "passes": w.Passes,
		"capacity": w.Capacity, "headroom": w.Headroom()}
}

func (w *VmsWorker) HeartbeatOnce() error {
	return w.HeartbeatWith(w.Status(), w.HeartbeatExtra())
}

// Run: one box — the loop as a process. Nomad or systemd restarts it.
// heartbeat is the function to publish with (a cluster worker adds fields).
func (w *VmsWorker) Run(poll time.Duration, stop <-chan struct{}, heartbeat func() error) {
	if heartbeat == nil {
		heartbeat = w.HeartbeatOnce
	}
	leaseEvery := (w.LeaseTTL - w.LeaseMargin) / 3
	if leaseEvery < 1 {
		leaseEvery = 1
	}
	var lastLease, lastHb float64
	for {
		select {
		case <-stop:
			w.Act.StopAll()
			heartbeat()
			w.ReleaseSlot() // an orderly stop says so; a crash says nothing
			return
		default:
		}
		w.ReconcileOnce()
		w.PumpOnce()
		if w.Clock()-lastLease >= leaseEvery {
			w.LeasePass()
			lastLease = w.Clock()
		}
		if w.Clock()-lastHb >= 10 {
			heartbeat()
			lastHb = w.Clock()
		}
		select {
		case <-stop:
		case <-time.After(poll):
		}
	}
}
