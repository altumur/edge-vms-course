package cluster_test

// Lesson 1 — the platform's stores become Nomad's. М10's contract against
// the cluster's fakes: the same ModifyIndex and CAS, the same ACL, the same
// base types — nothing in vms/ notices.

import (
	"errors"
	"sort"
	"sync"
	"testing"

	"clustervms/cluster"
	p "vmsserver/psimplatform"
)

func TestCASIsTheSamePromiseAsTheFilesMade(t *testing.T) {
	v := cluster.NewFakeVariables()
	idx, _ := v.Put("vms/cameras/1", cluster.Items{"name": "gate"}, 0)
	if _, err := v.Put("vms/cameras/1", cluster.Items{"name": "gate 2"}, idx); err != nil {
		t.Fatal(err)
	}
	if _, err := v.Put("vms/cameras/1", cluster.Items{"name": "stale"}, idx); !errors.Is(err, cluster.ErrConflict) {
		t.Fatal("must conflict")
	}
	items, _, _ := v.Get("vms/cameras/1")
	l, _ := v.List("vms/")
	eq(t, items, cluster.Items{"name": "gate 2"})
	eq(t, l, []string{"vms/cameras/1"})
}

func TestOneWriterPerPrefixIsAnACLPolicy(t *testing.T) {
	v := cluster.NewFakeVariables()
	ctl := v.AsWriter("vmscontroller", "vms/*")
	wrk := v.AsWriter("vmsworker", "vms/epoch/*", "vms/slots/*")
	if _, err := ctl.Put("vms/cameras/7", cluster.Items{"name": "x"}, cluster.NoCAS); err != nil {
		t.Fatal(err)
	}
	if _, err := wrk.Put("vms/epoch/7", cluster.Items{"epoch": "1"}, cluster.NoCAS); err != nil {
		t.Fatal(err)
	}
	if _, err := wrk.Put("vms/cameras/7", cluster.Items{"name": "y"}, cluster.NoCAS); !errors.Is(err, cluster.ErrForbidden) {
		t.Fatal("forbidden")
	}
}

func TestTheEpochIssuerUnderFourGoroutinesOnTheRaftFake(t *testing.T) {
	v := cluster.NewFakeVariables()
	var mu sync.Mutex
	var wg sync.WaitGroup
	var got []int
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 50; j++ {
				e, _, _ := p.NextEpoch(v, "vms/epoch/7")
				mu.Lock()
				got = append(got, e)
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	sort.Ints(got)
	for i, e := range got {
		if e != i+1 {
			t.Fatal(got)
		}
	}
	eq(t, len(got), 200)
}

func TestM10sBaseTypesRunOnTheClusterStoresUnchanged(t *testing.T) {
	c := newCluster()
	sub := p.Subsystem{Name: "thing"}
	ctl := p.NewController(sub, c.Vars, c.Objects, c.Wall.Now)
	w := p.NewWorker(sub, c.Vars, c.Objects, p.WorkerOptions{Clock: c.Clock.Now, Wall: c.Wall.Now, Instance: "A"})
	if n, _ := w.ClaimSlot(""); n != "w-1" {
		t.Fatal(n)
	}
	w.HeartbeatWith([]map[string]any{{"id": 1, "phase": "running"}}, map[string]any{"server": "srv-a"})
	eq(t, sortedKeys(ctl.WorkersSeen(45)), []string{"w-1"})
	eq(t, ctl.WorkersSeen(45)["w-1"].Extra["server"], "srv-a")
	ctl.Assign("w-1", []string{"1"})
	eq(t, w.Assignment().Units, []string{"1"})
	if e, _ := w.TakeEpoch("1"); e != 1 || !w.MayWrite("1") {
		t.Fatal(e)
	}
}

func TestTheObjectStoreOnThisClusterIsVariables(t *testing.T) {
	// Heartbeats and the snapshot are ~10 KB every ten seconds from a dozen
	// processes: not the volume the keep-raft-small rule was about. The
	// contract is the point — nothing in vms/ knows which store it is talking to.
	v := cluster.NewFakeVariables()
	objects := cluster.NewVariablesObjectStore(v.AsWriter("vmsworker", "objects/*", "vms/epoch/*", "vms/slots/*"), "")
	sub := p.Subsystem{Name: "vms"}
	c := newCluster()
	w := p.NewWorker(sub, v, objects, p.WorkerOptions{Name: "w-1", Clock: c.Clock.Now, Wall: c.Wall.Now})
	w.HeartbeatWith([]map[string]any{{"id": 7, "phase": "running"}}, map[string]any{"server": "srv-a"})
	l, _ := v.List("objects/")
	ol, _ := objects.List("vms/")
	eq(t, l, []string{"objects/vms/w-1/heartbeat"})
	eq(t, ol, []string{"vms/w-1/heartbeat"})
	ctl := p.NewController(sub, v, objects, c.Wall.Now)
	eq(t, ctl.WorkersSeen(45)["w-1"].Extra["server"], "srv-a")
	err := cluster.NewVariablesObjectStore(v.AsWriter("vmsworker", "vms/epoch/*"), "").Put("vms/w-2/heartbeat", []byte("{}"))
	if !errors.Is(err, cluster.ErrForbidden) { // the ACL comes with the token, as for every Variable
		t.Fatal(err)
	}
}
