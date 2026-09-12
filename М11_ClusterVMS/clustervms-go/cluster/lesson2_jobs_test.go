package cluster_test

// Lesson 2 — workers, resources and the controller as jobs. Identity by
// claim from NOMAD_ALLOC_INDEX; scale out and in; the duplicate index; the
// ACL from inside an allocation.

import (
	"errors"
	"strings"
	"testing"

	"clustervms/cluster"
	"vmsserver/vms"
)

func clusterWith(t *testing.T, nCams int) (*Cluster, *cluster.ClusterController) {
	c := newCluster()
	ctl := cluster.NewClusterController(c.Vars.AsWriter("vmscontroller", "vms/*"), c.Objects, 4, c.Wall.Now, "")
	for i := 0; i < nCams; i++ {
		c.create(t, ctl, src(i))
	}
	return c, ctl
}

func TestTheSlotComesFromTheAllocationIndexAndTheLabelsFromTheServer(t *testing.T) {
	c, ctl := clusterWith(t, 6)
	w0, w1 := c.worker(t, 0, "srv-a", 4, nil), c.worker(t, 1, "srv-b", 4, nil)
	eq(t, []string{w0.Name, w1.Name}, []string{"w-0", "w-1"})
	w0.HeartbeatOnce()
	w1.HeartbeatOnce()
	hb := ctl.WorkersSeen(45)
	eq(t, hb["w-0"].Extra["server"], "srv-a")
	eq(t, hb["w-0"].Extra["labels"], "vlan:cctv-a")
	eq(t, hb["w-1"].Extra["labels"], "vlan:cctv-a,vlan:cctv-b")
	eq(t, hb["w-1"].Extra["alloc"], "alloc-0002")
	eq(t, ctl.Slots()["w-1"].Holder, "alloc-0002") // the claim names the allocation
}

func TestNomadJobScaleOutThenIn(t *testing.T) {
	// `nomad job scale vmsworker 3`: a new allocation with index 2 claims w-2 and
	// the next camera lands on it. `… 2`: index 2 gets SIGTERM, releases, and its
	// cameras are redistributed in one placement pass. The controller asked for none of it.
	c, ctl := clusterWith(t, 8)
	ws := []*cluster.ClusterWorker{c.worker(t, 0, "srv-a", 4, nil), c.worker(t, 1, "srv-b", 4, nil)}
	for _, w := range ws {
		w.HeartbeatOnce()
	}
	ctl.EnsurePlaced(nil)
	for _, w := range ws {
		w.ReconcileOnce()
		w.HeartbeatOnce()
	}
	eq(t, ctl.Headroom(), 0)
	eq(t, ctl.Load("w-0")+ctl.Load("w-1"), 8) // what /metrics shows the autoscaler
	// scale out: the autoscaler saw avg(vms_worker_load) = 1.0
	c.create(t, ctl, src(9))
	placed, _ := ctl.EnsurePlaced(nil)
	if len(placed) == 0 || ctl.Where(9) != "" { // full: the ninth waits
		t.Fatal(placed)
	}
	w2 := c.worker(t, 2, "srv-c", 4, nil)
	w2.HeartbeatOnce()
	ctl.EnsurePlaced(nil)
	if w2.Name != "w-2" || ctl.Where(9) != "w-2" || !strings.Contains(ctl.Placement(9).Reason, "on srv-c") {
		t.Fatal(ctl.Placement(9))
	}
	// scale in: index 2 is stopped in order
	w2.ReconcileOnce()
	w2.ReleaseSlot()
	ctl.DeleteCamera(1)
	ctl.DeleteCamera(2) // room to move into
	moves := ctl.Redistribute(nil)
	if len(moves) != 1 || moves[0].Camera != 9 || moves[0].From != "w-2" || len(ctl.Assignment("w-2").Units) != 0 {
		t.Fatal(moves)
	}
}

func TestTwoAllocationsWithOneIndexResolveAtTheCAS(t *testing.T) {
	// Nomad issue #10727: a duplicate allocation index. The index is a label;
	// the slot row is the proof. The second claim wins; the first fences.
	c, ctl := clusterWith(t, 2)
	a := c.worker(t, 0, "srv-a", 0, nil)
	ctl.Assign("w-0", []string{"1", "2"})
	a.ReconcileOnce()
	b := c.worker(t, 0, "srv-b", 0, nil) // same index, a second allocation
	if b.Name != "w-0" || ctl.Slots()["w-0"].Holder != b.Instance || ctl.Slots()["w-0"].Gen != 2 {
		t.Fatal(ctl.Slots()["w-0"])
	}
	if lost := a.LeasePass(); len(lost) == 0 || a.RecordingAllowed || !strings.Contains(a.FencedReason, "slot w-0") {
		t.Fatal(a.FencedReason)
	}
	eq(t, b.ReconcileOnce(), actions("start 1", "start 2"))
	eq(t, b.LeasePass(), []string{})
}

func TestTheACLFromInsideAnAllocation(t *testing.T) {
	// A worker's token writes its epochs and its slot; the controller's writes vms/*.
	c, ctl := clusterWith(t, 1)
	w := c.worker(t, 0, "srv-a", 0, nil)
	if _, err := w.Vars.Put("vms/cameras/1", cluster.Items{"name": "tampered"}, cluster.NoCAS); !errors.Is(err, cluster.ErrForbidden) {
		t.Fatal(err)
	}
	w.TakeEpoch("1")
	ctl.UpdateCamera(1, map[string]any{"name": "ok"})
	ep, _, _ := c.Vars.Get("vms/epoch/1")
	if ctl.Camera(1).Name != "ok" || ep["epoch"] != "1" {
		t.Fatal(ep)
	}
	_ = vms.Converged
}
