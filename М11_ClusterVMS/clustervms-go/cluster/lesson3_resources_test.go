package cluster_test

// Lesson 3 — what stays on the server, and what does not. Configuration is
// already in raft (the RPO is zero); footage stays on the resource; the
// manifest returns with it; a timeline spans two resources and names the
// one that is unreachable.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"clustervms/cluster"
	p "vmsserver/psimplatform"
	"vmsserver/vms"
)

func TestAnEditDuringTheFailoverIsSimplyThere(t *testing.T) {
	// The RPO inside the cluster is zero: the controller's write went into raft,
	// and the replacement worker reads raft.
	c := newCluster()
	ctl := c.controller(0, "")
	c.create(t, ctl, map[string]any{"source": "driverpack://file/1.mp4", "name": "before"})
	a := c.worker(t, 1, "srv-a", 0, nil)
	a.HeartbeatOnce()
	ctl.EnsurePlaced(nil)
	a.ReconcileOnce()
	c.Wall.Advance(20)                                                        // srv-a is gone; w-1 is between instances
	ctl.UpdateCamera(1, map[string]any{"name": "edited during the failover"}) // acknowledged after the CAS commit: it is in raft
	b := c.worker(t, 1, "srv-b", 0, nil)                                      // the replacement
	eq(t, b.ReconcileOnce(), actions("start 1"))
	eq(t, b.Rows[0].Name, "edited during the failover")
	if raw, _ := c.Objects.Get("vms/config"); raw != nil { // nothing was published for this to work
		t.Fatal("published")
	}
}

func TestATimelineSpansTwoResourcesAndNamesTheUnreachableOne(t *testing.T) {
	c := newCluster()
	c.controller(0, "")
	tt := c.Wall.Now()
	segment(t, c.Servers["srv-a"], 7, 3, tt-1200, 600, 1000) // before the failure, on A
	segment(t, c.Servers["srv-a"], 7, 3, tt-600, 600, 1000)
	segment(t, c.Servers["srv-b"], 7, 4, tt-300, 600, 1000) // after, on B, next epoch
	hbs := c.resources(nil)
	seen := p.ResourcesSeen(c.Objects)
	eq(t, seen["srv-a"].Units, map[string][]string{"rec": {"7"}}) // media + the manifest, the recorder's tree
	eq(t, seen["srv-c"].Units, map[string][]string{})
	if seen["srv-a"].Usage <= 2000 { // media + the manifest
		t.Fatal(seen["srv-a"].Usage)
	}
	tl := cluster.MergedTimeline(seen, dirReader{c}, 7, tt-2000, tt, 4, c.Wall.Now(), 45)
	var got []string
	for _, s := range tl.Segments {
		got = append(got, s.Server+"/"+p.Str(s.Epoch)+"/"+p.Str(s.Fenced))
	}
	eq(t, got, []string{"srv-a/3/true", "srv-a/3/true", "srv-b/4/false"})
	eq(t, tl.Unreachable, []string{})
	// srv-a dies: its heartbeat goes stale; its ranges are unavailable, and the answer says so by name
	c.Wall.Advance(60)
	hbs["srv-b"].Heartbeat()
	hbs["srv-c"].Heartbeat()
	tl = cluster.MergedTimeline(p.ResourcesSeen(c.Objects), dirReader{c}, 7, tt-2000, tt, 4, c.Wall.Now(), 45)
	if len(tl.Segments) != 1 || tl.Segments[0].Server != "srv-b" {
		t.Fatal(tl.Segments)
	}
	eq(t, tl.Unreachable, []string{"srv-a"})
	if !strings.Contains(tl.Note, "unavailable until the server returns") || !strings.Contains(tl.Note, "not lost") {
		t.Fatal(tl.Note)
	}
	// srv-a returns: its manifest came back with its disks — nothing was rebuilt
	hbs["srv-a"].Heartbeat()
	tl = cluster.MergedTimeline(p.ResourcesSeen(c.Objects), dirReader{c}, 7, tt-2000, tt, 4, c.Wall.Now(), 45)
	if len(tl.Segments) != 3 || len(tl.Unreachable) != 0 {
		t.Fatal(tl)
	}
}

func TestTheResourcePolicyNeedsNeitherWorkerNorController(t *testing.T) {
	c := newCluster()
	ctl := c.controller(0, "")
	c.create(t, ctl, map[string]any{"source": "driverpack://file/1.mp4"})
	p.NewSpecController(vms.RecSpec, c.Vars, c.Objects, 0, c.Wall.Now, "").Create(map[string]any{"cam": "1", "retention_days": 1}) // retention is the RECORDING's row
	srv := c.Servers["srv-a"]
	tt := c.Wall.Now()
	segment(t, srv, 1, 1, tt-3*86400, 600, 1000)
	segment(t, srv, 1, 1, tt-3600, 600, 1000)
	os.Remove(filepath.Join(srv.Archive, vms.NewManifest(srv.Archive, 1).Read()[1].Path)) // a file gone behind the manifest's back
	rep := cluster.ClusterResource(srv.Resource, "srv-a", "http://srv-a", c.Vars, c.Objects, c.Wall.Now, nil).Pass()
	eq(t, rep, map[string]any{"rec.added": 0, "rec.dropped": 1, "rec.media_removed": 1, "removed": 0, "enabled": false, "mirrored": 0, "peers": []string{}})
	eq(t, len(vms.NewManifest(srv.Archive, 1).Read()), 0)
}

func TestAWorkerWithNoAssignmentInventsNothing(t *testing.T) {
	c := newCluster()
	w := c.worker(t, 5, "srv-c", 0, nil)
	eq(t, w.ReconcileOnce(), actions())
	eq(t, w.Name, "w-5")
	w.HeartbeatOnce()
	raw, _ := c.Objects.Get("vms/w-5/heartbeat")
	var hb map[string]any
	json.Unmarshal(raw, &hb)
	if len(hb["status"].([]any)) != 0 || hb["headroom"] != 50.0 {
		t.Fatal(hb)
	}
}
