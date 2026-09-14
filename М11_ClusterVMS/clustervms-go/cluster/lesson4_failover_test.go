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

func TestAServerGoneWithNowhereToRescheduleTheControllerMovesTheCameras(t *testing.T) {
	// One worker per server (distinct_hosts), count = the archive servers: when
	// srv-a dies there is no spare server for Nomad to put w-1 on, so nobody claims
	// the slot. Two silences from one server — the slot lapsed and stayed lapsed for
	// another lostAfter (Nomad's chance), and the resource on srv-a silent — are a
	// fact about the server, and the controller moves w-1's cameras to the worker
	// that is here. One silence — a crashed process, its resource still answering —
	// moves nothing: that process returns under the same name.
	c, ctl, a, actA := recording(t, 3)
	eq(t, ctl.Policy(), map[string]string{"servers": "shared"})                                                      // the default: a box is several workers on one server
	if p, err := ctl.SetPolicy(map[string]string{"servers": "distinct"}); err != nil || p["servers"] != "distinct" { // the administrator's choice, one row: vms/policy
		t.Fatal(p, err)
	}
	rs := c.resources(nil)
	actB := vms.NewFakeActuator()
	b := c.worker(t, 2, "srv-b", 0, actB) // the other server's worker, idle
	b.HeartbeatOnce()
	eq(t, len(ctl.Assignment("w-2").Units), 0)
	// a crash: w-1's process dies, srv-a's resource keeps heartbeating — Nomad restarts the process under the same name
	c.Wall.Advance(2*lostAfter + 3)
	b.HeartbeatOnce()
	for _, r := range rs {
		r.Heartbeat()
	}
	if !ctl.Slots()["w-1"].Lapsed(c.Wall.Now()) || len(ctl.GoneServers(45)) != 0 || len(ctl.Redistribute(nil)) != 0 { // one silence: left alone
		t.Fatal("one silence")
	}
	// the power pull: srv-a is gone — its worker and its resource both silent; nothing can claim w-1 (distinct_hosts)
	c.Wall.Advance(2*lostAfter + 3)
	b.HeartbeatOnce()
	rs["srv-b"].Heartbeat()
	rs["srv-c"].Heartbeat()
	eq(t, ctl.GoneServers(45), map[string]string{"w-1": "srv-a"})
	moves := ctl.Redistribute(nil)
	eq(t, len(moves), 3)
	for _, m := range moves {
		if m.From != "w-1" || m.To != "w-2" {
			t.Fatal(m)
		}
	}
	if r := ctl.Placement(1).Reason; !strings.HasPrefix(r, "server srv-a gone: slot w-1 lapsed and its resource silent; ") || !strings.HasSuffix(r, "; on srv-b") {
		t.Fatal(r)
	}
	eq(t, b.ReconcileOnce(), actions("start 1", "start 2", "start 3")) // the next epoch, on srv-b
	eq(t, actB.Epochs, map[int]int{1: 2, 2: 2, 3: 2})
	b.HeartbeatOnce()
	eq(t, len(ctl.Assignment("w-1").Units), 0)
	eq(t, ctl.Where(1), "w-2")
	// srv-a returns: its worker claims w-1 again, reads an empty assignment, records nothing; nothing moves back
	rs["srv-a"].Heartbeat()
	a2 := c.worker(t, 1, "srv-a", 0, nil)
	a2.HeartbeatOnce()
	eq(t, a2.Name, "w-1")
	eq(t, len(a2.ReconcileOnce()), 0)
	eq(t, len(ctl.Redistribute(nil)), 0)
	eq(t, ctl.ResourceState("srv-a", 45), "live")
	eq(t, ctl.Where(1), "w-2")                        // adding a place to record moves nothing
	eq(t, actA.Epochs, map[int]int{1: 1, 2: 1, 3: 1}) // the fenced instance's footage is intact under e1; B's under e2
	_ = a
	// under shared, the same two silences move nothing: the slot is Nomad's to reschedule, and its replacement inherits
	ctl.SetPolicy(map[string]string{"servers": "shared"})
	c.create(t, ctl, src(9))
	pls, _ := ctl.EnsurePlaced(nil)
	eq(t, pls[len(pls)-1].Worker, "w-1") // w-1, back on srv-a, has the most room
	c.Wall.Advance(2*lostAfter + 3)      // srv-a dies again, w-1 with it
	b.HeartbeatOnce()
	rs["srv-b"].Heartbeat()
	rs["srv-c"].Heartbeat()
	if !ctl.Slots()["w-1"].Lapsed(c.Wall.Now()) || len(ctl.GoneServers(45)) != 0 || len(ctl.Redistribute(nil)) != 0 {
		t.Fatal("shared: the controller waits for Nomad's replacement")
	}
	eq(t, ctl.Where(4), "w-1")
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
