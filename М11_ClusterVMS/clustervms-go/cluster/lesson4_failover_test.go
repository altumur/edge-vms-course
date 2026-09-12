package cluster_test

// Lesson 4 — failover, and the two instances of one worker. The power pull
// on a fake clock; the measured number; the old instance waking up; and the
// reassignment window, which is the same window with a different verdict.

import (
	"strings"
	"testing"

	"clustervms/cluster"
	"vmsserver/vms"
)

const lostAfter = 45.0

func recording(t *testing.T, n int) (*Cluster, *cluster.ClusterController, *cluster.ClusterWorker, *vms.FakeActuator) {
	c := newCluster()
	ctl := c.controller(0, "")
	for i := 0; i < n; i++ {
		c.create(t, ctl, src(i))
	}
	act := vms.NewFakeActuator()
	a := c.worker(t, 1, "srv-a", 0, act)
	a.HeartbeatOnce()
	ctl.EnsurePlaced(nil)
	a.ReconcileOnce()
	a.HeartbeatOnce()
	want := []int{}
	for i := 1; i <= n; i++ {
		want = append(want, i)
		eq(t, act.Epochs[i], 1)
	}
	eq(t, act.RunningIDs(), want)
	return c, ctl, a, act
}

func TestThePowerPull(t *testing.T) {
	// Server A dies at t=0. Nomad's `disconnect { lost_after = 45s }` starts the
	// replacement on B at t=45 (+ a schedule). It claims w-1, reads its
	// assignment, takes the next epoch for every camera and records — asking nobody.
	c, ctl, a, _ := recording(t, 3)
	c.Wall.Advance(lostAfter + 3) // lost_after, then placement
	actB := vms.NewFakeActuator()
	b := c.worker(t, 1, "srv-b", 0, actB)
	eq(t, b.Name, "w-1")
	eq(t, b.PreviousInstance, a.Instance)
	eq(t, b.ReconcileOnce(), actions("start 1", "start 2", "start 3"))
	eq(t, actB.Epochs, map[int]int{1: 2, 2: 2, 3: 2})
	eq(t, b.Server, "srv-b")
	b.HeartbeatOnce()
	eq(t, ctl.FailoverSeconds(), map[string]float64{"w-1": 48.0}) // last heartbeat of A → B's start: the RTO this run
	eq(t, ctl.WorkersSeen(45)["w-1"].Extra["server"], "srv-b")
	eq(t, ctl.Where(1), "w-1") // nothing was rewritten
}

func TestTheOldInstanceWakesUpAndTheArchiveIsIntact(t *testing.T) {
	// Server A was not dead — partitioned, or paused. It comes back with w-1
	// still running epoch 1. Its next renewal finds the slot held by B: fenced
	// at the slot, and every epoch says the same. It stops.
	c, ctl, a, actA := recording(t, 3)
	c.Wall.Advance(lostAfter + 3)
	b := c.worker(t, 1, "srv-b", 0, nil)
	b.ReconcileOnce()
	a.LeasePass() // kill -CONT
	if a.RecordingAllowed || len(actA.RunningIDs()) != 0 || !strings.Contains(a.FencedReason, "slot w-1") {
		t.Fatal(a.FencedReason)
	}
	eq(t, a.RenewLeases(), []string{"1", "2", "3"}) // the resource-level token agrees, per camera
	eq(t, a.Conflicts(), 3)
	a.HeartbeatOnce()
	hb := ctl.WorkersSeen(1e12) // both wrote a heartbeat under one name...
	eq(t, hb["w-1"].Extra["fenced"], true)
	eq(t, hb["w-1"].Extra["server"], "srv-a")
	b.HeartbeatOnce()
	eq(t, ctl.WorkersSeen(45)["w-1"].Extra["fenced"], false) // ...and the live one wrote last
	eq(t, a.ReconcileOnce(), actions("failed 1", "failed 2", "failed 3"))
}

func TestTheReassignmentWindowIsTheSameWindowWithADifferentVerdict(t *testing.T) {
	// The controller moves camera 2 from w-1 to w-2. For up to TTL − margin both
	// may write — into different epochs. w-1 loses the lease on 2, sees it is no
	// longer assigned, lets it go; it is NOT a zombie and keeps 1 and 3.
	c, ctl, a, actA := recording(t, 3)
	actB := vms.NewFakeActuator()
	b := c.worker(t, 2, "srv-b", 0, actB)
	b.HeartbeatOnce()
	ctl.MoveTo(2, "w-2", "operator: srv-b sees that VLAN")
	eq(t, b.ReconcileOnce(), actions("start 2"))
	eq(t, actB.Epochs[2], 2) // the destination takes the next epoch
	eq(t, a.LeasePass(), []string{"2"})
	if !a.RecordingAllowed {
		t.Fatal("fenced")
	}
	eq(t, actA.RunningIDs(), []int{1, 3})
	eq(t, a.ReconcileOnce(), actions())
	eq(t, ctl.Where(2), "w-2")
}

func TestTheLeaseStopsWritingBeforeTheReplacementMayStart(t *testing.T) {
	// TTL 30, margin 5: the holder stops at 25 on its own clock; Nomad's
	// lost_after is 45. The window between 25 and 45 is the margin the design buys.
	c, _, a, _ := recording(t, 1)
	c.Clock.Advance(24)
	if !a.MayWrite("1") {
		t.Fatal("24")
	}
	c.Clock.Advance(2)
	if a.MayWrite("1") { // 26 s without a renewal: it stops itself
		t.Fatal("26")
	}
	eq(t, a.LeasePass(), []string{}) // renewal succeeds (nobody took the epoch)...
	if !a.MayWrite("1") {            // ...and it may write again — it was never fenced
		t.Fatal("renewed")
	}
}
