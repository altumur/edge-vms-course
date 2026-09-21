package vms_test

// `<name>/requests/<id>` and what runs on it: an operator asking the recorder for a range, a scan asking it
// for footage the device has, the survey asking it to keep what a model liked — and the device's INDEX,
// which is what lets any of them know whether the minutes exist at all.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/w2cplatform"
)

func console(box *testbox.Box, spec *p.SubsystemSpec) *p.SpecController {
	return p.NewSpecController(spec, box.Vars.AsWriter("console", spec.ACLConsole()...), box.Objects, 2, box.Wall.Now, "")
}

func requestIDs(box *testbox.Box) []string {
	keys, _ := box.Vars.List(vms.RecSpec.Sub().RequestsPrefix())
	out := []string{}
	for _, k := range keys {
		out = append(out, k[strings.LastIndex(k, "/")+1:])
	}
	return out
}

// An edge box: a holder of camera 1 whose device has an archive, a recording of it, and a recorder placed on
// it. `index` is the device's own listing (nil: this driver cannot list).
func edgeBox(t *testing.T, index map[string][][2]float64, cov vms.Coverage) (*testbox.Box, *vms.VmsWorker, *p.SpecController, *vms.RecWorker) {
	t.Helper()
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars, box.Objects, 0, box.Wall.Now)
	w := holder(t, box, func(k string) vms.Device {
		d := vms.NewFakeDevice(k, []string{"1"}, map[string]vms.Coverage{"1": cov})
		d.Index = index
		return d
	}, nil)
	mustCreate(t, ctl, map[string]any{"name": "front", "source": card})
	ctl.EnsurePlaced(nil)
	w.ReconcileOnce()
	w.HeartbeatOnce()

	recCon := console(box, vms.RecSpec)
	if _, err := recCon.Create(map[string]any{"cam": "1"}); err != nil {
		t.Fatal(err)
	}
	recCtl := p.NewSpecController(vms.RecSpec, box.Vars.AsWriter("reccontroller", vms.RecSpec.ACLController()...), box.Objects, 0, box.Wall.Now, "")
	arch := vms.NewArchiveResource(box.Spool, box.Archive, 600, box.Wall.Now)
	r, err := vms.NewRecWorker("r-1", box.Vars.AsWriter("recworker", "rec/epoch/*", "rec/slots/*"), box.Objects, vms.NewFakeActuator(), arch,
		vms.VmsWorkerOptions{Server: "srv-1", Env: vms.Env{}, WorkerOptions: p.WorkerOptions{Clock: box.Clock.Now, Wall: box.Wall.Now}})
	if err != nil {
		t.Fatal(err)
	}
	r.KeepDays, r.Settle = 1, 1000
	r.HeartbeatOnce()
	recCtl.EnsurePlaced(nil)
	r.ReconcileOnce()
	return box, w, recCon, r
}

func ask(box *testbox.Box, id, unit string, t0, t1 float64) {
	box.Vars.Put(vms.RecSpec.Sub().RequestKey(id), p.Items{"unit": unit, "cam": unit, "from": ftoaT(t0), "to": ftoaT(t1),
		"at": "1", "by": "anna"}, p.Absent)
}

func ftoaT(f float64) string { b, _ := json.Marshal(f); return string(b) }

// -- the operator's request ----------------------------------------------------------------------------

func TestABackfillRequestIsARowAndNotA202(t *testing.T) {
	// It used to answer 202 and store nothing. The text was true about what the recorder would do and false
	// about anything having been asked — a lie that survives right up until somebody checks whether the
	// range arrived.
	box := testbox.NewBox()
	rec := console(box, vms.RecSpec)
	rec.Create(map[string]any{"cam": "7"})
	vctl := vms.NewVmsController(box.Vars.AsWriter("console", vms.Spec.ACLConsole()...), box.Objects, 0, box.Wall.Now)
	h := vms.NewHandler(vctl, vms.NewArchiveResource(box.Spool, box.Archive, 600, box.Wall.Now), box.Wall.Now, rec)

	post := func() map[string]any {
		req := httptest.NewRequest("POST", "/backfill", bytes.NewBufferString(`{"cam":"7","from":100,"to":200}`))
		req.Header.Set("X-User", "anna")
		rw := httptest.NewRecorder()
		h.ServeHTTP(rw, req)
		if rw.Code != 202 {
			t.Fatalf("%d %s", rw.Code, rw.Body)
		}
		var body map[string]any
		json.Unmarshal(rw.Body.Bytes(), &body)
		return body["queued"].(map[string]any)
	}
	if q := post(); q["id"] != "7-100-200" {
		t.Fatal(q)
	}
	it, _, _ := box.Vars.Get(vms.RecSpec.Sub().RequestKey("7-100-200"))
	if it["unit"] != "7" || it["from"] != "100" || it["by"] != "anna" {
		t.Fatalf("%v", it)
	}
	post() // a retry is the same row, not a second fetch
	eq(t, requestIDs(box), []string{"7-100-200"})
}

func TestARequestTheRecorderFetchedIsCleared(t *testing.T) {
	box := testbox.NewBox()
	rec := console(box, vms.RecSpec)
	ask(box, "7-100-200", "7", 100, 200)
	ask(box, "7-300-400", "7", 300, 400)
	box.Objects.Put(vms.RecSpec.Sub().HeartbeatKey("r-1"), p.Heartbeat{Worker: "r-1", Ts: box.Wall.Now(),
		Extra: map[string]any{"server": "srv-1", "fetched": "7-100-200"}}.ToBytes())
	if n := vms.ClearRequests(rec); n != 1 {
		t.Fatal(n)
	}
	eq(t, requestIDs(box), []string{"7-300-400"})
	if vms.ClearRequests(rec) != 0 { // …and again is a no-op
		t.Fatal("cleared twice")
	}
}

func TestARequestIsFetchedOutsideTheWindowAndTheBudget(t *testing.T) {
	// The ordinary pass is bounded by a budget and an hour because backfill shares the device's uplink with
	// live. A range a PERSON asked for is different work: they are looking at that gap now, and "tonight"
	// is not a useful answer.
	now := testbox.NewBox().Wall.Now()
	box, _, recCon, r := edgeBox(t, nil, vms.Coverage{From: 0, To: 1e12, Fragments: 5})
	r.Window, r.BackfillBudget = [2]int{int(time.Unix(int64(now), 0).Hour()+2) % 24, int(time.Unix(int64(now), 0).Hour()+3) % 24}, 0
	ask(box, "1-a", "1", now-76400, now-70000)

	r.PumpOnce() // the ORDINARY pass, not a direct call: a pass nobody runs is the bug this port has had twice
	eq(t, r.Fetched, []string{"1-a"})
	edge := 0
	for _, s := range vms.NewManifest(box.Archive, "1").Read() {
		if s.Source == "edge" {
			edge++
		}
	}
	if edge == 0 {
		t.Fatal("nothing arrived")
	}
	r.HeartbeatOnce()
	if !strings.Contains(p.Heartbeats(box.Objects, "rec/")["r-1"].ExtraString("fetched", ""), "1-a") {
		t.Fatal("the worker did not say so")
	}
	if vms.ClearRequests(recCon) != 1 || len(requestIDs(box)) != 0 {
		t.Fatal("the console did not remove it")
	}
}

func TestARequestForSomebodyElsesRecordingIsLeftAlone(t *testing.T) {
	// Every recorder reads the same prefix. The camera here is held and its device answers — so the only
	// thing that can refuse this range is that the recording belongs to another recorder. Fetching it would
	// write another server's unit.
	box, _, _, _ := edgeBox(t, nil, vms.Coverage{From: 0, To: 1e12, Fragments: 5})
	arch := vms.NewArchiveResource(box.Spool+"2", box.Archive+"2", 600, box.Wall.Now)
	other, err := vms.NewRecWorker("r-2", box.Vars.AsWriter("recworker", "rec/epoch/*", "rec/slots/*"), box.Objects, vms.NewFakeActuator(), arch,
		vms.VmsWorkerOptions{Server: "srv-2", Env: vms.Env{}, WorkerOptions: p.WorkerOptions{Clock: box.Clock.Now, Wall: box.Wall.Now}})
	if err != nil {
		t.Fatal(err)
	}
	other.ReconcileOnce() // …and this recorder holds NOTHING
	if len(other.Rows) != 0 {
		t.Fatal(other.Rows)
	}
	if _, _, ok := other.DeviceSource("1"); !ok {
		t.Fatal("the device has to be right there, answering, for this test to mean anything")
	}
	now := box.Wall.Now()
	ask(box, "1-a", "1", now-76400, now-70000)
	if got := other.Requests(2); len(got) != 0 || len(other.Fetched) != 0 {
		t.Fatal(got, other.Fetched)
	}
	eq(t, requestIDs(box), []string{"1-a"}) // still asked, for whoever holds it
}

func TestABackfillWithNoRecorderBehindTheConsoleIsRefusedAndNotAccepted(t *testing.T) {
	box := testbox.NewBox()
	vctl := vms.NewVmsController(box.Vars.AsWriter("console", vms.Spec.ACLConsole()...), box.Objects, 0, box.Wall.Now)
	h := vms.NewHandler(vctl, vms.NewArchiveResource(box.Spool, box.Archive, 600, box.Wall.Now), box.Wall.Now, nil)
	rw := httptest.NewRecorder()
	h.ServeHTTP(rw, httptest.NewRequest("POST", "/backfill", bytes.NewBufferString(`{"cam":"7","from":100,"to":200}`)))
	if rw.Code != 503 {
		t.Fatalf("%d %s", rw.Code, rw.Body)
	}
}

func TestARequestWaitsWhileTheDiskIsOverTheMark(t *testing.T) {
	// A person asking does not open this door: a disk the resource is emptying this minute cannot be given
	// more, however politely.
	box, _, _, r := edgeBox(t, nil, vms.Coverage{From: 0, To: 1e12, Fragments: 5})
	now := box.Wall.Now()
	box.Vars.Put(p.SpaceKey, p.Items{"enabled": "true", "high": "0.85", "low": "0.75"}, p.Absent)
	r.SpaceProbe = func(string) (int64, int64) { return 1_000_000, 100_000 } // 90 % full
	ask(box, "1-b", "1", now-76400, now-70000)
	if got := r.Requests(2); len(got) != 0 || len(r.Fetched) != 0 {
		t.Fatal(got)
	}
	eq(t, requestIDs(box), []string{"1-b"}) // kept: it is a wait, not a refusal
	r.SpaceProbe = func(string) (int64, int64) { return 1_000_000, 500_000 }
	if got := r.Requests(2); len(got) != 1 {
		t.Fatal("room again, same request")
	}
	eq(t, r.Fetched, []string{"1-b"})
}

func TestARequestTheRecorderCouldNotServeIsNotReportedAsServed(t *testing.T) {
	// A recorder whose lease lapsed fetches nothing. Saying `fetched` anyway would have the console delete a
	// request nobody served — and the operator's range would silently never arrive.
	box, _, _, r := edgeBox(t, nil, vms.Coverage{From: 0, To: 1e12, Fragments: 5})
	now := box.Wall.Now()
	ask(box, "1-c", "1", now-76400, now-70000)
	box.Clock.Advance(26) // past the lease, short of a renewal
	if r.MayWrite("1") {
		t.Fatal("expired")
	}
	r.Requests(2)
	if len(r.Fetched) != 0 {
		t.Fatalf("reported as fetched with no lease: %v", r.Fetched)
	}
	eq(t, requestIDs(box), []string{"1-c"})
}

// -- a scan asking for footage ------------------------------------------------------------------------

func TestAJobWaitingOnTheDeviceAsksTheRecorderOnce(t *testing.T) {
	// The scan does not read the device itself: that door admits two sessions and they belong to the
	// operator watching the gap and to the recorder saving it. So the range is fetched once, into our
	// archive, and the scan runs over footage we own.
	box := testbox.NewBox()
	ctl, adm := jobCtls(box)
	rec := console(box, vms.RecSpec)
	jobHeartbeat(box, "j-1", "srv-1")
	job := aJob(t, ctl, "7-lpr-1")
	adm.EnsurePlaced(nil)

	jobHeartbeat(box, "j-1", "srv-1", map[string]any{"id": job, "phase": "fetching", "rec": "7", "cam": "7",
		"from": 100.0, "to": 200.0, "why": "the device has these minutes"})
	reaped(t, ctl, 0, 0)
	if p.Str(ctl.Unit(job)["state"]) != "fetching" || adm.Placement(job) == nil {
		t.Fatalf("the operator sees why it is not running, and it keeps its worker: %v", ctl.Unit(job))
	}
	if vms.AskForFootage(ctl, rec, 60) != 1 {
		t.Fatal("not asked")
	}
	it, _, _ := box.Vars.Get(vms.RecSpec.Sub().RequestKey("7-100-200"))
	if it["unit"] != "7" || it["by"] != "detjob/"+job {
		t.Fatalf("%v", it)
	}
	if vms.AskForFootage(ctl, rec, 60) != 0 { // a pass every 30 s writes one row, not a queue
		t.Fatal("asked twice")
	}

	// A running scan's entry carries the same rec/from/to. It is reading footage we have; asking the
	// recorder for it would fetch minutes already here.
	jobHeartbeat(box, "j-1", "srv-1", map[string]any{"id": job, "phase": "running", "rec": "7", "cam": "7",
		"from": 300.0, "to": 400.0})
	if vms.AskForFootage(ctl, rec, 60) != 0 {
		t.Fatal("a running job asked for footage")
	}
}

func TestWhenTheFootageArrivesTheJobGoesBackToRunning(t *testing.T) {
	box := testbox.NewBox()
	ctl, adm := jobCtls(box)
	jobHeartbeat(box, "j-1", "srv-1")
	job := aJob(t, ctl, "7-lpr-1")
	adm.EnsurePlaced(nil)
	jobHeartbeat(box, "j-1", "srv-1", map[string]any{"id": job, "phase": "fetching", "rec": "7", "cam": "7", "from": 100.0, "to": 200.0})
	vms.Reap(ctl, 60)
	jobHeartbeat(box, "j-1", "srv-1", map[string]any{"id": job, "phase": "running", "done_through": 150.0})
	vms.Reap(ctl, 60)
	if p.Str(ctl.Unit(job)["state"]) != "running" {
		t.Fatalf("%v", ctl.Unit(job))
	}
}

// -- footage that arrives from a device is a hole in the detections too ----------------------------------

func dets(t *testing.T, box *testbox.Box, cam string, enabled bool, kinds ...string) *p.SpecController {
	t.Helper()
	det := console(box, vms.DetSpec)
	for _, k := range kinds {
		if _, err := det.Create(p.Row{"name": cam + "-" + k, "cam": cam, "kind": k, "enabled": enabled, "params": k + "-settings"}); err != nil {
			t.Fatal(err)
		}
	}
	return det
}

func recorderClosed(box *testbox.Box, spans ...string) {
	box.Objects.Put(vms.RecSpec.Sub().HeartbeatKey("r-1"), p.Heartbeat{Worker: "r-1", Ts: box.Wall.Now(),
		Extra: map[string]any{"server": "srv-1", "closed": strings.Join(spans, ",")}}.ToBytes())
}

func recOf(t *testing.T, box *testbox.Box, cam string) *p.SpecController {
	t.Helper()
	rec := console(box, vms.RecSpec)
	if _, err := rec.Create(map[string]any{"cam": cam}); err != nil {
		t.Fatal(err)
	}
	return rec
}

func TestWhatArrivedFromADeviceIsScannedByEveryDetectorOfThatCamera(t *testing.T) {
	// The two holes are the same minutes: nothing was recording the camera, so nothing was watching it
	// either. One scan per detector, with the detector's own settings — a retro scan run with different
	// parameters is not the same answer.
	box := testbox.NewBox()
	ctl, _ := jobCtls(box)
	rec, det := recOf(t, box, "7"), dets(t, box, "7", true, "lpr", "linecross")
	recorderClosed(box, "7|1000|1600")
	if n := vms.ScanWhatArrived(rec, det, ctl); n != 2 {
		t.Fatal(n)
	}
	got := map[string]p.Row{}
	for _, j := range ctl.Units() {
		got[p.Str(j["id"])] = j
	}
	j, ok := got["7-lpr-1000-1600"]
	if len(got) != 2 || !ok || got["7-linecross-1000-1600"] == nil {
		t.Fatalf("%v", got)
	}
	if p.Str(j["rec"]) != "7" || p.Str(j["cam"]) != "7" || p.ToFloat(j["from"]) != 1000 || p.ToFloat(j["to"]) != 1600 ||
		p.Str(j["params"]) != "lpr-settings" || p.Str(j["state"]) != "queued" {
		t.Fatalf("%v", j)
	}
}

func TestThePassRunsEveryThirtySecondsAndMakesTheJobOnce(t *testing.T) {
	box := testbox.NewBox()
	ctl, _ := jobCtls(box)
	rec, det := recOf(t, box, "7"), dets(t, box, "7", true, "lpr")
	recorderClosed(box, "7|1000|1600")
	if vms.ScanWhatArrived(rec, det, ctl) != 1 || vms.ScanWhatArrived(rec, det, ctl) != 0 || len(ctl.Units()) != 1 {
		t.Fatal("the id IS the range: the second pass finds the row already there")
	}
}

func TestAJobAnOperatorDeletedDoesNotComeBack(t *testing.T) {
	// Unit() stops seeing a deleted row; the row itself stays, marked. Reading the marker is what keeps a
	// person's decision from being undone by a pass.
	box := testbox.NewBox()
	ctl, _ := jobCtls(box)
	rec, det := recOf(t, box, "7"), dets(t, box, "7", true, "lpr")
	recorderClosed(box, "7|1000|1600")
	vms.ScanWhatArrived(rec, det, ctl)
	if err := ctl.Delete("7-lpr-1000-1600"); err != nil {
		t.Fatal(err)
	}
	if ctl.Unit("7-lpr-1000-1600") != nil || vms.ScanWhatArrived(rec, det, ctl) != 0 {
		t.Fatal("a deleted scan came back")
	}
}

func TestADetectorThatIsOffDoesNotScanThePast(t *testing.T) {
	box := testbox.NewBox()
	ctl, _ := jobCtls(box)
	rec, det := recOf(t, box, "7"), dets(t, box, "7", false, "lpr")
	recorderClosed(box, "7|1000|1600")
	if vms.ScanWhatArrived(rec, det, ctl) != 0 {
		t.Fatal("a detector that is off scanned the past")
	}
}

func TestAnotherCamerasDetectorIsNotPointedAtThisFootage(t *testing.T) {
	box := testbox.NewBox()
	ctl, _ := jobCtls(box)
	rec, det := recOf(t, box, "7"), dets(t, box, "9", true, "lpr")
	recorderClosed(box, "7|1000|1600")
	if vms.ScanWhatArrived(rec, det, ctl) != 0 {
		t.Fatal("camera 9's detector was pointed at camera 7")
	}
}

func TestTheHoleInTheFootageAndTheHoleInTheDetectionsCloseTogether(t *testing.T) {
	// End to end, with the real recorder: the card's minutes arrive, the recorder says which range it closed,
	// and the console turns that into a scan by every detector of that camera.
	box, _, recCon, r := edgeBox(t, nil, vms.Coverage{From: 0, To: 1e12, Fragments: 5})
	det := dets(t, box, "1", true, "lpr")
	jobs := console(box, vms.DetJobSpec)
	now := box.Wall.Now()
	done := r.Backfill(1, now, true) // the link came back
	if len(done) == 0 || done[0].Segments == 0 {
		t.Fatal(done)
	}
	r.HeartbeatOnce()
	if p.Heartbeats(box.Objects, "rec/")["r-1"].ExtraString("closed", "") == "" {
		t.Fatal("the recorder closed a range and told nobody")
	}
	if vms.ScanWhatArrived(recCon, det, jobs) != 1 {
		t.Fatal("no scan was queued")
	}
	j := jobs.Units()[0]
	if p.Str(j["rec"]) != "1" || p.ToFloat(j["from"]) != float64(int64(done[0].From)) || p.ToFloat(j["to"]) != float64(int64(done[0].To)) {
		t.Fatalf("%v vs %v", j, done[0])
	}
}

func TestAFetchThatBroughtNothingNewQueuesNoScan(t *testing.T) {
	// kept == 0 usually means live recording reached those minutes while we were fetching. Those minutes are
	// already ours AND were already watched by the live detector; reporting them as newly arrived would scan
	// them a second time and double every event in them.
	box, _, recCon, r := edgeBox(t, nil, vms.Coverage{From: 0, To: 1e12, Fragments: 5})
	det := dets(t, box, "1", true, "lpr")
	jobs := console(box, vms.DetJobSpec)
	now := box.Wall.Now()
	start, end := now-80000, now-76400
	pth := vms.SegmentPath(box.Archive, "1", r.Epochs["1"], time.Unix(int64(start), 0).UTC())
	os.MkdirAll(filepath.Dir(pth), 0o755)
	os.WriteFile(pth, []byte("x"), 0o644)
	rel, _ := filepath.Rel(box.Archive, pth)
	vms.NewManifest(box.Archive, "1").Append(vms.Segment{Unit: "1", Epoch: r.Epochs["1"], Start: start, End: end, Path: rel, Bytes: 1, Source: "live"})

	if got := r.Fetch("1", "1", "http://srv-1:8083/playback/1", start, end); got.Segments != 0 {
		t.Fatal(got)
	}
	r.HeartbeatOnce()
	if c := p.Heartbeats(box.Objects, "rec/")["r-1"].ExtraString("closed", ""); c != "" {
		t.Fatalf("minutes we already had were announced as newly arrived: %q", c)
	}
	if vms.ScanWhatArrived(recCon, det, jobs) != 0 {
		t.Fatal("a scan was queued over minutes already watched")
	}
}

// -- the survey's hits --------------------------------------------------------------------------------

func TestTheConsoleTurnsAKeptStretchIntoARequest(t *testing.T) {
	box := testbox.NewBox()
	survey := console(box, vms.SurveySpec)
	rec := console(box, vms.RecSpec)
	rec.Create(map[string]any{"cam": "7", "enabled": false}) // a tree and a retention; no live recording
	box.Objects.Put(vms.SURVEY.HeartbeatKey("s-1"), p.Heartbeat{Worker: "s-1", Ts: box.Wall.Now(),
		Extra: map[string]any{"server": "srv-1", "hits": "7|1000|1120"}}.ToBytes())
	if vms.KeepWhatFired(survey, rec) != 1 {
		t.Fatal("not asked")
	}
	it, _, _ := box.Vars.Get(vms.RecSpec.Sub().RequestKey("7-1000-1120"))
	if it["unit"] != "7" || it["by"] != "survey/7" {
		t.Fatalf("%v", it)
	}
	if vms.KeepWhatFired(survey, rec) != 0 { // one row, not a queue
		t.Fatal("asked twice")
	}
	if rec.Unit("7").Bool("enabled") || p.ToFloat(rec.Unit("7")["retention_days"]) != 30 {
		t.Fatalf("one archive, one retention, still nothing recorded live: %v", rec.Unit("7"))
	}
}

func TestACameraNothingRecordsHereHasNowhereToKeepIt(t *testing.T) {
	box := testbox.NewBox()
	survey := console(box, vms.SurveySpec)
	rec := console(box, vms.RecSpec)
	box.Objects.Put(vms.SURVEY.HeartbeatKey("s-1"), p.Heartbeat{Worker: "s-1", Ts: box.Wall.Now(),
		Extra: map[string]any{"server": "srv-1", "hits": "9|1000|1120"}}.ToBytes())
	if vms.KeepWhatFired(survey, rec) != 0 || len(requestIDs(box)) != 0 {
		t.Fatal("asked a recorder for a camera nothing records")
	}
}

// -- the device's INDEX ---------------------------------------------------------------------------------

func TestTheIndexIsADoorAndNotAField(t *testing.T) {
	// The summary answers "is there anything at all"; it cannot answer "is there anything at 10:05". And the
	// index is fetched rather than heartbeated, because the heartbeat is one object under a ceiling and this
	// one grows with the device.
	day := 86400.0
	_, w, _, _ := edgeBox(t, map[string][][2]float64{"1": {{100, 200}, {5000, 5600}}}, vms.Coverage{From: 0, To: day, Fragments: 5})
	srv := httptest.NewServer(w.PlaybackHandler())
	defer srv.Close()
	base := srv.URL + "/recordings/1"

	hb := p.Heartbeats(w.Objects, "vms/")["w-1"]
	if len(hb.Status) == 0 || !strings.Contains(p.Str(hb.Status[0]["index_url"]), "/recordings/1") {
		t.Fatalf("no door in the heartbeat: %v", hb.Status)
	}
	if _, carried := hb.Status[0]["spans"]; carried {
		t.Fatal("the index rode in the heartbeat")
	}
	spans, err := vms.DeviceRecordings(base, 0, day)
	if err != nil {
		t.Fatal(err)
	}
	eq(t, spans, [][2]float64{{100, 200}, {5000, 5600}})
	spans, _ = vms.DeviceRecordings(base, 150, 5200)
	eq(t, spans, [][2]float64{{150, 200}, {5000, 5200}})
	spans, _ = vms.DeviceRecordings(base, 1000, 4000)
	if vms.CoveredBy(spans, true, 1000, 4000) != 0 { // the whole point: inside the summary, and empty
		t.Fatal(spans)
	}
	if w.DeviceOfRow(w.Rows[0]).InUse() != 0 { // listing is not reading: no session taken
		t.Fatal("a session was taken to list")
	}
}

func TestADriverThatCannotListSaysSoAndIsNotReadAsEmpty(t *testing.T) {
	_, w, _, _ := edgeBox(t, nil, vms.Coverage{From: 0, To: 86400, Fragments: 5})
	srv := httptest.NewServer(w.PlaybackHandler())
	defer srv.Close()
	resp, _ := http.Get(srv.URL + "/recordings/1?from=0&to=100")
	if resp.StatusCode != 501 {
		t.Fatal(resp.Status)
	}
	if _, err := vms.DeviceRecordings(srv.URL+"/recordings/1", 0, 100); err != vms.ErrCannotList {
		t.Fatal(err)
	}
	if vms.CoveredBy(nil, false, 0, 100) != 100 || vms.CoveredBy(nil, true, 0, 100) != 0 {
		t.Fatal("cannot tell, and can tell and there is nothing, are two answers")
	}
}

func TestAJobIsNotPromisedMinutesTheDeviceDoesNotHave(t *testing.T) {
	// `fetching` says "the footage exists, it is simply not ours yet". Said about a gap in the device's own
	// recording it is a promise nothing can keep.
	box, w, _, _ := edgeBox(t, map[string][][2]float64{"1": {{100, 200}}}, vms.Coverage{From: 0, To: 86400, Fragments: 5})
	srv := httptest.NewServer(w.PlaybackHandler())
	defer srv.Close()
	// the heartbeat names the holder's own port; in the test the door is on another
	hb := p.Heartbeats(box.Objects, "vms/")["w-1"]
	hb.Status[0]["index_url"] = srv.URL + "/recordings/1"
	box.Objects.Put("vms/heartbeats/w-1", hb.ToBytes())

	j := jobWorker(t, box, "j-1")
	if !j.DeviceHas("1", 100, 200) {
		t.Fatal("a span it really has")
	}
	if j.DeviceHas("1", 1000, 2000) {
		t.Fatal("inside the summary, and empty")
	}
	j.Index = func(string, float64, float64) ([][2]float64, error) { return nil, vms.ErrCannotList }
	if !j.DeviceHas("1", 1000, 2000) {
		t.Fatal("a driver that cannot list refused work that might succeed")
	}
}

// -- the scan's side of it --------------------------------------------------------------------------------

func holderOfCamera(box *testbox.Box, cov map[string]any) {
	st := map[string]any{"id": "7", "phase": "running", "live_url": "rtsp://srv-1:8554/7"}
	if cov != nil {
		st["coverage"] = cov
	}
	box.Objects.Put("vms/heartbeats/w-1", p.Heartbeat{Worker: "w-1", Ts: box.Wall.Now(), Status: []map[string]any{st},
		Extra: map[string]any{"server": "srv-1", "capacity": 50, "headroom": 49}}.ToBytes())
}

func TestFootageTheDeviceHasAndWeDoNotIsAStepNotADeadEnd(t *testing.T) {
	// `fetching`, not `waiting`: the minutes exist, they are simply not ours yet.
	box := testbox.NewBox()
	makeJob(t, box, "7-lpr-1", mm(0), mm(10)) // no manifest on this server
	holderOfCamera(box, map[string]any{"from": mm(-100), "to": mm(100), "fragments": 5})
	w := jobWorker(t, box, "j-1")
	w.ReconcileOnce()
	st := w.Status("7-lpr-1")
	if p.Str(st["phase"]) != "fetching" || !strings.Contains(p.Str(st["why"]), "device") {
		t.Fatalf("%v", st)
	}
	if p.Str(st["rec"]) != "7" || p.ToFloat(st["from"]) != mm(0) || p.ToFloat(st["to"]) != mm(10) {
		t.Fatalf("the console asks the recorder from this entry and nothing else: %v", st)
	}
	if _, took := w.Epochs["7-lpr-1"]; took {
		t.Fatal("nothing is being written yet")
	}
}

func TestNobodyRecordedItIsADifferentAnswer(t *testing.T) {
	box := testbox.NewBox()
	makeJob(t, box, "7-lpr-1", mm(0), mm(10))
	holderOfCamera(box, nil) // held, but the device has no archive
	w := jobWorker(t, box, "j-1")
	w.ReconcileOnce()
	if p.Str(w.Status("7-lpr-1")["phase"]) != "waiting" {
		t.Fatalf("%v", w.Status("7-lpr-1"))
	}
}

func TestADeviceWhoseCoverageMissesTheIntervalIsNotAsked(t *testing.T) {
	// The card keeps three days. A search over last month is not a fetch that will ever succeed, and
	// saying `fetching` would leave the job hopeful for ever.
	box := testbox.NewBox()
	makeJob(t, box, "7-lpr-1", mm(0), mm(10))
	holderOfCamera(box, map[string]any{"from": mm(500), "to": mm(900), "fragments": 5})
	w := jobWorker(t, box, "j-1")
	w.ReconcileOnce()
	if p.Str(w.Status("7-lpr-1")["phase"]) != "waiting" {
		t.Fatalf("%v", w.Status("7-lpr-1"))
	}
}

func TestTheConsoleFrontsEverySubsystemItWritesFor(t *testing.T) {
	// The console holds the grant for the scans' and the surveys' rows. Without a screen for them an operator
	// could not ask for a scan or start a watch, and the grant would be for the reaper alone.
	box := testbox.NewBox()
	vctl := vms.NewVmsController(box.Vars.AsWriter("console", vms.Spec.ACLConsole()...), box.Objects, 0, box.Wall.Now)
	h := vms.NewConsoleWith(vctl, nil, box.Wall.Now, console(box, vms.RecSpec),
		[]*p.SpecController{console(box, vms.DetSpec), console(box, vms.DetJobSpec), console(box, vms.SurveySpec)}).Handler()
	for _, sub := range []string{"det", "detjob", "survey"} {
		rw := httptest.NewRecorder()
		h.ServeHTTP(rw, httptest.NewRequest("GET", "/"+sub+"/spec", nil))
		var spec map[string]any
		json.Unmarshal(rw.Body.Bytes(), &spec)
		if rw.Code != 200 || spec["name"] != sub {
			t.Fatalf("/%s/spec: %d %s", sub, rw.Code, rw.Body)
		}
	}
}
