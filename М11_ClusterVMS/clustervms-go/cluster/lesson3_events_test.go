package cluster_test

// Lesson 3, second half — events. Written by the worker holding a unit's
// epoch into the unit's bucket on its server's resource; any subsystem, its
// own prefix; held in a database by EACH RESOURCE over its own tree — a cache
// that proves it by being deleted and rebuilt — and merged by the console,
// which holds none; unavailable — by name — when the resource is, never lost;
// a detector's event about camera 7 found by a field.

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"clustervms/cluster"
	p "vmsserver/w2cplatform"
	"vmsserver/vms"
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

func TestEventsAreIndexedByEachResourceAndMergedByTheConsoleWhichHoldsNone(t *testing.T) {
	c := newCluster()
	tt := c.Wall.Now() - 3600
	media(t, c, "srv-a", 7, 3, tt)
	observe(c, "srv-a", "vms", "7", 3, tt+12, "motion", map[string]any{"zone": "gate"}) // the VMS worker, recording camera 7
	observe(c, "srv-a", "vms", "7", 3, tt+40, "silent", nil)
	observe(c, "srv-b", "vms", "7", 4, tt+1205, "motion", nil)                                     // after a failover: next epoch, other server
	observe(c, "srv-c", "det", "d-12", 1, tt+30, "person", map[string]any{"cam": 7, "score": 0.9}) // a detector on a GPU server, ABOUT camera 7
	observe(c, "srv-c", "lpr", "lane-1", 1, tt+5, "plate", map[string]any{"plate": "AB123"})       // a third subsystem, its own prefix
	rs := c.resources(nil)
	eq(t, p.ResourcesSeen(c.Objects)["srv-c"].Units, map[string][]string{"det": {"d-12"}, "lpr": {"lane-1"}})
	// one database per resource, over its own tree only — nothing cluster-wide
	eq(t, rs["srv-a"].Database.Rebuild(), p.DBReport{Added: 2, Segments: 1, Mirrored: []string{}})
	eq(t, rs["srv-b"].Database.Rebuild().Added, 1)
	eq(t, rs["srv-c"].Database.Rebuild(), p.DBReport{Added: 2, Segments: 2, Mirrored: []string{}})
	eq(t, rs["srv-a"].Database.State, "live")
	for _, e := range rs["srv-a"].Database.Query(p.Query{T0: tt, T1: tt + 3600}).Events {
		eq(t, e.Server, "srv-a")
	}
	// the console merges by time, and fences by the epochs only the cluster's rows know
	m := c.merged(rs)
	q := m.Query(p.Query{T0: tt, T1: tt + 3600, Cam: p.IntPtr(7), CurrentEpochs: map[[2]string]int{{"vms", "7"}: 4}})
	eq(t, q.State, "live")
	eq(t, evs(q), []string{"vms/7/motion/srv-a/3/true", "det/d-12/person/srv-c/1/false", "vms/7/silent/srv-a/3/true", "vms/7/motion/srv-b/4/false"})
	if p.ToFloat(q.Events[1].Fields["score"]) != 0.9 || !strings.HasPrefix(q.Events[1].Bucket, "det/d-12/e1/") { // found by the field; it lives in ITS bucket
		t.Fatal(q.Events[1])
	}
	lp := m.Query(p.Query{T0: tt, T1: tt + 3600, Subsystem: p.StrPtr("lpr")})
	if len(lp.Events) != 1 || lp.Events[0].Fields["plate"] != "AB123" {
		t.Fatal(lp)
	}
	mo := m.Query(p.Query{T0: tt, T1: tt + 3600, Kind: p.StrPtr("motion")})
	if len(mo.Events) != 2 || !mo.Events[0].CamIs(7) || !mo.Events[1].CamIs(7) {
		t.Fatal(mo)
	}
	// the database is a cache: a resource job restarted rebuilds to the same answer from its tree alone
	before := evs(rs["srv-a"].Database.Query(p.Query{T0: tt, T1: tt + 3600}))
	again := p.NewEventDatabase(c.Servers["srv-a"].Archive, "srv-a", c.Wall.Now, bucketSeconds)
	eq(t, again.Rebuild().Added, 2)
	eq(t, evs(again.Query(p.Query{T0: tt, T1: tt + 3600})), before)
	// tail: a new bucket on srv-c is on the merged timeline after srv-c's next tail — nobody else is told
	observe(c, "srv-c", "det", "d-12", 1, tt+700, "person", map[string]any{"cam": 9})
	eq(t, rs["srv-c"].Database.Tail().Added, 1)
	eq(t, len(m.Query(p.Query{T0: tt, T1: tt + 3600, Kind: p.StrPtr("person")}).Events), 2)
}

func TestADeadResourceMakesTheAnswerIncompleteByNameNotWrong(t *testing.T) {
	c := newCluster()
	tt := c.Wall.Now() - 3600
	observe(c, "srv-a", "vms", "7", 3, tt+12, "motion", nil)
	observe(c, "srv-b", "vms", "8", 1, tt+20, "motion", nil)
	rs := c.resources(nil)
	for _, r := range rs {
		r.Database.Rebuild()
	}
	c.Wall.Advance(60)
	rs["srv-b"].Heartbeat()
	rs["srv-c"].Heartbeat() // srv-a went silent
	m := c.merged(rs)
	cams := func() []int {
		var out []int
		for _, e := range m.Query(p.Query{T0: tt, T1: tt + 3600}).Events {
			out = append(out, *e.Cam)
		}
		return out
	}
	eq(t, cams(), []int{8})
	eq(t, m.State, "live; srv-a unreachable") // unavailable, and the state says so
	rs["srv-a"].Heartbeat()
	eq(t, cams(), []int{7, 8})
	eq(t, m.State, "live")         // back with its disks: its database answers again, nothing rebuilt
	c.Servers["srv-b"].Down = true // live by heartbeat, not answering: named too
	eq(t, cams(), []int{7})
	eq(t, m.State, "live; srv-b unreachable")
	c.Servers["srv-b"].Down = false
	// retention on srv-b removed a bucket: ITS database forgets, by (server, path) — the console is not told, it asks
	c.Vars.Put("vms/retention/8", cluster.Items{"days": "0.01"}, cluster.NoCAS)
	eq(t, rs["srv-b"].Retain(), 1)
	eq(t, cams(), []int{7})
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
	c.create(t, ctl, map[string]any{"source": "driverpack://file/7.mp4", "events_retention_days": 30}) // the camera's events: the VMS row's knob
	rt, _, _ := c.Vars.Get("vms/retention/1")
	eq(t, rt, cluster.Items{"days": "30"})                     // the VMS's policy for its unit, as a row the platform reads
	if rr, _, _ := c.Vars.Get("rec/recordings/1"); rr != nil { // no recording: the camera is watched, its footage nobody's
		t.Fatal(rr)
	}
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
	if rep["removed"] != 2 || e2 != nil || e1 == nil || e3 == nil {
		t.Fatal(rep)
	}
	if rep["rec.added"] != 0 || rep["rec.media_removed"] != 0 || len(vms.NewManifest(srv.Archive, 1).Read()) != 0 { // the recorder's pass: no footage here, nothing to do
		t.Fatal(rep)
	}
	if _, err := os.Stat(filepath.Join(srv.Archive, "rec")); err == nil { // events are the worker's tree (vms/); footage would be the recorder's (rec/)
		t.Fatal("a rec/ tree with no recorder")
	}
}

func TestTheEventsKnobIsAPeerCopyAndTheOwnerRestores(t *testing.T) {
	// The storage knob's events row, as a copy between resources — no store in
	// between. Off: a silent server's events are unavailable by name. On: each
	// resource copied its CLOSED buckets to the next live resource after it, and
	// a fresh merge answers completely from the peer, saying so. Back with an
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
	// srv-b's own database covers the copies it holds — under srv-a's name, since only the source differs
	eq(t, pol["srv-b"].Database.Rebuild(), p.DBReport{Added: 3, Segments: 3, Mirrored: []string{"srv-a"}})
	var owners []string
	for _, e := range pol["srv-b"].Database.Query(p.Query{T0: tt, T1: tt + 7200}).Events {
		owners = append(owners, e.Server)
	}
	eq(t, owners, []string{"srv-a", "srv-b", "srv-a"}) // by time; two of them under srv-a's name
	pol["srv-a"].Database.Rebuild()
	pol["srv-c"].Database.Rebuild()
	m := c.merged(pol)
	ev := m.Query(p.Query{T0: tt, T1: tt + 7200, Cam: p.IntPtr(7)}).Events
	eq(t, evs(p.QueryResult{Events: ev}), []string{"vms/7/motion/srv-a/3/false", "det/d-12/person/srv-a/1/false", "vms/7/motion/srv-a/3/false"})
	eq(t, m.State, "live") // the owner answers, open bucket included; srv-b's copies are dropped
	// srv-a dies
	c.Wall.Advance(60)
	pol["srv-b"].Heartbeat()
	pol["srv-c"].Heartbeat()
	ev = m.Query(p.Query{T0: tt, T1: tt + 7200, Cam: p.IntPtr(7)}).Events
	eq(t, evs(p.QueryResult{Events: ev}), []string{"vms/7/motion/srv-a/3/false", "det/d-12/person/srv-a/1/false"}) // complete, from the peer; the open bucket is the RPO
	eq(t, m.State, "live; srv-a from mirror")
	// srv-a returns — with a REPLACED, empty disk
	os.RemoveAll(c.Servers["srv-a"].Archive)
	os.MkdirAll(c.Servers["srv-a"].Archive, 0o755)
	pol["srv-a"].Heartbeat()
	r = pol["srv-a"].Restore()
	eq(t, r["pulled"], 2)    // its two closed buckets are home;
	eq(t, r["rec.added"], 0) // no footage was ever here (rec/ is the recorder's)
	bs := p.BucketsUnder(c.Servers["srv-a"].Archive, "vms", "7", bucketSeconds)
	if len(bs) != 1 || bs[0].Path != ev[0].Bucket {
		t.Fatal(bs)
	}
	pol["srv-a"].Heartbeat()
	eq(t, pol["srv-a"].Database.Rebuild().Added, 2) // its job restarts: the database over the restored tree
	ev2 := m.Query(p.Query{T0: tt, T1: tt + 7200, Cam: p.IntPtr(7)}).Events
	eq(t, evs(p.QueryResult{Events: ev2}), evs(p.QueryResult{Events: ev}))
	eq(t, m.State, "live") // the owner is the source again; no duplicates
	ol, _ := c.Objects.List("platform/mirror")
	vl, _ := c.Vars.List("vms/mirror")
	if len(ol) != 0 || len(vl) != 0 { // no store in between, ever; and not the VMS's knob
		t.Fatal(ol, vl)
	}
}
