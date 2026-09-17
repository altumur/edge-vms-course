package vms_test

// Lesson 6 — vmscontroller: the only writer; refusals; placement with its
// property tests; two controllers; the read model; the failure arithmetic.

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/w2cplatform"
)

func mustCreate(t *testing.T, ctl *vms.VmsController, fields map[string]any) vms.Camera {
	t.Helper()
	r, err := ctl.CreateCamera(fields)
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func src(i any) map[string]any {
	return map[string]any{"source": fmt.Sprintf("driverpack://file/%v.mp4", i)}
}

func TestCRUDByCASAndWhatItRefuses(t *testing.T) {
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars, box.Objects, 0, box.Wall.Now)
	r := mustCreate(t, ctl, map[string]any{"name": "gate", "source": "driverpack://file/gate.mp4"})
	if r.ID != 1 || r.Revision != 1 || ctl.Camera(1).Name != "gate" {
		t.Fatal(r)
	}
	if u, _ := ctl.UpdateCamera(1, map[string]any{"events_retention_days": 14}); u.Revision != 2 {
		t.Fatal(u)
	}
	for _, bad := range []map[string]any{{"worker": "w-1"}, {"revision": 9}, {"phase": "running"}, {"epoch": 3}, {"placement": map[string]any{}}} {
		_, err := ctl.UpdateCamera(1, bad)
		var refused *vms.Refused
		if !errors.As(err, &refused) || !strings.Contains(refused.Msg, "may not set") {
			t.Fatal("must refuse", bad, err)
		}
	}
	_, err := ctl.CreateCamera(map[string]any{"name": "x"})
	var refused *vms.Refused
	if !errors.As(err, &refused) || !strings.Contains(refused.Msg, "needs a source") {
		t.Fatal(err)
	}
	ctl.DeleteCamera(1)
	if ctl.Camera(1) != nil || len(ctl.Cameras()) != 0 {
		t.Fatal("deleted")
	}
}

func TestPlacementIsStoredWithAReasonAndAddingAWorkerMovesNothing(t *testing.T) {
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars, box.Objects, 3, box.Wall.Now)
	for i := 0; i < 6; i++ {
		mustCreate(t, ctl, src(i))
	}
	placed, _ := ctl.EnsurePlaced([]string{"w-1", "w-2"})
	if len(placed) != 6 {
		t.Fatal(placed)
	}
	for _, pl := range placed {
		if !strings.HasPrefix(pl.Reason, "most free capacity") {
			t.Fatal(pl)
		}
	}
	before := map[int]string{}
	count := map[string]int{}
	for _, c := range ctl.Cameras() {
		before[c.ID] = ctl.Where(c.ID)
		count[ctl.Where(c.ID)]++
	}
	if count["w-1"] != 3 || count["w-2"] != 3 {
		t.Fatal(count)
	}
	mustCreate(t, ctl, src(7))
	if pl, _ := ctl.Place(7, []string{"w-1", "w-2"}); pl != nil { // the system is full
		t.Fatal(pl)
	}
	ctl.EnsurePlaced([]string{"w-1", "w-2", "w-3"}) // a worker arrives
	for c, w := range before {
		if ctl.Where(c) != w { // nothing moved
			t.Fatal(c)
		}
	}
	if ctl.Where(7) != "w-3" || ctl.Placement(7).Rev != 1 || ctl.Placement(7).At != box.Wall.Now() { // the new one went to the new worker
		t.Fatal(ctl.Placement(7))
	}
}

func TestCapacityIsTheWorkersWordNotTheControllers(t *testing.T) {
	// Two workers on different hardware say different numbers in their
	// heartbeats; the controller places by what they said and its own constant
	// is only the fallback for a worker that said nothing.
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars, box.Objects, 50, box.Wall.Now)
	small := worker(t, box, "w-1", vms.NewFakeActuator(), vms.VmsWorkerOptions{Capacity: 2})
	big := worker(t, box, "w-2", vms.NewFakeActuator(), vms.VmsWorkerOptions{Capacity: 6})
	small.HeartbeatOnce()
	big.HeartbeatOnce()
	if ctl.CapacityOf("w-1") != 2 || ctl.CapacityOf("w-2") != 6 || ctl.CapacityOf("w-9") != 50 {
		t.Fatal("capacity")
	}
	for i := 0; i < 9; i++ {
		mustCreate(t, ctl, src(i))
	}
	placed, _ := ctl.EnsurePlaced(nil)
	if len(placed) != 8 || ctl.Load("w-1") != 2 || ctl.Load("w-2") != 6 { // the ninth waits: the system is full
		t.Fatal(len(placed))
	}
	if pl, _ := ctl.Place(9, nil); pl != nil || ctl.Headroom() != 8 { // headroom is stale until they heartbeat again
		t.Fatal(pl, ctl.Headroom())
	}
	small.ReconcileOnce()
	big.ReconcileOnce()
	small.HeartbeatOnce()
	big.HeartbeatOnce()
	eq(t, ctl.Headroom(), 0)
	if !strings.Contains(ctl.Placement(1).Reason, "(6)") && !strings.Contains(ctl.Placement(2).Reason, "(6)") { // the reason says whose number it was
		t.Fatal(ctl.Placement(1).Reason)
	}
}

func TestTwoControllersAgreeByCAS(t *testing.T) {
	box := testbox.NewBox()
	a := vms.NewVmsController(box.Vars, box.Objects, 100, box.Wall.Now)
	for i := 0; i < 40; i++ {
		mustCreate(t, a, src(i))
	}
	var wg sync.WaitGroup
	for _, prefer := range [][]string{{"w-1", "w-2"}, {"w-2", "w-1"}} {
		wg.Add(1)
		go func(prefer []string) {
			defer wg.Done()
			c := vms.NewVmsController(box.Vars, box.Objects, 100, box.Wall.Now)
			for _, cam := range c.Cameras() {
				c.Place(cam.ID, prefer)
			}
		}(prefer)
	}
	wg.Wait()
	c := vms.NewVmsController(box.Vars, box.Objects, 0, box.Wall.Now)
	n := 0
	for _, cam := range c.Cameras() {
		if c.Where(cam.ID) == "" {
			t.Fatal(cam.ID)
		}
		n++
	}
	var units []int
	for _, w := range []string{"w-1", "w-2"} {
		for _, u := range c.Assignment(w).Units {
			i, _ := strconv.Atoi(u)
			units = append(units, i)
		}
	}
	sort.Ints(units)
	for i, u := range units { // every camera exactly once, whoever won
		if u != i+1 {
			t.Fatal(units)
		}
	}
	if n != 40 || len(units) != 40 {
		t.Fatal(n, len(units))
	}
}

func TestRebalanceIsExplicitBudgetedAndStopsInTheDeadBand(t *testing.T) {
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars, box.Objects, 10, box.Wall.Now)
	for i := 0; i < 8; i++ {
		mustCreate(t, ctl, src(i))
	}
	ctl.EnsurePlaced([]string{"w-1"})                                   // all eight on w-1
	eq(t, ctl.Rebalance(0, 0.10, []string{"w-1", "w-2"}), []vms.Move{}) // no budget, no moves
	moves := ctl.Rebalance(3, 0.10, []string{"w-1", "w-2"})
	if len(moves) != 3 {
		t.Fatal(moves)
	}
	for _, m := range moves {
		if m.From != "w-1" || m.To != "w-2" {
			t.Fatal(m)
		}
	}
	if !strings.Contains(ctl.Placement(moves[0].Camera).Reason, "rebalance") || ctl.Load("w-1") != 5 || ctl.Load("w-2") != 3 {
		t.Fatal(ctl.Load("w-1"), ctl.Load("w-2"))
	}
	eq(t, ctl.Rebalance(5, 0.10, []string{"w-1", "w-2"}), []vms.Move{{4, "w-1", "w-2"}}) // one more, then inside the dead band
}

func TestTheFailureArithmetic(t *testing.T) {
	// Stop each process in turn and say what stopped.
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars, box.Objects, 0, box.Wall.Now)
	for i := 1; i <= 2; i++ {
		mustCreate(t, ctl, src(i))
	}
	act := vms.NewFakeActuator()
	w := worker(t, box, "w-1", act, vms.VmsWorkerOptions{})
	w.HeartbeatOnce()
	ctl.EnsurePlaced(nil)
	w.ReconcileOnce()
	w.HeartbeatOnce()
	eq(t, act.RunningIDs(), []int{1, 2})
	// controller down: the read model still answers (heartbeats), recording continues, edits stop
	rows := vms.NewVmsController(box.Vars, box.Objects, 0, box.Wall.Now).ReadModel(45)
	if len(rows) != 2 || rows[0]["phase"] != "running" || rows[1]["phase"] != "running" {
		t.Fatal(rows)
	}
	// worker down: the console shows the last snapshot with its age; edits still land in the store
	box.Wall.Advance(100)
	ctl.UpdateCamera(1, map[string]any{"name": "edited while w-1 was down"})
	rows = ctl.ReadModel(45)
	if rows[0]["worker_state"] != "stale" || rows[0]["age"] != 100.0 {
		t.Fatal(rows[0])
	}
	// ...and are applied the moment the worker is back — from the store, not from the controller
	w2 := worker(t, box, "w-1", vms.NewFakeActuator(), vms.VmsWorkerOptions{})
	eq(t, w2.ReconcileOnce(), actions("start 1", "start 2"))
	eq(t, w2.Rows[0].Name, "edited while w-1 was down")
}

func TestScaleInReleasesASlotAndTheControllerRedistributes(t *testing.T) {
	// Nomad decided `count` 3 → 2. The worker it stops releases its slot; the
	// controller's placement pass moves that slot's cameras — its one unasked
	// move — and nothing else. A crash releases nothing and moves nothing.
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars, box.Objects, 4, box.Wall.Now)
	var ws []*vms.VmsWorker
	for i := 0; i < 3; i++ {
		w := worker(t, box, "", vms.NewFakeActuator(), vms.VmsWorkerOptions{Capacity: 4})
		w.HeartbeatOnce()
		ws = append(ws, w)
	}
	for i := 1; i <= 6; i++ {
		mustCreate(t, ctl, src(i))
	}
	ctl.EnsurePlaced(nil)
	loads := map[string]int{}
	for w, a := range ctl.Assignments() {
		loads[w] = len(a.Units)
	}
	eq(t, loads, map[string]int{"w-1": 2, "w-2": 2, "w-3": 2})
	eq(t, ctl.Headroom(), 12)
	eq(t, ctl.Redistribute(nil), []vms.Move{}) // nothing released: nothing moves
	box.Wall.Advance(46)                       // w-3 crashed: silent, not released
	ws[0].HeartbeatOnce()
	ws[1].HeartbeatOnce()
	eq(t, ctl.Redistribute(nil), []vms.Move{})
	eq(t, ctl.Where(3), "w-3") // a crash is Nomad's to fix; the cameras wait for w-3
	ws[2].ReleaseSlot()        // scale-in: SIGTERM, an orderly stop
	moves := ctl.Redistribute(nil)
	if len(moves) != 2 || moves[0].Camera != 3 || moves[0].From != "w-3" || moves[1].Camera != 6 || moves[1].From != "w-3" {
		t.Fatal(moves)
	}
	if len(ctl.Assignment("w-3").Units) != 0 || !strings.Contains(ctl.Placement(3).Reason, "slot w-3 released") {
		t.Fatal(ctl.Placement(3))
	}
	for _, c := range []int{3, 6} {
		if w := ctl.Where(c); w != "w-1" && w != "w-2" {
			t.Fatal(w)
		}
	}
	ws[0].ReconcileOnce()
	ws[1].ReconcileOnce()
	ws[0].HeartbeatOnce()
	ws[1].HeartbeatOnce()
	eq(t, ctl.Headroom(), 8-6) // 2 workers × 4, six cameras: what the autoscaler reads
	ctl.Retire("w-1")          // the operator's word that a slot is gone for good
	eq(t, ctl.ReleasedSlots(), []string{"w-1"})
}

func call(t *testing.T, method, url string, body any, headers map[string]string) (int, map[string]any, []byte) {
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
	var m map[string]any
	json.Unmarshal(raw, &m)
	return resp.StatusCode, m, raw
}

func TestTheConsoleOverHTTP(t *testing.T) {
	// The console is its own process with its own token: the operator's rows,
	// never placement. The controller, on its pass, places what the console
	// created and unplaces what it deleted.
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars.AsWriter("vmscontroller", vms.Spec.ACLController()...), box.Objects, 0, box.Wall.Now)
	con := vms.NewVmsController(box.Vars.AsWriter("console", vms.Spec.ACLConsole()...), box.Objects, 0, box.Wall.Now) // what the console process holds
	w := worker(t, box, "w-1", vms.NewFakeActuator(), vms.VmsWorkerOptions{Server: "srv-1"})
	w.HeartbeatOnce()
	srv, ln, err := vms.Serve(con, vms.NewArchiveResource(box.Spool, box.Archive, 600, nil), "127.0.0.1:0", box.Wall.Now, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	base := "http://" + ln.Addr().String()
	st, r, _ := call(t, "POST", base+"/cameras", map[string]any{"name": "gate", "source": "driverpack://file/gate.mp4"}, map[string]string{"Idempotency-Key": "k1"})
	if st != 201 || r["id"] != 1.0 || r["worker"] != nil { // the row; not placed by the console
		t.Fatal(st, r)
	}
	_, r2, _ := call(t, "POST", base+"/cameras", map[string]any{"name": "gate", "source": "driverpack://file/gate.mp4"}, map[string]string{"Idempotency-Key": "k1"})
	if !reflect.DeepEqual(r2, r) || len(ctl.Cameras()) != 1 { // the same POST, not a second camera
		t.Fatal(r2)
	}
	if _, err := con.Place(1, nil); !errors.Is(err, p.ErrForbidden) { // a console token never writes placement
		t.Fatal(err)
	}
	if pls, _ := ctl.EnsurePlaced(nil); len(pls) != 1 || pls[0].Worker != "w-1" || ctl.Where(1) != "w-1" { // the controller's pass did
		t.Fatal(pls)
	}
	if st, _, _ := call(t, "PUT", base+"/cameras/1", map[string]any{"worker": "w-9"}, map[string]string{"Idempotency-Key": "k2"}); st != 400 {
		t.Fatal(st)
	}
	w.ReconcileOnce()
	w.HeartbeatOnce()
	_, body, _ := call(t, "GET", base+"/cameras", nil, nil)
	rows := body["rows"].([]any)
	if rows[0].(map[string]any)["phase"] != "running" || rows[0].(map[string]any)["server"] != "srv-1" {
		t.Fatal(rows)
	}
	if _, wh, _ := call(t, "GET", base+"/where/1", nil, nil); wh["worker"] != "w-1" {
		t.Fatal(wh)
	}
	if _, _, raw := call(t, "GET", base+"/metrics", nil, nil); !strings.Contains(string(raw), "vms_cameras_running 1") {
		t.Fatal(string(raw))
	}
	// an operator's mark is the CONSOLE's event: its own bucket, never a worker's
	st, m, _ := call(t, "POST", base+"/marks", map[string]any{"cam": 1, "note": "left the bag"}, map[string]string{"Idempotency-Key": "k3", "X-User": "murat"})
	if st != 201 || m["subsystem"] != "console" || !strings.HasPrefix(m["bucket"].(string), "console/"+m["unit"].(string)+"/e1/") {
		t.Fatal(m)
	}
	if _, again, _ := call(t, "POST", base+"/marks", map[string]any{"cam": 1, "note": "left the bag"}, map[string]string{"Idempotency-Key": "k3", "X-User": "murat"}); !reflect.DeepEqual(again, m) { // idempotent: one mark
		t.Fatal(again)
	}
	ev := p.ReadBucket(filepath.Join(box.Archive, m["bucket"].(string)))
	if len(ev) != 1 || ev[0].T() != box.Wall.Now() || ev[0].Kind() != "mark" || p.ToFloat(ev[0]["cam"]) != 1 || ev[0]["user"] != "murat" || ev[0]["note"] != "left the bag" {
		t.Fatal(ev)
	}
	eq(t, p.SubsystemsUnder(box.Archive), map[string][]string{"console": {m["unit"].(string)}}) // not in vms/1/: that bucket has one writer
	// the page, and the bytes it plays: three fetches and a Range
	_, _, page := call(t, "GET", base+"/", nil, nil)
	for _, want := range []string{"/spec", "/timeline/", "/segment/", "<video"} {
		if !strings.Contains(string(page), want) {
			t.Fatal(want)
		}
	}
	if body := string(page)[strings.LastIndex(string(page), "-->"):]; strings.Contains(strings.ToLower(body), "camera") { // the page (after its comments) is the spec's, not the VMS's
		t.Fatal("the page names a camera")
	}
	_, spec, _ := call(t, "GET", base+"/spec", nil, nil) // what the page reads first: the YAML, not code
	if spec["rows"] != "cameras" || spec["media"] != true || len(spec["fields"].([]any)) < 3 {
		t.Fatal(spec)
	}
	seg := writeSegment(t, box.Spool, 1, 1, "2026-09-12T10:00:00", 256, 0)
	vms.NewArchiveResource(box.Spool, box.Archive, 600, nil).Promote(seg, 0, "live")
	_, _, raw := call(t, "GET", base+"/timeline/1", nil, nil)
	var tl []map[string]any
	json.Unmarshal(raw, &tl)
	if len(tl) != 1 || tl[0]["media"] != "rec/1/e1/20260912T100000Z.mp4" {
		t.Fatal(string(raw))
	}
	req, _ := http.NewRequest("GET", base+"/segment/"+tl[0]["media"].(string), nil)
	req.Header.Set("Range", "bytes=10-19")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	part, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 206 || len(part) != 10 || resp.Header.Get("Content-Range") != "bytes 10-19/256" {
		t.Fatal(resp.StatusCode, len(body), resp.Header.Get("Content-Range"))
	}
	if st, _, _ := call(t, "GET", base+"/segment/vms/1/e1/nope.mp4", nil, nil); st != 404 {
		t.Fatal(st)
	}
	// the page's writes: disable, then delete — through the controller
	if st, r, _ := call(t, "PUT", base+"/cameras/1", map[string]any{"enabled": false}, map[string]string{"Idempotency-Key": "k4"}); st != 200 || r["enabled"] != false || ctl.Camera(1).Revision != 2 {
		t.Fatal(st, r)
	}
	if st, r, _ := call(t, "DELETE", base+"/cameras/1", nil, nil); st != 200 || r["deleted"] != 1.0 || len(ctl.Cameras()) != 0 || ctl.Where(1) != "w-1" { // the row is gone; the placement waits for the pass
		t.Fatal(st, r)
	}
	if gone := ctl.UnplaceDeleted(); !reflect.DeepEqual(gone, []int{1}) || ctl.Where(1) != "" || len(ctl.Assignment("w-1").Units) != 0 {
		t.Fatal(gone)
	}
	if st, _, _ := call(t, "DELETE", base+"/cameras/1", nil, nil); st != 404 { // gone is gone
		t.Fatal(st)
	}
}

func TestARetryThatLandsOnAnotherConsoleIsOneCamera(t *testing.T) {
	// With a console on every server, a client's retry may reach a different
	// instance. The key is a Variable, claimed by create-only CAS, so the
	// second console serves the first one's reply and never repeats the write.
	box := testbox.NewBox()
	a := vms.NewVmsController(box.Vars.AsWriter("console", vms.Spec.ACLConsole()...), box.Objects, 0, box.Wall.Now)
	b := vms.NewVmsController(box.Vars.AsWriter("console", vms.Spec.ACLConsole()...), box.Objects, 0, box.Wall.Now)
	s1, l1, _ := vms.Serve(a, nil, "127.0.0.1:0", box.Wall.Now, nil)
	s2, l2, _ := vms.Serve(b, nil, "127.0.0.1:0", box.Wall.Now, nil)
	defer s1.Close()
	defer s2.Close()
	b1, b2 := "http://"+l1.Addr().String(), "http://"+l2.Addr().String()
	body := map[string]any{"name": "gate", "source": "driverpack://file/g.mp4"}
	st, first, _ := call(t, "POST", b1+"/cameras", body, map[string]string{"Idempotency-Key": "k-1"})
	st2, again, _ := call(t, "POST", b2+"/cameras", body, map[string]string{"Idempotency-Key": "k-1"}) // the retry reaches the OTHER console
	if st != 201 || st2 != 201 || !reflect.DeepEqual(first, again) || len(a.Cameras()) != 1 {
		t.Fatal(st, st2, first, again)
	}
	if items, _, _ := box.Vars.Get("vms/idem/k-1"); items["state"] != "done" {
		t.Fatal(items)
	}
	// in flight: console B holds the claim and has not answered yet; A waits for B's reply rather than writing
	box.Vars.Put("vms/idem/k-2", p.Items{"state": "pending", "at": "0"}, p.Absent)
	done := make(chan map[string]any, 1)
	go func() {
		_, r, _ := call(t, "POST", b1+"/cameras", body, map[string]string{"Idempotency-Key": "k-2"})
		done <- r
	}()
	time.Sleep(150 * time.Millisecond)
	select {
	case r := <-done:
		t.Fatal("answered before the claim was filled", r)
	default:
	}
	if len(a.Cameras()) != 1 {
		t.Fatal("wrote while another instance held the key")
	}
	box.Vars.Put("vms/idem/k-2", p.Items{"state": "done", "status": "201", "body": `{"id":1,"worker":null}`, "at": "0"}, p.NoCAS)
	if r := <-done; r["id"] != 1.0 || len(a.Cameras()) != 1 {
		t.Fatal(r)
	}
	if st, _, _ := call(t, "POST", b1+"/cameras", body, map[string]string{"Idempotency-Key": "a/b"}); st != 400 { // not a path segment
		t.Fatal(st)
	}
	// old keys go: a day later the next claim prunes them
	keys := p.NewIdempotencyKeys(box.Vars, "vms/idem/", box.Wall.Now)
	box.Wall.Advance(90000)
	if n := keys.ForcePrune(); n != 2 {
		t.Fatal(n)
	}
	if paths, _ := box.Vars.List("vms/idem/"); len(paths) != 0 {
		t.Fatal(paths)
	}
	if _, r, _ := call(t, "POST", b2+"/cameras", body, map[string]string{"Idempotency-Key": "k-1"}); r["id"] != 2.0 { // a forgotten key is a new request, by design
		t.Fatal(r)
	}
}

func TestAReleasedSlotIsNotGivenNewCameras(t *testing.T) {
	// The bug this fixes: Redistribute knew a released slot was leaving and moved
	// its cameras off — and the SAME pass could hand it a brand-new camera,
	// because the placement pool was built from "seen and heartbeating" and a
	// process on its way out is both. Leaving is not a capacity.
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars, box.Objects, 4, box.Wall.Now)
	var ws []*vms.VmsWorker
	for i := 0; i < 2; i++ {
		w := worker(t, box, "", vms.NewFakeActuator(), vms.VmsWorkerOptions{Capacity: 4})
		w.HeartbeatOnce()
		ws = append(ws, w)
	}
	mustCreate(t, ctl, src(1))
	ctl.EnsurePlaced(nil)
	ws[0].ReleaseSlot() // scale-in: w-1 is stopping, and still heartbeating while it does
	ws[0].HeartbeatOnce()
	ws[1].HeartbeatOnce()
	ctl.Redistribute(nil)
	mustCreate(t, ctl, src(2)) // a camera added DURING the scale-in
	ctl.EnsurePlaced(nil)
	for _, c := range []int{1, 2} {
		if w := ctl.Where(c); w != "w-2" {
			t.Fatalf("camera %d went to %q: a released slot took a new camera", c, w)
		}
	}
	if len(ctl.Assignment("w-1").Units) != 0 {
		t.Fatal(ctl.Assignment("w-1"))
	}
}
