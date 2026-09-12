// Package reconciler is М9 Lesson 6's loop, in Go.
//
// The port that Lesson 9 says is the whole rewrite: the schema, the loop's
// logic, the state machine, the backoff and jitter policy, and the
// desired/actual contract survive unchanged; only the actuator's
// implementation language changes. This file is the loop. Its tests are the
// Python suite's seven, line for line.
//
//	Desired state is persisted. Actual state is derived.
package reconciler

import (
	"math"
	"math/rand"
	"sort"
)

// Position is the vocabulary from Lesson 6, Step 7. Positions only —
// reasons live on the conditions axis (Lesson 9).
type Position string

const (
	Converged Position = "converged"
	Lagging   Position = "lagging"
	Stalled   Position = "stalled"
)

// Camera is one row of desired state: the operator-owned half of М9's
// cameras table, plus the controller-assigned revision.
type Camera struct {
	ID       int64
	Enabled  bool
	Revision int64
}

// Store hands the reconciler the desired state. It is read on every pass;
// the reconciler keeps nothing of it.
type Store interface {
	Desired() []Camera
}

// Actuator is verb -> success. "start", "restart", "stop". In М9 Lesson 7
// it is GStreamer; here and in the tests it is whatever you inject.
type Actuator func(verb string, cam Camera) bool

// Action is what one pass did.
type Action struct {
	Verb string
	ID   int64
}

type failure struct {
	N       int
	RetryAt float64
	Delay   float64
}

// Reconciler compares two sets and acts on the difference. It does not
// process events.
type Reconciler struct {
	store         Store
	actuator      Actuator
	actual        map[int64]int64 // camera id -> applied revision. IN MEMORY ONLY.
	failures      map[int64]failure
	MaxBackoff    float64
	StallFailures int
	rand          *rand.Rand
}

func New(store Store, actuator Actuator) *Reconciler {
	return &Reconciler{
		store:         store,
		actuator:      actuator,
		actual:        map[int64]int64{}, // a fresh process knows nothing and rediscovers everything
		failures:      map[int64]failure{},
		MaxBackoff:    60,
		StallFailures: 3,
		rand:          rand.New(rand.NewSource(rand.Int63())),
	}
}

// Reconcile runs one pass at time `now` (seconds, any monotonic origin —
// tests pass it explicitly so they control the clock).
func (r *Reconciler) Reconcile(now float64) []Action {
	desired := map[int64]Camera{}
	var order []int64
	for _, c := range r.store.Desired() {
		if c.Enabled {
			desired[c.ID] = c
			order = append(order, c.ID)
		}
	}
	sort.Slice(order, func(i, j int) bool { return order[i] < order[j] })

	var actions []Action
	for _, id := range order {
		cam := desired[id]
		have, running := r.actual[id]
		if running && have >= cam.Revision { // already applied: >=, not ==
			continue
		}
		if f, ok := r.failures[id]; ok && now < f.RetryAt { // in backoff, not yet
			continue
		}
		verb := "start"
		if running {
			verb = "restart"
		}
		if r.actuator(verb, cam) {
			r.actual[id] = cam.Revision
			delete(r.failures, id)
			actions = append(actions, Action{verb, id})
		} else {
			r.fail(id, now)
			actions = append(actions, Action{"failed", id})
		}
	}

	// The stop loop walks what we are RUNNING, not what is desired: you
	// cannot learn about a deletion by looking at rows that exist.
	var stops []int64
	for id := range r.actual {
		if _, want := desired[id]; !want {
			stops = append(stops, id)
		}
	}
	sort.Slice(stops, func(i, j int) bool { return stops[i] < stops[j] })
	for _, id := range stops {
		r.actuator("stop", Camera{ID: id})
		delete(r.actual, id)
		actions = append(actions, Action{"stop", id})
	}
	return actions
}

// fail schedules the next attempt: exponential, capped, and JITTERED —
// 50–100 % of the base — so two hundred cameras that failed together do
// not retry together.
func (r *Reconciler) fail(id int64, now float64) {
	n := r.failures[id].N + 1
	base := math.Min(math.Pow(2, float64(n)), r.MaxBackoff)
	delay := base * (0.5 + r.rand.Float64()*0.5)
	r.failures[id] = failure{N: n, RetryAt: now + delay, Delay: delay}
}

// Lost forgets a pipeline that died (bus error, watchdog) so the next pass
// restarts it, and counts the failure so backoff applies.
func (r *Reconciler) Lost(id int64, now float64) {
	delete(r.actual, id)
	r.fail(id, now)
}

// Status is camera id -> (position, lag).
type Status struct {
	Position Position
	Lag      int64
}

func (r *Reconciler) Status() map[int64]Status {
	out := map[int64]Status{}
	for _, cam := range r.store.Desired() {
		if !cam.Enabled {
			continue
		}
		lag := cam.Revision - r.actual[cam.ID]
		if lag < 0 {
			lag = 0
		}
		switch {
		case lag == 0:
			out[cam.ID] = Status{Converged, 0}
		case r.failures[cam.ID].N >= r.StallFailures:
			out[cam.ID] = Status{Stalled, lag}
		default:
			out[cam.ID] = Status{Lagging, lag}
		}
	}
	return out
}

// Failures exposes the backoff table for tests and the console (read-only copy).
func (r *Reconciler) Failures() map[int64]struct {
	N              int
	RetryAt, Delay float64
} {
	out := map[int64]struct {
		N              int
		RetryAt, Delay float64
	}{}
	for id, f := range r.failures {
		out[id] = struct {
			N              int
			RetryAt, Delay float64
		}{f.N, f.RetryAt, f.Delay}
	}
	return out
}

// Actual exposes the in-memory applied revisions (tests only).
func (r *Reconciler) Actual() map[int64]int64 {
	out := map[int64]int64{}
	for k, v := range r.actual {
		out[k] = v
	}
	return out
}

// Clear forgets every applied revision. This is what a fence does: the
// pipelines were stopped underneath the loop, so the next pass must see
// nothing running and try (and, gated, fail) to start each camera again.
func (r *Reconciler) Clear() {
	r.actual = map[int64]int64{}
}

// SetActual is the mistake, kept only so a test can make it on purpose.
// Do not call it from anything that ships.
func (r *Reconciler) SetActual(saved map[int64]int64) {
	r.actual = saved
}
