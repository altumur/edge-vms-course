package vms_test

// Lesson 4 — vmsworker: М9's loop over an assignment; the epoch and the
// lease; the restart with the controller stopped; the zombie on one box.

import (
	"reflect"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/w2cplatform"
)

type fakeStore struct{ rows []vms.Camera }

func (s *fakeStore) Desired() []vms.Camera { return s.rows }

func cam(i, revision int, enabled bool) vms.Camera {
	id := strconv.Itoa(i) // a unit id is a string: `id: numeric` writes the number, it does not stay one
	return vms.Camera{ID: id, Name: "cam" + id, Source: "driverpack://file/cam" + id + ".mp4",
		Enabled: enabled, Priority: 100, Revision: revision}
}

func actions(a ...string) []vms.Action {
	out := []vms.Action{}
	for _, s := range a {
		verb, id, _ := strings.Cut(s, " ")
		out = append(out, vms.Action{Verb: verb, ID: id})
	}
	return out
}

func eq(t *testing.T, got, want any) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

// -- М9 Lesson 6's seven, unchanged in meaning ------------------------------------------

func TestConvergeThenIdle(t *testing.T) {
	r := vms.NewReconciler(&fakeStore{[]vms.Camera{cam(1, 1, true), cam(2, 1, true)}}, vms.NewFakeActuator().Actuate)
	eq(t, r.Reconcile(0), actions("start 1", "start 2"))
	eq(t, r.Reconcile(0), actions())
	eq(t, r.Reconcile(0), actions())
}

func TestRevisionBumpRestarts(t *testing.T) {
	store := &fakeStore{[]vms.Camera{cam(1, 1, true)}}
	r := vms.NewReconciler(store, vms.NewFakeActuator().Actuate)
	r.Reconcile(0)
	store.rows[0].Revision = 2
	eq(t, r.Reconcile(0), actions("restart 1"))
	eq(t, r.Reconcile(0), actions())
	eq(t, r.Actual["1"], 2)
}

func TestDisableAndDelete(t *testing.T) {
	store := &fakeStore{[]vms.Camera{cam(1, 1, true), cam(2, 1, true)}}
	r := vms.NewReconciler(store, vms.NewFakeActuator().Actuate)
	r.Reconcile(0)
	store.rows[0].Enabled = false
	eq(t, r.Reconcile(0), actions("stop 1"))
	store.rows = store.rows[:1]
	eq(t, r.Reconcile(0), actions("stop 2"))
	eq(t, len(r.Actual), 0)
}

func TestRestartReDerivesActual(t *testing.T) {
	store := &fakeStore{[]vms.Camera{cam(1, 1, true)}}
	vms.NewReconciler(store, vms.NewFakeActuator().Actuate).Reconcile(0)
	r2 := vms.NewReconciler(store, vms.NewFakeActuator().Actuate)
	eq(t, len(r2.Actual), 0)
	eq(t, r2.Reconcile(0), actions("start 1"))
}

func TestPersistedActualIsACacheThatLies(t *testing.T) {
	act := vms.NewFakeActuator()
	liar := vms.NewReconciler(&fakeStore{[]vms.Camera{cam(1, 2, true)}}, act.Actuate)
	liar.Actual = map[string]int{"1": 2} // "saved" from a previous life
	eq(t, liar.Reconcile(0), actions())
	eq(t, liar.Status()["1"].State, vms.Converged)
	eq(t, act.RunningIDs(), []string{})
}

func TestBackoffWithJitterSpreads200Cameras(t *testing.T) {
	var rows []vms.Camera
	for i := 0; i < 200; i++ {
		rows = append(rows, cam(i, 1, true))
	}
	act := vms.NewFakeActuator()
	act.Failing = func(string) bool { return true }
	r := vms.NewReconciler(&fakeStore{rows}, act.Actuate)
	r.Reconcile(0)
	var retries []float64
	for _, f := range r.Failures {
		retries = append(retries, f.RetryAt)
	}
	sort.Float64s(retries)
	if !(1.0 <= retries[0] && retries[len(retries)-1] <= 2.0 && retries[len(retries)-1]-retries[0] > 0.5) {
		t.Fatal(retries[0], retries[len(retries)-1])
	}
	eq(t, r.Reconcile(0.5), actions())
	eq(t, len(r.Reconcile(2.0)), 200)
}

func TestLaggingVsStalled(t *testing.T) {
	act := vms.NewFakeActuator()
	act.Failing = vms.FailingSet("2")
	r := vms.NewReconciler(&fakeStore{[]vms.Camera{cam(1, 1, true), cam(2, 1, true)}}, act.Actuate)
	r.Reconcile(0)
	eq(t, r.Status()["1"], vms.Position{vms.Converged, 0})
	eq(t, r.Status()["2"], vms.Position{vms.Lagging, 1})
	now := 0.0
	for i := 0; i < 3; i++ {
		now = r.Failures["2"].RetryAt + 0.01
		r.Reconcile(now)
	}
	eq(t, r.Status()["2"], vms.Position{vms.Stalled, 1})
	r.Lost("1", now)
	_, running := r.Actual["1"]
	eq(t, running, false)
	eq(t, r.Status()["1"].State, vms.Lagging)
}

// -- the worker over an assignment ---------------------------------------------------------

func boxWithCameras(t *testing.T, n int) (*testbox.Box, *vms.VmsController) {
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars, box.Objects, 50, box.Wall.Now)
	for i := 1; i <= n; i++ {
		if _, err := ctl.CreateCamera(map[string]any{"name": "cam" + strconv.Itoa(i), "source": "driverpack://file/cam" + strconv.Itoa(i) + ".mp4"}); err != nil {
			t.Fatal(err)
		}
	}
	return box, ctl
}

func worker(t *testing.T, box *testbox.Box, name string, act *vms.FakeActuator, o vms.VmsWorkerOptions) *vms.VmsWorker {
	t.Helper()
	o.Clock, o.Wall = box.Clock.Now, box.Wall.Now
	w, err := vms.NewVmsWorker(name, box.Vars, box.Objects, act, o)
	if err != nil {
		t.Fatal(err)
	}
	return w
}

func TestWorkerRunsItsAssignmentAndTakesAnEpochPerCamera(t *testing.T) {
	box, ctl := boxWithCameras(t, 2)
	act := vms.NewFakeActuator()
	w := worker(t, box, "w-1", act, vms.VmsWorkerOptions{Server: "srv-1"})
	eq(t, w.ReconcileOnce(), actions()) // unassigned: it invents nothing
	ctl.Assign("w-1", []string{"1", "2"})
	eq(t, w.ReconcileOnce(), actions("start 1", "start 2"))
	eq(t, act.Epochs, map[string]int{"1": 1, "2": 1})
	if !w.MayWrite("1") || !w.MayWrite("2") {
		t.Fatal("may write")
	}
	ep, _, _ := box.Vars.Get("vms/epoch/1")
	eq(t, ep, p.Items{"epoch": "1"})
	ctl.UpdateCamera("1", map[string]any{"name": "gate"}) // an edit: revision 2
	eq(t, w.ReconcileOnce(), actions("restart 1"))
	eq(t, act.Epochs["1"], 1)        // a restart keeps its epoch
	ctl.Assign("w-1", []string{"2"}) // camera 1 reassigned away
	eq(t, w.ReconcileOnce(), actions("stop 1"))
	if _, held := w.Epochs["1"]; held {
		t.Fatal("released")
	}
	w.HeartbeatOnce()
	hb := ctl.WorkersSeen(45)["w-1"]
	if len(hb.Status) != 1 || p.ToFloat(hb.Status[0]["id"]) != 2 || hb.Status[0]["phase"] != "running" || hb.Extra["server"] != "srv-1" {
		t.Fatal(hb)
	}
}

func TestRestartWithTheControllerStopped(t *testing.T) {
	// The controller is never on the recovery path: a fresh worker reads its
	// assignment and records; nothing is asked of anyone.
	box, ctl := boxWithCameras(t, 3)
	ctl.Assign("w-1", []string{"1", "2", "3"})
	w1 := worker(t, box, "w-1", vms.NewFakeActuator(), vms.VmsWorkerOptions{})
	w1.ReconcileOnce()
	ctl = nil // the controller is gone
	act2 := vms.NewFakeActuator()
	w2 := worker(t, box, "w-1", act2, vms.VmsWorkerOptions{}) // kill -9, restart
	eq(t, len(w2.Reconciler.Actual), 0)                       // a fresh process knows nothing
	eq(t, w2.ReconcileOnce(), actions("start 1", "start 2", "start 3"))
	eq(t, act2.Epochs, map[string]int{"1": 2, "2": 2, "3": 2}) // the next epoch for each: the old instance is fenced by construction
}

func TestTheZombieOnOneBox(t *testing.T) {
	// Two instances of w-1 given the same assignment: the second takes the slot
	// and the next epochs; the first fences itself on renewal and stops everything.
	box, ctl := boxWithCameras(t, 1)
	ctl.Assign("w-1", []string{"1"})
	aAct, bAct := vms.NewFakeActuator(), vms.NewFakeActuator()
	a := worker(t, box, "w-1", aAct, vms.VmsWorkerOptions{})
	a.ReconcileOnce()
	eq(t, aAct.RunningIDs(), []string{"1"})
	eq(t, aAct.Epochs["1"], 1)
	b := worker(t, box, "w-1", bAct, vms.VmsWorkerOptions{}) // the replacement
	b.ReconcileOnce()
	eq(t, bAct.RunningIDs(), []string{"1"})
	eq(t, bAct.Epochs["1"], 2)
	eq(t, a.LeasePass(), []string{"1"})                                                                     // A wakes, renews, fences
	if a.RecordingAllowed || len(aAct.RunningIDs()) != 0 || !strings.Contains(a.FencedReason, "slot w-1") { // fenced at the slot first...
		t.Fatal(a.FencedReason)
	}
	eq(t, a.RenewLeases(), []string{"1"}) // ...and the camera's epoch says the same
	eq(t, a.Conflicts(), 1)
	eq(t, a.ReconcileOnce(), actions("failed 1")) // it may start nothing
	eq(t, b.LeasePass(), []string{})
	eq(t, bAct.RunningIDs(), []string{"1"}) // B is fine
}

func TestAReplacementWithoutANameInheritsTheLapsedSlot(t *testing.T) {
	// Nomad started `count = 2` workers and nobody told them their names. One
	// dies; its replacement claims whatever is free — the lapsed slot first —
	// and records the dead one's cameras from the assignment, asking nobody.
	box, ctl := boxWithCameras(t, 4)
	a := worker(t, box, "", vms.NewFakeActuator(), vms.VmsWorkerOptions{})
	b := worker(t, box, "", vms.NewFakeActuator(), vms.VmsWorkerOptions{})
	eq(t, []string{a.Name, b.Name}, []string{"w-1", "w-2"})
	ctl.Assign("w-1", []string{"1", "2"})
	ctl.Assign("w-2", []string{"3", "4"})
	a.ReconcileOnce()
	b.ReconcileOnce()
	box.Wall.Advance(46) // A is dead: its slot lapsed, its cameras are listed on w-1
	b.LeasePass()        // B is alive and renews
	act := vms.NewFakeActuator()
	c := worker(t, box, "", act, vms.VmsWorkerOptions{}) // the replacement alloc
	eq(t, c.Name, "w-1")                                 // not w-3: the lapsed slot, and with it the assignment
	eq(t, c.ReconcileOnce(), actions("start 1", "start 2"))
	eq(t, act.Epochs, map[string]int{"1": 2, "2": 2})
	if a.RenewSlot() { // A, wherever it is, is fenced at the slot
		t.Fatal("A")
	}
	eq(t, c.LeasePass(), []string{}) // C is fine
}

func TestTheZombieIsFencedAtTheSlotFirst(t *testing.T) {
	box, ctl := boxWithCameras(t, 1)
	ctl.Assign("w-1", []string{"1"})
	a := worker(t, box, "w-1", vms.NewFakeActuator(), vms.VmsWorkerOptions{})
	a.ReconcileOnce()
	b := worker(t, box, "w-1", vms.NewFakeActuator(), vms.VmsWorkerOptions{}) // same index, new alloc
	eq(t, b.Slot.Gen, 2)
	eq(t, a.LeasePass(), []string{"1"})
	if !strings.Contains(a.FencedReason, "slot w-1") {
		t.Fatal(a.FencedReason)
	}
	eq(t, b.LeasePass(), []string{})
}

func TestTheWorkerObservesWhatItHoldsRecordingOrNot(t *testing.T) {
	// An event is written by the worker that holds the camera's epoch, into the
	// camera's bucket on this server's resource. Not recording is not a reason;
	// not holding it is. A lost pipeline writes `silent`.
	box, ctl := boxWithCameras(t, 2)
	ctl.Assign("w-1", []string{"1"})
	act := vms.NewFakeActuator()
	w := worker(t, box, "w-1", act, vms.VmsWorkerOptions{ArchiveRoot: box.Archive})
	eq(t, w.Observe("1", "motion", nil), "") // no epoch held yet: not mine to observe
	w.ReconcileOnce()
	pth := w.Observe("1", "motion", map[string]any{"zone": "gate"})
	if pth == "" || !strings.HasPrefix(pth, box.Archive+"/vms/1/e1") || p.ReadBucket(pth)[0]["zone"] != "gate" {
		t.Fatal(pth)
	}
	eq(t, w.Observe("2", "motion", nil), "")               // camera 2 is not assigned to me
	act.Post("1", "person", map[string]any{"score": 0.91}) // an element posted on the bus...
	w.PumpOnce()                                           // ...and the worker, holding the epoch, made it a line
	act.Dead = []string{"1"}                               // the pipeline died
	w.PumpOnce()
	var kinds []string
	for _, e := range p.ReadBucket(pth) {
		kinds = append(kinds, e.Kind())
	}
	eq(t, kinds, []string{"motion", "person", "silent"})
	_, running := w.Reconciler.Actual["1"]
	eq(t, running, false)
	eq(t, p.ToFloat(p.ReadBucket(pth)[1]["score"]), 0.91)
	w.Fence("test")
	act.Post("1", "motion", nil)
	w.PumpOnce()
	eq(t, len(p.ReadBucket(pth)), 3) // a fenced instance's bus still posts; Observe drops it
	l, _ := box.Vars.List("vms/events")
	eq(t, len(l), 0)
	eq(t, len(ctl.WorkersSeen(45)), 0) // nobody was told; nothing went to the store
}

func TestAReassignmentIsNotAZombie(t *testing.T) {
	// The same lease loss, but the camera is no longer mine: let it go quietly.
	box, ctl := boxWithCameras(t, 1)
	ctl.Assign("w-1", []string{"1"})
	w1 := worker(t, box, "w-1", vms.NewFakeActuator(), vms.VmsWorkerOptions{})
	w1.ReconcileOnce()
	ctl.MoveTo("1", "w-2", "operator asked")
	w2 := worker(t, box, "w-2", vms.NewFakeActuator(), vms.VmsWorkerOptions{})
	w2.ReconcileOnce()
	eq(t, w1.LeasePass(), []string{"1"})
	if !w1.RecordingAllowed || len(w1.Reconciler.Actual) != 0 { // released, not fenced
		t.Fatal("fenced")
	}
	eq(t, w1.ReconcileOnce(), actions())
}

func TestLeaseExpiryWithoutRenewalStopsStarts(t *testing.T) {
	box, ctl := boxWithCameras(t, 1)
	ctl.Assign("w-1", []string{"1"})
	act := vms.NewFakeActuator()
	w := worker(t, box, "w-1", act, vms.VmsWorkerOptions{WorkerOptions: p.WorkerOptions{LeaseTTL: 30, LeaseMargin: 5}})
	w.ReconcileOnce()
	box.Clock.Advance(26)
	if w.MayWrite("1") {
		t.Fatal("expired")
	}
	w.Reconciler.Lost("1", w.Now()) // the pipeline died meanwhile
	box.Clock.Advance(5)            // past its backoff
	eq(t, w.ReconcileOnce(), actions("start 1"))
	eq(t, act.Epochs["1"], 2) // a start takes a fresh epoch and lease
}

// countingObjects counts the writes, so a test can ask HOW MANY heartbeats went out rather than whether
// one did. Everything else is the real store underneath.
type countingObjects struct {
	p.ObjectStore
	mu   sync.Mutex
	puts map[string]int
}

func (c *countingObjects) Put(key string, data []byte) error {
	c.mu.Lock()
	c.puts[key]++
	c.mu.Unlock()
	return c.ObjectStore.Put(key, data)
}

func (c *countingObjects) count(key string) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.puts[key]
}

func TestTheFirstHeartbeatDoesNotWaitForTheFirstTick(t *testing.T) {
	// A worker nobody can see is a worker nothing is placed on.
	//
	// Both loops heartbeat every ten seconds, and for a long time the FIRST one here waited for that
	// tick: Monotonic() counts from process start, so `clock() - 0 >= 10` is false on the first pass.
	// Python's time.monotonic() counts from boot, where the same line is true — one loop, two behaviours,
	// and a restarted Go worker invisible to the controller, its cameras unplaced, for ten seconds.
	//
	// Neither suite could see that. It takes a real worker of one language against a real controller of
	// the other, which is what ../../vmsserver/tests/test_cross_go_worker.py does, and where it was
	// found. This is the same claim, kept here where it is cheap to check.
	//
	// Counted rather than timed, and on a clock that never advances: an orderly stop heartbeats too, so
	// one pass with the announcement is two heartbeats and one without it is one.
	box := testbox.NewBox()
	objects := &countingObjects{ObjectStore: box.Objects, puts: map[string]int{}}
	clock := testbox.NewClock(0) // counts from process start — the case that used to be silent
	w, err := vms.NewVmsWorker("w-1", box.Vars, objects, vms.NewFakeActuator(),
		vms.VmsWorkerOptions{Server: "srv-1", WorkerOptions: p.WorkerOptions{Clock: clock.Now, Wall: box.Wall.Now}})
	if err != nil {
		t.Fatal(err)
	}

	stop, done := make(chan struct{}), make(chan struct{})
	go func() { defer close(done); w.Run(time.Millisecond, stop) }()
	time.Sleep(50 * time.Millisecond) // many passes; the clock does not move, so no tick is due
	close(stop)
	<-done

	if n := objects.count("vms/heartbeats/w-1"); n != 2 {
		t.Fatalf("heartbeats: %d — want the announcement before the loop and the orderly stop's own", n)
	}
	if raw, _ := box.Objects.Get("vms/heartbeats/w-1"); raw == nil {
		t.Fatal("nothing in the store")
	}
}
