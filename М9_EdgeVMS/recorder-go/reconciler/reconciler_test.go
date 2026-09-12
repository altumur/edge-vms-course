package reconciler

import (
	"reflect"
	"sort"
	"testing"
)

type fakeStore struct{ rows []Camera }

func (s *fakeStore) Desired() []Camera { return s.rows }

type fakeActuator struct {
	failing map[int64]bool
	failAll bool
	calls   []Action
	running map[int64]bool
}

func newActuator() *fakeActuator {
	return &fakeActuator{failing: map[int64]bool{}, running: map[int64]bool{}}
}

func (a *fakeActuator) fn() Actuator {
	return func(verb string, cam Camera) bool {
		a.calls = append(a.calls, Action{verb, cam.ID})
		if verb == "stop" {
			delete(a.running, cam.ID)
			return true
		}
		if a.failAll || a.failing[cam.ID] {
			delete(a.running, cam.ID)
			return false
		}
		a.running[cam.ID] = true
		return true
	}
}

func cam(id int64, rev int64) Camera { return Camera{ID: id, Enabled: true, Revision: rev} }

func eq(t *testing.T, got, want []Action) {
	t.Helper()
	if len(got) == 0 && len(want) == 0 {
		return
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func Test1ConvergeThenIdle(t *testing.T) {
	s, a := &fakeStore{[]Camera{cam(1, 1), cam(2, 1)}}, newActuator()
	r := New(s, a.fn())
	eq(t, r.Reconcile(0), []Action{{"start", 1}, {"start", 2}})
	eq(t, r.Reconcile(0), nil) // a converged loop is silent
	eq(t, r.Reconcile(0), nil)
	if len(a.calls) != 2 {
		t.Fatalf("actuator called %d times, want 2", len(a.calls))
	}
}

func Test2RevisionBumpRestarts(t *testing.T) {
	s, a := &fakeStore{[]Camera{cam(1, 1)}}, newActuator()
	r := New(s, a.fn())
	r.Reconcile(0)
	s.rows[0].Revision = 2
	eq(t, r.Reconcile(0), []Action{{"restart", 1}})
	eq(t, r.Reconcile(0), nil)
	if r.Actual()[1] != 2 {
		t.Fatal("applied revision not recorded")
	}
}

func Test3DisableAndDelete(t *testing.T) {
	s, a := &fakeStore{[]Camera{cam(1, 1), cam(2, 1)}}, newActuator()
	r := New(s, a.fn())
	r.Reconcile(0)
	s.rows[0].Enabled = false
	eq(t, r.Reconcile(0), []Action{{"stop", 1}})
	s.rows = s.rows[:1]
	eq(t, r.Reconcile(0), []Action{{"stop", 2}})
	if len(r.Actual()) != 0 {
		t.Fatal("actual not empty")
	}
	eq(t, r.Reconcile(0), nil)
}

func Test4RestartReDerivesActual(t *testing.T) {
	s := &fakeStore{[]Camera{cam(1, 1)}}
	r1 := New(s, newActuator().fn())
	r1.Reconcile(0)
	r2 := New(s, newActuator().fn()) // fresh process, empty actual
	if len(r2.Actual()) != 0 {
		t.Fatal("a fresh process must know nothing")
	}
	eq(t, r2.Reconcile(0), []Action{{"start", 1}}) // rebuilds from the store
}

func Test5PersistedActualIsACacheThatLies(t *testing.T) {
	s, a := &fakeStore{[]Camera{cam(1, 2)}}, newActuator()
	liar := New(s, a.fn())
	liar.SetActual(map[int64]int64{1: 2}) // "loaded from disk on startup"
	eq(t, liar.Reconcile(0), nil)         // it does nothing
	if liar.Status()[1].Position != Converged {
		t.Fatal("expected the lie: converged")
	}
	if len(a.running) != 0 {
		t.Fatal("nothing should be recording — that is the bug")
	}
}

func Test6BackoffWithJitterSpreads200Cameras(t *testing.T) {
	rows := make([]Camera, 200)
	for i := range rows {
		rows[i] = cam(int64(i), 1)
	}
	a := newActuator()
	a.failAll = true
	r := New(&fakeStore{rows}, a.fn())
	r.Reconcile(0)
	var retries []float64
	for _, f := range r.Failures() {
		retries = append(retries, f.RetryAt)
	}
	sort.Float64s(retries)
	spread := retries[len(retries)-1] - retries[0]
	if retries[0] < 1.0 || retries[len(retries)-1] > 2.0 { // base 2**1 = 2, jitter 50–100 %
		t.Fatalf("first retry outside 1.00–2.00s: %.3f..%.3f", retries[0], retries[len(retries)-1])
	}
	if spread <= 0.5 {
		t.Fatalf("retries bunched within %.3fs", spread)
	}
	eq(t, r.Reconcile(0.5), nil) // nobody before their retry_at
	if n := len(r.Reconcile(2.0)); n != 200 {
		t.Fatalf("expected 200 retries, got %d", n)
	}
	for _, f := range r.Failures() { // exponential: second failure's base is 4
		if f.Delay < 2.0 || f.Delay > 4.0 {
			t.Fatalf("second delay %.3f outside 2–4", f.Delay)
		}
	}
	t.Logf("200 cameras, first retry spread %.2f-%.2fs", retries[0], retries[len(retries)-1])
}

func Test7LaggingVsStalled(t *testing.T) {
	s, a := &fakeStore{[]Camera{cam(1, 1), cam(2, 1)}}, newActuator()
	a.failing[2] = true
	r := New(s, a.fn())
	r.StallFailures = 3
	r.Reconcile(0)
	if st := r.Status(); st[1] != (Status{Converged, 0}) || st[2] != (Status{Lagging, 1}) {
		t.Fatalf("status after one failure: %v", st)
	}
	now := 0.0
	for i := 0; i < 3; i++ {
		now = r.Failures()[2].RetryAt + 0.01
		r.Reconcile(now)
	}
	if r.Status()[2] != (Status{Stalled, 1}) {
		t.Fatalf("expected stalled, got %v", r.Status()[2])
	}
	r.Lost(1, now)
	if _, ok := r.Actual()[1]; ok || r.Status()[1].Position != Lagging {
		t.Fatal("a lost pipeline must be forgotten and reported lagging")
	}
}

func TestMaxBackoffCapsDelay(t *testing.T) {
	a := newActuator()
	a.failing[1] = true
	r := New(&fakeStore{[]Camera{cam(1, 1)}}, a.fn())
	r.MaxBackoff = 10
	now := 0.0
	for i := 0; i < 8; i++ {
		r.Reconcile(now)
		now = r.Failures()[1].RetryAt + 0.01
	}
	if r.Failures()[1].Delay > 10 {
		t.Fatalf("delay %.2f exceeds cap", r.Failures()[1].Delay)
	}
}
