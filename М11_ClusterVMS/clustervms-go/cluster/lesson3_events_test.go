package cluster_test

// Lesson 3, second half — events. Written by the worker holding a unit's
// epoch into the unit's bucket on its server's resource; any subsystem, its
// own prefix; indexed by a job that is a cache and proves it by being
// deleted and rebuilt; unavailable — by name — when the resource is, never
// lost; a detector's event about camera 7 found by a field.

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"clustervms/cluster"
	"vmsserver/vms"
	p "vmsserver/vmsplatform"
)

func media(t *testing.T, c *Cluster, server string, cam, epoch int, start float64) {
	srv := c.Servers[server]
	pth := vms.SegmentPath(srv.Spool, cam, epoch, time.Unix(int64(start), 0).UTC())
	os.MkdirAll(filepath.Dir(pth), 0o755)
	os.WriteFile(pth, []byte(strings.Repeat("x", 1000)), 0o644)
	touch(pth, start+bucketSeconds)
	if _, err := srv.Resource.Promote(pth, 0); err != nil {
		t.Fatal(err)
	}
}

func evs(r p.QueryResult) []string {
	var out []string
	for _, e := range r.Events {
		out = append(out, e.Subsystem+"/"+e.Unit+"/"+e.Kind+"/"+e.Server+"/"+p.Str(e.Epoch)+"/"+p.Str(e.Fenced))
	}
	return out
}

func TestEventsAreIndexedAcrossResourcesAndSubsystemsAndTheIndexIsACache(t *testing.T) {
	c := newCluster()
	tt := c.Wall.Now() - 3600
	media(t, c, "srv-a", 7, 3, tt)
	observe(c, "srv-a", "vms", "7", 3, tt+12, "motion", map[string]any{"zone": "gate"}) // the VMS worker, recording camera 7
	observe(c, "srv-a", "vms", "7", 3, tt+40, "silent", nil)
	observe(c, "srv-b", "vms", "7", 4, tt+1205, "motion", nil)                                     // after a failover: next epoch, other server
	observe(c, "srv-c", "det", "d-12", 1, tt+30, "person", map[string]any{"cam": 7, "score": 0.9}) // a detector on a GPU server, ABOUT camera 7
	observe(c, "srv-c", "counter", "a", 1, tt+5, "round", map[string]any{"value": 10})             // a third subsystem, its own prefix
	hbs := c.resources(nil)
	eq(t, p.ResourcesSeen(c.Objects)["srv-c"].Units, map[string][]string{"counter": {"a"}, "det": {"d-12"}})
	idx := p.NewEventIndex(dirReader{c}, c.Wall.Now)
	rep := idx.Rebuild(p.ResourcesSeen(c.Objects))
	eq(t, rep, p.IndexReport{Added: 5, Unreachable: []string{}, FromMirror: []string{}, Segments: 4})
	eq(t, idx.State, "live")
	q := idx.Query(p.Query{T0: tt, T1: tt + 3600, Cam: p.IntPtr(7), CurrentEpochs: map[[2]string]int{{"vms", "7"}: 4}})
	eq(t, evs(q), []string{"vms/7/motion/srv-a/3/true", "det/d-12/person/srv-c/1/false", "vms/7/silent/srv-a/3/true", "vms/7/motion/srv-b/4/false"})
	if p.ToFloat(q.Events[1].Fields["score"]) != 0.9 || !strings.HasPrefix(q.Events[1].Bucket, "det/d-12/e1/") { // found by the field; it lives in ITS bucket
		t.Fatal(q.Events[1])
	}
	cnt := idx.Query(p.Query{T0: tt, T1: tt + 3600, Subsystem: p.StrPtr("counter")})
	if len(cnt.Events) != 1 || p.ToFloat(cnt.Events[0].Fields["value"]) != 10 {
		t.Fatal(cnt)
	}
	mo := idx.Query(p.Query{T0: tt, T1: tt + 3600, Kind: p.StrPtr("motion")})
	if len(mo.Events) != 2 || !mo.Events[0].CamIs(7) || !mo.Events[1].CamIs(7) {
		t.Fatal(mo)
	}
	// the index is a cache: a new instance after a failover rebuilds to the same answer from the resources alone
	idx2 := p.NewEventIndex(dirReader{c}, c.Wall.Now)
	idx2.Rebuild(p.ResourcesSeen(c.Objects))
	eq(t, evs(idx2.Query(p.Query{T0: tt, T1: tt + 3600})), evs(idx.Query(p.Query{T0: tt, T1: tt + 3600})))
	// tail: a new bucket on srv-c
	observe(c, "srv-c", "det", "d-12", 1, tt+700, "person", map[string]any{"cam": 9})
	hbs["srv-c"].Heartbeat()
	eq(t, idx.Tail(p.ResourcesSeen(c.Objects)).Added, 1)
}

func TestADeadResourceMakesTheAnswerIncompleteByNameNotWrong(t *testing.T) {
	c := newCluster()
	tt := c.Wall.Now() - 3600
	observe(c, "srv-a", "vms", "7", 3, tt+12, "motion", nil)
	observe(c, "srv-b", "vms", "8", 1, tt+20, "motion", nil)
	hbs := c.resources(nil)
	c.Wall.Advance(60)
	hbs["srv-b"].Heartbeat()
	hbs["srv-c"].Heartbeat()                         // srv-a went silent
	idx := p.NewEventIndex(dirReader{c}, c.Wall.Now) // a fresh eventindex, after a failover of its own
	rep := idx.Rebuild(p.ResourcesSeen(c.Objects))
	eq(t, rep.Added, 1)
	eq(t, rep.Unreachable, []string{"srv-a"})
	eq(t, idx.State, "live; srv-a unreachable")
	q := idx.Query(p.Query{T0: tt, T1: tt + 3600})
	if len(q.Events) != 1 || !q.Events[0].CamIs(8) { // srv-a's events are unavailable, and the state says so
		t.Fatal(q)
	}
	hbs["srv-a"].Heartbeat()
	eq(t, idx.Tail(p.ResourcesSeen(c.Objects)).Added, 1)
	eq(t, idx.State, "live") // back with its disks: indexed, not rebuilt
	// retention on srv-b removed a bucket: the index forgets, by (server, path)
	b := p.BucketsUnder(c.Servers["srv-b"].Archive, "vms", "8", bucketSeconds)[0]
	eq(t, idx.Forget("srv-b", []string{b.Path}), 1)
	q = idx.Query(p.Query{T0: tt, T1: tt + 3600})
	if len(q.Events) != 1 || !q.Events[0].CamIs(7) {
		t.Fatal(q)
	}
	vl, _ := c.Vars.List("vms/events")
	ol, _ := c.Objects.List("vms/events")
	if len(vl) != 0 || len(ol) != 0 { // no controller, no database, wrote an event
		t.Fatal(vl, ol)
	}
}

func TestTheResourcePolicyRetainsEachSubsystemsBucketsByItsOwnRow(t *testing.T) {
	c := newCluster()
	ctl := c.controller(0, "")
	srv := c.Servers["srv-a"]
	c.create(t, ctl, map[string]any{"source": "driverpack://file/7.mp4", "retention_days": 1, "events_retention_days": 30})
	rt, _, _ := c.Vars.Get("vms/retention/1")
	eq(t, rt, cluster.Items{"days": "30"}) // the VMS's policy for its unit, as a row the platform reads
	now := c.Wall.Now()
	p1 := observe(c, "srv-a", "vms", "1", 1, now-40*86400, "motion", nil)    // older than the VMS's policy
	p2 := observe(c, "srv-a", "vms", "1", 1, now-3600, "motion", nil)        // recent
	p3 := observe(c, "srv-a", "det", "d-1", 1, now-400*86400, "person", nil) // another subsystem: a year by default
	for _, pth := range []string{p1, p2, p3} {
		touch(pth, now-100)
	}
	res := cluster.ClusterResource(srv.Resource, "srv-a", "http://srv-a", c.Vars, c.Objects, c.Wall.Now, nil)
	rep := res.Pass()
	_, e1 := os.Stat(p1)
	_, e2 := os.Stat(p2)
	_, e3 := os.Stat(p3)
	if rep["vms.added"] != 2 || rep["removed"] != 2 || e2 != nil || e1 == nil || e3 == nil {
		t.Fatal(rep)
	}
	eq(t, len(vms.NewManifest(srv.Archive, 1).Buckets()), 2)                                   // the VMS's lines: its pass ran before the platform removed the file...
	if res.Pass()["vms.dropped"] != 1 || len(vms.NewManifest(srv.Archive, 1).Buckets()) != 1 { // ...and drops it on the next pass
		t.Fatal("dropped")
	}
}

func TestTheEventsKnobIsAPeerCopyAndTheOwnerRestores(t *testing.T) {
	// The storage knob's events row, as a copy between resources — no store in
	// between. Off: a silent server's events are unavailable by name. On: each
	// resource copied its CLOSED buckets to the next live resource after it, and
	// a fresh index answers completely from the peer, saying so. Back with an
	// empty disk, the owner pulls its buckets home; nobody else ever writes them.
	all := []string{"srv-a", "srv-b", "srv-c"}
	eq(t, p.PeersOf("srv-a", all, 1), []string{"srv-b"})
	eq(t, p.PeersOf("srv-c", all, 1), []string{"srv-a"})
	eq(t, p.PeersOf("srv-b", all, 2), []string{"srv-c", "srv-a"})
	eq(t, p.PeersOf("srv-a", []string{"srv-a"}, 1), []string{})
	c := newCluster()
	tt := c.Wall.Now() - 7200
	rd := dirReader{c}
	observe(c, "srv-a", "vms", "7", 3, tt+12, "motion", map[string]any{"zone": "gate"})
	observe(c, "srv-a", "det", "d-12", 1, tt+30, "person", map[string]any{"cam": 7})
	observe(c, "srv-a", "vms", "7", 3, tt+6800, "motion", nil) // in the OPEN bucket: not closed, not mirrored
	observe(c, "srv-b", "vms", "8", 1, tt+20, "motion", nil)
	pol := c.resources(rd)
	if pol["srv-a"].Pass()["enabled"] != false || len(p.MirroredBuckets(c.Servers["srv-b"].Archive, "srv-a", bucketSeconds)) != 0 { // knob off: nothing leaves
		t.Fatal("knob")
	}
	c.Vars.Put(p.MirrorKey, cluster.Items{"enabled": "true", "copies": "1"}, cluster.NoCAS) // the knob: one Variable
	r := pol["srv-a"].Pass()
	eq(t, r["mirrored"], 2)
	eq(t, r["peers"], []string{"srv-b"}) // a -> b, closed buckets only
	r = pol["srv-b"].Pass()
	eq(t, r["mirrored"], 1)
	eq(t, r["peers"], []string{"srv-c"})      // b -> c
	eq(t, pol["srv-a"].Pass()["mirrored"], 0) // exactly once: the peer said what it holds
	for _, hb := range pol {
		hb.Heartbeat()
	}
	eq(t, p.ResourcesSeen(c.Objects)["srv-b"].Mirrors, map[string]int{"srv-a": 2})
	eq(t, p.ResourcesSeen(c.Objects)["srv-c"].Mirrors, map[string]int{"srv-b": 1})
	if !strings.HasPrefix(p.MirroredBuckets(c.Servers["srv-b"].Archive, "srv-a", bucketSeconds)[0].Path, "det/d-12/e1/") { // the ORIGINAL path, under .mirror/srv-a/
		t.Fatal("path")
	}
	if _, ok := p.SubsystemsUnder(c.Servers["srv-b"].Archive)[".mirror"]; ok {
		t.Fatal(".mirror")
	}
	// srv-a dies
	c.Wall.Advance(60)
	pol["srv-b"].Heartbeat()
	pol["srv-c"].Heartbeat()
	idx := p.NewEventIndex(rd, c.Wall.Now) // a fresh index after its own failover
	rep := idx.Rebuild(p.ResourcesSeen(c.Objects))
	eq(t, rep.Unreachable, []string{})
	eq(t, rep.FromMirror, []string{"srv-a"})
	eq(t, rep.Added, 3)
	eq(t, idx.State, "live; srv-a from mirror")
	ev := idx.Query(p.Query{T0: tt, T1: tt + 7200, Cam: p.IntPtr(7)}).Events
	eq(t, evs(p.QueryResult{Events: ev}), []string{"vms/7/motion/srv-a/3/false", "det/d-12/person/srv-a/1/false"}) // complete; the open bucket is the RPO
	// srv-a returns — with a REPLACED, empty disk
	os.RemoveAll(c.Servers["srv-a"].Archive)
	os.MkdirAll(c.Servers["srv-a"].Archive, 0o755)
	pol["srv-a"].Heartbeat()
	r = pol["srv-a"].Restore()
	eq(t, r["pulled"], 2)    // its two closed buckets are home;
	eq(t, r["vms.added"], 1) // the vms manifest line rebuilt
	bs := p.BucketsUnder(c.Servers["srv-a"].Archive, "vms", "7", bucketSeconds)
	if len(bs) != 1 || bs[0].Path != ev[0].Bucket {
		t.Fatal(bs)
	}
	pol["srv-a"].Heartbeat()
	rep = idx.Tail(p.ResourcesSeen(c.Objects))
	eq(t, rep.Added, 0)
	eq(t, rep.FromMirror, []string{})
	eq(t, idx.State, "live") // seen already; the resource is the source again
	ol, _ := c.Objects.List("platform/mirror")
	vl, _ := c.Vars.List("vms/mirror")
	if len(ol) != 0 || len(vl) != 0 { // no store in between, ever; and not the VMS's knob
		t.Fatal(ol, vl)
	}
}
