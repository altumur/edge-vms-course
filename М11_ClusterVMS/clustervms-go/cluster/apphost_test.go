package cluster

// The whole thing with fakes: prologue, the zombie fenced, the two numbers.

import (
	"fmt"
	"strings"
	"testing"

	"recorder/reconciler"
)

func actionsOf(a []reconciler.Action) string {
	var s []string
	for _, x := range a {
		s = append(s, fmt.Sprintf("%s %d", x.Verb, x.ID))
	}
	return strings.Join(s, ",")
}

func seenNode(t *testing.T, v *FakeVariables, objs ObjectStore, ids ...int64) Identity {
	cams := make([]CameraRow, 0, len(ids))
	for _, id := range ids {
		cams = append(cams, Cam(id, 2))
	}
	return seen(t, v, objs, "node-3", cams...)
}

func TestPrologueRestoresAndRecordsIntoANewEpoch(t *testing.T) {
	v, objs := world(t)
	clk, wall := &fakeClock{1000}, &fakeClock{10_000}
	ident := seenNode(t, v, objs, 7, 8)
	must(t, objs.Put("node-3/heartbeat", []byte(`{"ts": 9950.0, "epoch": 1}`))) // the old instance's last sign of life
	b := NewFakeClusterStore()
	act := NewFakeActuator()
	host := NewClusterAppHost(testSettings(), b, v, objs, ident, act, clk.now, wall.now)
	r, err := host.Prologue()
	must(t, err)
	if r.State != "restored" || r.Cameras != 2 || host.Settings.Epoch != 1 || act.Settings().Epoch != 1 {
		t.Fatalf("%+v epoch %d/%d", r, host.Settings.Epoch, act.Settings().Epoch)
	}
	acts, err := host.ReconcileOnce()
	must(t, err)
	if actionsOf(acts) != "start 7,start 8" {
		t.Fatal(actionsOf(acts))
	}
	if host.Failover["last"] != 50 || host.Failover["worst"] != 50 { // node_failover_seconds
		t.Fatalf("%v", host.Failover)
	}
	if fo, _, _ := v.Get("nodes/node-3/failover"); fo["worst"] != "50.0" {
		t.Fatalf("%v", fo)
	}
	must(t, host.ReportOnce())
	if c := b.Cond(7, "replicated"); !c.Status || c.Reason != "" {
		t.Fatalf("%+v", c)
	}
	if !strings.Contains(RenderMetrics(host), `node_epoch_conflicts{node="node-3"} 0`) {
		t.Fatal(RenderMetrics(host))
	}
}

func TestZombieIsFencedWhenTheReplacementTakesTheEpoch(t *testing.T) {
	v, objs := world(t)
	clk := &fakeClock{1000}
	ident := seenNode(t, v, objs, 7)
	actA, actB := NewFakeActuator(), NewFakeActuator()
	a := NewClusterAppHost(testSettings(), NewFakeClusterStore(), v, objs, ident, actA, clk.now, nil)
	_, err := a.Prologue()
	must(t, err)
	a.ReconcileOnce()
	if fmt.Sprint(actA.RunningIDs()) != "[7]" || a.Settings.Epoch != 1 {
		t.Fatal("A records into epoch 1")
	}
	// Nomad thinks A is lost; B starts on another server and takes epoch 2
	b := NewClusterAppHost(testSettings(), NewFakeClusterStore(), v, objs, ident, actB, clk.now, nil)
	_, err = b.Prologue()
	must(t, err)
	b.ReconcileOnce()
	if b.Settings.Epoch != 2 || fmt.Sprint(actB.RunningIDs()) != "[7]" {
		t.Fatal("B records into epoch 2")
	}
	// A wakes (SIGCONT) and renews its lease: the epoch moved. It fences itself.
	if a.Lease.Renew() {
		t.Fatal("A must lose its lease")
	}
	a.Fence("a newer epoch was issued")
	if len(actA.RunningIDs()) != 0 || a.RecordingAllowed {
		t.Fatal("A stopped everything")
	}
	acts, _ := a.ReconcileOnce()
	if actionsOf(acts) != "failed 7" { // it may start nothing
		t.Fatal(actionsOf(acts))
	}
	if !strings.Contains(RenderMetrics(a), `node_epoch_conflicts{node="node-3"} 1`) { // the counter moved
		t.Fatal(RenderMetrics(a))
	}
	if !strings.Contains(RenderMetrics(b), `node_epoch_conflicts{node="node-3"} 0`) {
		t.Fatal(RenderMetrics(b))
	}
	// B's lease is fine, and its archive path carries its own epoch
	if !b.Lease.Renew() || actB.Settings().Epoch != 2 {
		t.Fatal("B holds")
	}
}

func TestUnseenNodeRecordsNothingAndSaysSo(t *testing.T) {
	v, objs := world(t)
	host := NewClusterAppHost(testSettings(), NewFakeClusterStore(), v, objs, Identity{recorder: "node-9"}, NewFakeActuator(), nil, nil)
	r, err := host.Prologue()
	must(t, err)
	acts, _ := host.ReconcileOnce()
	if r.State != "unconfigured" || len(acts) != 0 {
		t.Fatal(r.State)
	}
	if host.Settings.Epoch != 1 { // it still holds an epoch: an operator may configure it
		t.Fatal("epoch")
	}
}

// М12: the console's camera list is built from this object, never from a call to the recorder.
func TestHeartbeatCarriesTheStatusSnapshot(t *testing.T) {
	v, objs := world(t)
	clk, wall := &fakeClock{1000}, &fakeClock{10_000}
	ident := seenNode(t, v, objs, 7, 8)
	host := NewClusterAppHost(testSettings(), NewFakeClusterStore(), v, objs, ident, NewFakeActuator(), clk.now, wall.now)
	_, err := host.Prologue()
	must(t, err)
	host.ReconcileOnce()
	hb := host.HeartbeatPayload()
	if hb.TS != 10_000 || hb.Epoch != 1 || hb.Server == "" || len(hb.Cameras) != 2 {
		t.Fatalf("%+v", hb)
	}
	if hb.Cameras[0].ID != 7 || hb.Cameras[0].Phase != "running" || hb.Cameras[0].Name != "cam7" || hb.Cameras[0].Site != "hq" {
		t.Fatalf("%+v", hb.Cameras[0])
	}
}
