package vms

// М9 Lesson 6 — the reconcile loop, with nothing in it. The same >=, the
// same stop loop over what is RUNNING, the same jitter.
//
//	Desired state is persisted. Actual state is derived.

import (
	"math"
	"math/rand"
	"sort"
)

const (
	Converged = "converged"
	Lagging   = "lagging"
	Stalled   = "stalled"
)

type Store interface{ Desired() []Camera }

// ActuatorFunc: the verb, the camera (with Epoch set by the gate) -> ok.
type ActuatorFunc func(verb string, cam Camera) bool

type Action struct {
	Verb string
	ID   string
}

type Failure struct {
	N       int
	RetryAt float64
	Delay   float64
}

type Position struct {
	State string
	Lag   int
}

type Reconciler struct {
	Store         Store
	Actuator      ActuatorFunc
	Actual        map[string]int // unit id -> revision   IN MEMORY ONLY
	Failures      map[string]*Failure
	MaxBackoff    float64
	StallFailures int
}

func NewReconciler(store Store, act ActuatorFunc) *Reconciler {
	return &Reconciler{store, act, map[string]int{}, map[string]*Failure{}, 60, 3}
}

func (r *Reconciler) Reconcile(now float64) []Action {
	desired := map[string]Camera{}
	var order []string
	for _, c := range r.Store.Desired() {
		if c.Enabled {
			desired[c.ID] = c
			order = append(order, c.ID)
		}
	}
	actions := []Action{}
	for _, cid := range order {
		cam := desired[cid]
		have, running := r.Actual[cid]
		if running && have >= cam.Revision {
			continue
		}
		if f := r.Failures[cid]; f != nil && now < f.RetryAt {
			continue
		}
		verb := "start"
		if running {
			verb = "restart"
		}
		if r.Actuator(verb, cam) {
			r.Actual[cid] = cam.Revision
			delete(r.Failures, cid)
			actions = append(actions, Action{verb, cid})
		} else {
			r.fail(cid, now)
			actions = append(actions, Action{"failed", cid})
		}
	}
	var running []string
	for cid := range r.Actual { // the stop loop walks what is RUNNING
		running = append(running, cid)
	}
	sort.Strings(running)
	for _, cid := range running {
		if _, ok := desired[cid]; !ok {
			r.Actuator("stop", Camera{ID: cid})
			delete(r.Actual, cid)
			actions = append(actions, Action{"stop", cid})
		}
	}
	return actions
}

func (r *Reconciler) fail(cid string, now float64) {
	n := 1
	if f := r.Failures[cid]; f != nil {
		n = f.N + 1
	}
	base := math.Min(math.Pow(2, float64(n)), r.MaxBackoff)
	delay := base * (0.5 + rand.Float64()*0.5)
	r.Failures[cid] = &Failure{n, now + delay, delay}
}

func (r *Reconciler) Lost(cid string, now float64) {
	delete(r.Actual, cid)
	r.fail(cid, now)
}

// Clear is what a fence does: the pipelines were stopped underneath the loop.
func (r *Reconciler) Clear() { r.Actual = map[string]int{} }

func (r *Reconciler) Status() map[string]Position {
	out := map[string]Position{}
	for _, cam := range r.Store.Desired() {
		if !cam.Enabled {
			continue
		}
		lag := cam.Revision - r.Actual[cam.ID]
		if lag < 0 {
			lag = 0
		}
		switch {
		case lag == 0:
			out[cam.ID] = Position{Converged, 0}
		case r.Failures[cam.ID] != nil && r.Failures[cam.ID].N >= r.StallFailures:
			out[cam.ID] = Position{Stalled, lag}
		default:
			out[cam.ID] = Position{Lagging, lag}
		}
	}
	return out
}
