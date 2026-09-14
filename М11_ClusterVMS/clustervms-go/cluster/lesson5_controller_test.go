package cluster_test

// Lesson 5 — the controller as a job: placement under label constraints,
// the directory in one scan, two controllers agreeing, the snapshot that
// leaves the cluster, and the console over real HTTP.

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"

	"clustervms/cluster"
	p "vmsserver/psimplatform"
)

func threeWorkers(t *testing.T, c *Cluster) map[string]*cluster.ClusterWorker {
	ws := map[string]*cluster.ClusterWorker{"w-0": c.worker(t, 0, "srv-a", 0, nil), "w-1": c.worker(t, 1, "srv-b", 0, nil), "w-2": c.worker(t, 2, "srv-c", 0, nil)}
	for _, w := range ws {
		w.HeartbeatOnce()
	}
	return ws
}

func oneOf(s string, opts ...string) bool {
	for _, o := range opts {
		if s == o {
			return true
		}
	}
	return false
}

func TestPlacementUnderLabelConstraints(t *testing.T) {
	c := newCluster()
	ctl := c.controller(10, "")
	threeWorkers(t, c)
	a := c.create(t, ctl, srcLabels("a", "vlan:cctv-a"))
	b := c.create(t, ctl, srcLabels("b", "vlan:cctv-b"))
	ab := c.create(t, ctl, srcLabels("ab", "vlan:cctv-a", "vlan:cctv-b"))
	x := c.create(t, ctl, srcLabels("x", "vlan:cctv-x"))
	ctl.EnsurePlaced(nil)
	if !oneOf(ctl.Where(a.ID), "w-0", "w-1") || !oneOf(ctl.Where(b.ID), "w-1", "w-2") || ctl.Where(ab.ID) != "w-1" {
		t.Fatal(ctl.Where(a.ID), ctl.Where(b.ID), ctl.Where(ab.ID))
	}
	reason := ctl.Placement(ab.ID).Reason
	if !strings.Contains(reason, "reaching vlan:cctv-a,vlan:cctv-b") || !strings.Contains(reason, "on srv-b") {
		t.Fatal(reason)
	}
	eq(t, ctl.Where(x.ID), "")
	eq(t, ctl.Unplaceable(), []cluster.Unplaceable{{ID: x.ID, Labels: []string{"vlan:cctv-x"}, WorkersLive: 3}})
}

func TestAddingAWorkerMovesNothingEvenWithConstraints(t *testing.T) {
	c := newCluster()
	ctl := c.controller(2, "")
	c.worker(t, 0, "srv-a", 2, nil).HeartbeatOnce()
	for i := 0; i < 3; i++ {
		c.create(t, ctl, srcLabels(i, "vlan:cctv-a"))
	}
	ctl.EnsurePlaced(nil)
	eq(t, []string{ctl.Where(1), ctl.Where(2), ctl.Where(3)}, []string{"w-0", "w-0", ""})
	c.worker(t, 1, "srv-b", 2, nil).HeartbeatOnce()
	ctl.EnsurePlaced(nil)
	eq(t, []string{ctl.Where(1), ctl.Where(2), ctl.Where(3)}, []string{"w-0", "w-0", "w-1"})
}

func TestWhereIsCamera7InOneScan(t *testing.T) {
	c := newCluster()
	ctl := c.controller(0, "")
	threeWorkers(t, c)
	for i := 0; i < 9; i++ {
		c.create(t, ctl, src(i))
	}
	ctl.EnsurePlaced(nil)
	d := cluster.NewDirectory(c.Vars, 5, c.Clock.Now)
	eq(t, d.Where(7), ctl.Where(7))
	eq(t, d.Scans, 1)
	for i := 1; i <= 9; i++ {
		eq(t, d.Where(i), ctl.Where(i))
	}
	eq(t, d.Scans, 1) // nine answers, one scan
	var all []int
	for _, w := range []string{"w-0", "w-1", "w-2"} {
		all = append(all, d.Holdings(w)...)
	}
	sort.Ints(all)
	eq(t, all, []int{1, 2, 3, 4, 5, 6, 7, 8, 9})
}

func TestTwoControllersAgreeUnderConstraints(t *testing.T) {
	c := newCluster()
	threeWorkers(t, c)
	a := c.controller(100, "")
	b := c.controller(100, "")
	for i := 0; i < 40; i++ {
		if i%2 == 1 {
			c.create(t, a, srcLabels(i, "vlan:cctv-b"))
		} else {
			c.create(t, a, src(i))
		}
	}
	var wg sync.WaitGroup
	for _, x := range []*cluster.ClusterController{a, b, a, b} {
		wg.Add(1)
		go func(x *cluster.ClusterController) { defer wg.Done(); x.EnsurePlaced(nil) }(x)
	}
	wg.Wait()
	var units []int
	for _, w := range []string{"w-0", "w-1", "w-2"} {
		for _, u := range a.Assignment(w).Units {
			n, _ := strconv.Atoi(u)
			units = append(units, n)
		}
	}
	sort.Ints(units)
	for i := 1; i <= 40; i++ {
		if a.Where(i) == "" || units[i-1] != i { // each camera in exactly one assignment
			t.Fatal(i, units)
		}
		if i%2 == 0 && a.Where(i) == "w-0" { // cctv-b cameras never on srv-a
			t.Fatal(i)
		}
	}
	eq(t, len(units), 40)
}

func TestTheSnapshotIsTheOnlyThingThatLeavesTheCluster(t *testing.T) {
	c := newCluster()
	ctl := c.controller(0, "north")
	threeWorkers(t, c)
	c.create(t, ctl, map[string]any{"source": "driverpack://file/1.mp4", "name": "gate"})
	ctl.EnsurePlaced(nil)
	ctl.PublishSnapshot()
	raw, _ := c.Objects.Get("vms/snapshot")
	var snap map[string]any
	json.Unmarshal(raw, &snap)
	cams := snap["cameras"].([]any)
	first := cams[0].(map[string]any)
	if snap["cluster"] != "north" || first["name"] != "gate" || !oneOf(first["server"].(string), "srv-a", "srv-b", "srv-c") {
		t.Fatal(snap)
	}
	eq(t, snap["ts"], c.Wall.Now()) // a copy, with an age — the domain's RPO is this
}

func call(t *testing.T, method, url string, body any, headers map[string]string) (int, string) {
	t.Helper()
	var rd io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rd = bytes.NewReader(b)
	}
	req, _ := http.NewRequest(method, url, rd)
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(raw)
}

func TestTheConsoleOverHTTP(t *testing.T) {
	c := newCluster()
	ctl := c.controller(0, "")
	ws := threeWorkers(t, c)
	srv, ln, err := cluster.Serve(ctl, "127.0.0.1:0", cluster.ConsoleOptions{WorstFailover: 48.0, ArchiveRoot: c.Servers["srv-a"].Archive})
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	base := "http://" + ln.Addr().String()
	st, out := call(t, "POST", base+"/cameras", srcLabels(1, "vlan:cctv-b"), map[string]string{"Idempotency-Key": "k1"})
	var created map[string]any
	json.Unmarshal([]byte(out), &created)
	if st != 201 || created["worker"] != nil { // the console wrote the row; placement is the controller's
		t.Fatal(st, out)
	}
	if st, _ := call(t, "POST", base+"/cameras", src(1), map[string]string{"Idempotency-Key": "k1"}); st != 201 || len(ctl.Cameras()) != 1 {
		t.Fatal(st)
	}
	if st, _ := call(t, "PUT", base+"/cameras/1", map[string]any{"worker": "w-0"}, nil); st != 400 {
		t.Fatal(st)
	}
	pls, _ := ctl.EnsurePlaced(nil) // the controller's pass, under the label
	if len(pls) != 1 || !oneOf(pls[0].Worker, "w-1", "w-2") {
		t.Fatal(pls)
	}
	placed := pls[0].Worker
	w := ws[placed]
	w.ReconcileOnce()
	w.HeartbeatOnce()
	st, out = call(t, "GET", base+"/where/1", nil, nil)
	var d map[string]any
	json.Unmarshal([]byte(out), &d)
	if st != 200 || d["worker"] != d["directory"] || !strings.Contains(d["reason"].(string), "on srv-") {
		t.Fatal(out)
	}
	_, out = call(t, "GET", base+"/metrics", nil, nil)
	if !strings.Contains(out, `vms_failover_seconds{kind="worst"} 48.0`) || !strings.Contains(out, "vms_workers_live 3") || !strings.Contains(out, "vms_cameras_recording 1") {
		t.Fatal(out)
	}
	if st, out := call(t, "GET", base+"/resources", nil, nil); st != 200 || out != "{}" {
		t.Fatal(st, out)
	}
	if _, out := call(t, "GET", base+"/unplaceable", nil, nil); out != "[]" {
		t.Fatal(out)
	}
	// srv-a's resource job, over real HTTP: the platform's routes, the VMS's reads, and the event database over ITS tree
	segment(t, c.Servers["srv-a"], 1, 1, c.Wall.Now()-600, 600, 256)
	res := cluster.ClusterResource(c.Servers["srv-a"].Resource, "srv-a", "", c.Vars, c.Objects, c.Wall.Now, nil)
	rsrv, rln, err := p.Serve(res, "127.0.0.1:0", cluster.VmsRoutes(c.Servers["srv-a"].Resource))
	if err != nil {
		t.Fatal(err)
	}
	defer rsrv.Close()
	res.URL = "http://" + rln.Addr().String()
	res.Heartbeat()
	res.Database.Rebuild()
	// an operator's mark: the console's own bucket on srv-a's resource; the console has no database — it asks srv-a's, by HTTP, and finds the `cam` field
	st, out = call(t, "POST", base+"/marks", map[string]any{"cam": 1, "note": "check the gate"}, map[string]string{"Idempotency-Key": "m1", "X-User": "murat"})
	var m map[string]any
	json.Unmarshal([]byte(out), &m)
	if st != 201 || !strings.HasPrefix(m["bucket"].(string), "console/"+m["unit"].(string)+"/e1/") {
		t.Fatal(out)
	}
	eq(t, res.Database.Tail().Added, 1)
	st, out = call(t, "GET", base+"/events?cam=1", nil, nil)
	var evr map[string]any
	json.Unmarshal([]byte(out), &evr)
	evl := evr["events"].([]any)
	if st != 200 || evr["state"] != "live" || len(evl) != 1 {
		t.Fatal(st, out)
	}
	if e := evl[0].(map[string]any); e["subsystem"] != "console" || e["kind"] != "mark" || e["user"] != "murat" || e["server"] != "srv-a" {
		t.Fatal(out)
	}
	// the page, and playback across the cluster: a segment on srv-a's resource, served through the console by server
	if _, page := call(t, "GET", base+"/", nil, nil); !strings.Contains(page, "<video") || !strings.Contains(page, "/segment/") {
		t.Fatal("page")
	}
	_, out = call(t, "GET", base+"/timeline/1", nil, nil)
	var tl map[string]any
	json.Unmarshal([]byte(out), &tl)
	seg := tl["segments"].([]any)[0].(map[string]any)
	if seg["server"] != "srv-a" {
		t.Fatal(out)
	}
	req, _ := http.NewRequest("GET", base+"/segment/"+seg["path"].(string)+"?server=srv-a", nil)
	req.Header.Set("Range", "bytes=0-9")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	part, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 206 || len(part) != 10 || resp.Header.Get("Content-Range") != "bytes 0-9/256" {
		t.Fatal(resp.StatusCode, len(part), resp.Header.Get("Content-Range"))
	}
	if st, _ := call(t, "GET", base+"/segment/"+seg["path"].(string)+"?server=srv-b", nil, nil); st != 404 { // srv-b has never heartbeaten
		t.Fatal(st)
	}
	if st, _ := call(t, "PUT", base+"/cameras/1", map[string]any{"enabled": false}, nil); st != 200 || ctl.Camera(1).Enabled {
		t.Fatal(st)
	}
	if st, _ := call(t, "DELETE", base+"/cameras/1", nil, nil); st != 200 || len(ctl.Cameras()) != 0 {
		t.Fatal(st)
	}
	if st, _ := call(t, "DELETE", base+"/cameras/1", nil, nil); st != 404 {
		t.Fatal(st)
	}
	if gone := ctl.UnplaceDeleted(); len(gone) != 1 || len(ctl.Assignment(placed).Units) != 0 { // the controller takes the placement back
		t.Fatal(gone)
	}
}
