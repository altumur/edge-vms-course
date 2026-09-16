package vms_test

// Lesson 10 — events: the database that is a cache. The resource PROCESS keeps
// a database over its own tree and serves it; the console holds none and
// asks. The worker's `silent` and the operator's `mark` on one camera's
// timeline (the Go port has no detector yet); a restarted database rebuilds
// to the same answer from the files; retention takes the rows with the file.

import (
	"reflect"
	"strings"
	"testing"

	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/w2cplatform"
)

// what `vms resource` does: the platform's Resource with the VMS registered,
// served over HTTP, heartbeating so the console can find it, its database rebuilt.
func resourceProcess(t *testing.T, box *testbox.Box) (*p.Resource, func()) {
	t.Helper()
	ar := vms.NewArchiveResource(box.Spool, box.Archive, 600, box.Wall.Now)
	res := vms.NewVmsResource(ar, "srv-1", "", box.Vars, box.Objects, box.Wall.Now, nil)
	srv, ln, err := p.Serve(res, "127.0.0.1:0", vms.ResourceRoutes(ar))
	if err != nil {
		t.Fatal(err)
	}
	res.URL = "http://" + ln.Addr().String()
	res.Heartbeat()
	res.Database.Rebuild()
	return res, func() { srv.Close() }
}

func TestEventsReachTheTimelineThroughTheResourceProcessAndTheConsole(t *testing.T) {
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars.AsWriter("vmscontroller", vms.Spec.ACLController()...), box.Objects, 0, box.Wall.Now)
	con := vms.NewVmsController(box.Vars.AsWriter("vmsconsole", vms.Spec.ACLConsole()...), box.Objects, 0, box.Wall.Now)
	act := vms.NewFakeActuator()
	w := worker(t, box, "w-1", act, vms.VmsWorkerOptions{Server: "srv-1", ArchiveRoot: box.Archive})
	w.HeartbeatOnce()
	if _, err := con.CreateCamera(map[string]any{"name": "gate", "source": "driverpack://file/gate.mp4"}); err != nil {
		t.Fatal(err)
	}
	ctl.EnsurePlaced(nil)
	w.ReconcileOnce()
	w.HeartbeatOnce()
	res, stopRes := resourceProcess(t, box)
	if seen := p.ResourcesSeen(box.Objects); len(seen) != 1 || seen["srv-1"].URL != res.URL { // the console finds the resource by its heartbeat
		t.Fatal(seen)
	}
	srv, ln, err := vms.Serve(con, vms.NewArchiveResource(box.Spool, box.Archive, 600, nil), "127.0.0.1:0", box.Wall.Now, nil) // no database here: MergedIndex asks srv-1
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	base := "http://" + ln.Addr().String()
	// the operator marks a moment, and the camera goes silent: two subsystems' events on one resource
	call(t, "POST", base+"/marks", map[string]any{"cam": 1, "note": "check this"}, map[string]string{"Idempotency-Key": "m1", "X-User": "murat"})
	box.Wall.Advance(2)
	act.Dead = []int{1}
	w.PumpOnce()
	eq(t, res.Database.Tail().Added, 2) // the resource's tail; the console was not told
	st, out, _ := call(t, "GET", base+"/events?cam=1", nil, nil)
	evs := out["events"].([]any)
	if st != 200 || out["state"] != "live" || len(evs) != 2 {
		t.Fatal(st, out)
	}
	var got [][3]any
	for _, e := range evs {
		m := e.(map[string]any)
		got = append(got, [3]any{m["subsystem"], m["kind"], m["fenced"]})
	}
	eq(t, got, [][3]any{{"console", "mark", false}, {"vms", "silent", false}})
	eq(t, evs[0].(map[string]any)["user"], "murat")
	// the worker's epoch moves: its events are fenced, the console's are not — fencing is per unit, by ITS subsystem's epoch
	box.Vars.Put("vms/epoch/1", p.Items{"epoch": "2"}, p.NoCAS)
	_, out, _ = call(t, "GET", base+"/events?cam=1", nil, nil)
	evs = out["events"].([]any)
	eq(t, evs[0].(map[string]any)["fenced"], false)
	eq(t, evs[1].(map[string]any)["fenced"], true)
	// the resource process stops: the console says so by name, and answers with what it has — nothing
	stopRes()
	st, out, _ = call(t, "GET", base+"/events?cam=1", nil, nil)
	if st != 200 || len(out["events"].([]any)) != 0 || out["state"] != "live; srv-1 unreachable" {
		t.Fatal(st, out)
	}
}

func TestTheDatabaseIsACacheAndRetentionTakesTheRowsWithTheFile(t *testing.T) {
	box := testbox.NewBox()
	tt := box.Wall.Now() - 3*86400
	vms.EventLogFor(box.Archive, 7, 1, 600).Append(tt+10, "motion", map[string]any{"zone": "gate"}) // three days old: past a 1-day policy
	vms.EventLogFor(box.Archive, 7, 1, 600).Append(box.Wall.Now()-100, "motion", nil)               // fresh
	ar := vms.NewArchiveResource(box.Spool, box.Archive, 600, box.Wall.Now)
	res := vms.NewVmsResource(ar, "srv-1", "http://srv-1", box.Vars, box.Objects, box.Wall.Now, nil)
	res.Heartbeat()
	rep := res.Database.Rebuild()
	if rep.Added != 2 || rep.Segments != 2 || len(rep.Mirrored) != 0 || res.Database.State != "live" {
		t.Fatal(rep)
	}
	again := p.NewEventDatabase(box.Archive, "srv-1", box.Wall.Now, 600)
	again.Rebuild()
	if !reflect.DeepEqual(again.Query(p.Query{T1: 1e12}).Events, res.Database.Query(p.Query{T1: 1e12}).Events) { // a cache proves it by being rebuilt
		t.Fatal("not the same rows")
	}
	m := p.NewMergedIndex(box.Objects, func(url string, q p.Query) (p.QueryResult, error) { return res.Database.Query(q), nil }, box.Wall.Now)
	times := func() []float64 {
		var out []float64
		for _, e := range m.Query(p.Query{T1: 1e12, Cam: p.IntPtr(7)}).Events {
			out = append(out, e.T)
		}
		return out
	}
	eq(t, times(), []float64{tt + 10, box.Wall.Now() - 100})
	box.Vars.Put("vms/retention/7", p.Items{"days": "1"}, p.NoCAS) // the VMS's policy for its unit, as a row the platform reads
	eq(t, res.Retain(), 1)                                         // the file went — and the rows with it
	eq(t, times(), []float64{box.Wall.Now() - 100})
	l, _ := box.Vars.List("vms/events")
	if len(l) != 0 || strings.Contains(strings.Join(l, ","), "events") {
		t.Fatal(l)
	}
}
