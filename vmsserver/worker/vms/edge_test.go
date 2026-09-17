package vms_test

// The edge: what the worker does with a device that recorded while we could not — and what it refuses to
// do when the disk it would write onto is already over its mark.
//
// The setup here writes rows and assignments straight into the store, because that is all a worker can
// see of a controller. Placement is Python's; these tests are about what happens AFTER it.

import (
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"vmsserver/worker/testbox"
	"vmsserver/worker/vms"
	p "vmsserver/worker/w2cplatform"
)

const (
	nvr  = "driverpack://acme/10.0.0.50/ch/" // + channel
	card = "driverpack://acme/10.0.0.7"
)

// The holder claims its slot and heartbeats first: a controller places on
// workers it can see.
func holder(t *testing.T, box *testbox.Box, factory func(string) vms.Device, act *vms.FakeActuator) *vms.VmsWorker {
	t.Helper()
	if act == nil {
		act = vms.NewFakeActuator()
	}
	w := worker(t, box, "w-1", act, vms.VmsWorkerOptions{Server: "srv-1", ArchiveRoot: box.Archive, DeviceFactory: factory})
	w.HeartbeatOnce()
	return w
}

func TestOneSessionPerDeviceHoweverManyChannelsAreAssigned(t *testing.T) {
	// Thirty-two channels of one NVR are one connection, not thirty-two: the same
	// argument as one connection to a camera, a level up. A camera with a card is
	// the degenerate case — a device with one channel.
	box := testbox.NewBox()
	ctl := p.NewController(vms.VMS, box.Vars, box.Objects, box.Wall.Now)
	var opened []string
	factory := func(key string) vms.Device {
		opened = append(opened, key)
		chans := []string{"1"}
		if key == "acme/10.0.0.50" {
			chans = nil
			for c := 1; c <= 32; c++ {
				chans = append(chans, strconv.Itoa(c))
			}
		}
		return vms.NewFakeDevice(key, chans, map[string]vms.Coverage{
			"1": {100, 400, 3}, "2": {From: 100, To: 400}, "3": {From: 100, To: 400}, "4": {From: 100, To: 400}})
	}
	w := holder(t, box, factory, nil)
	for _, ch := range []string{"17", "18", "19"} {
		addCamera(t, box, ctl, p.Items{"name": "nvr-" + ch, "source": nvr + ch})
	}
	addCamera(t, box, ctl, p.Items{"name": "front", "source": card})
	assignAll(t, box, ctl, "w-1")
	w.ReconcileOnce()

	sort.Strings(opened)
	eq(t, opened, []string{"acme/10.0.0.50", "acme/10.0.0.7"}) // two devices, four cameras
	if vms.DeviceOf(nvr+"17") != vms.DeviceOf(nvr+"18") || vms.DeviceOf(nvr+"17") != "acme/10.0.0.50" {
		t.Fatal(vms.DeviceOf(nvr + "17"))
	}
	if vms.ChannelOf(nvr+"17") != "17" || vms.ChannelOf(card) != "" {
		t.Fatal(vms.ChannelOf(nvr+"17"), vms.ChannelOf(card))
	}

	// the discovery: an OBSERVATION in the heartbeat, never a row the worker writes
	dev := map[string]map[string]any{}
	for _, d := range w.DeviceStatus() {
		dev[p.Str(d["device"])] = d
	}
	if dev["acme/10.0.0.50"]["channels"] != 32 || len(dev["acme/10.0.0.50"]["unimported"].([]string)) != 29 {
		t.Fatal(dev["acme/10.0.0.50"])
	}

	// and the second kind of output, beside the live one
	st := map[string]map[string]any{}
	for _, s := range w.Status() {
		st[p.Str(s["id"])] = s
	}
	if p.Str(st["1"]["playback_url"]) != "http://srv-1:8083/playback/1" {
		t.Fatal(st["1"])
	}
	eq(t, st["1"]["coverage"], map[string]any{"from": 100.0, "to": 400.0, "fragments": 3})
}

func TestAChannelKeptForItsArchiveIsHeldAndNotStreamed(t *testing.T) {
	// `live: on-demand` — the device is on the line, its footage is served, and no
	// live pipeline is built. Thirty-two channels imported for their footage would
	// otherwise be thirty-two streams nobody watches.
	box := testbox.NewBox()
	ctl := p.NewController(vms.VMS, box.Vars, box.Objects, box.Wall.Now)
	act := vms.NewFakeActuator()
	w := holder(t, box, func(k string) vms.Device {
		return vms.NewFakeDevice(k, []string{"1", "2"}, map[string]vms.Coverage{"1": {To: 9}, "2": {To: 9}})
	}, act)
	addCamera(t, box, ctl, p.Items{"name": "watched", "source": nvr + "1"})
	addCamera(t, box, ctl, p.Items{"name": "archive-only", "source": nvr + "2", "live": "on-demand"})
	assignAll(t, box, ctl, "w-1")

	eq(t, w.ReconcileOnce(), actions("start 1")) // only the watched one gets a pipeline
	eq(t, act.RunningIDs(), []string{"1"})
	st := map[string]map[string]any{}
	for _, s := range w.Status() {
		st[p.Str(s["id"])] = s
	}
	if st["1"]["phase"] != "running" || st["2"]["phase"] != "held" {
		t.Fatal(st["1"]["phase"], st["2"]["phase"])
	}
	if p.Str(st["2"]["playback_url"]) == "" { // held means its archive is still served
		t.Fatal(st["2"])
	}
	if w.Headroom() != 48 { // both rows still cost capacity
		t.Fatal(w.Headroom())
	}
}

func TestTheDevicesCeilingIsTheDevicesNotTheWorkers(t *testing.T) {
	// Capacity here is cameras; how many playbacks a device allows is the
	// hardware's own number, and an exhausted device is a 503 — admission control
	// inside the process, the way the gateway refuses a viewer. On a camera this
	// competes with live for the one uplink.
	box := testbox.NewBox()
	ctl := p.NewController(vms.VMS, box.Vars, box.Objects, box.Wall.Now)
	dev := vms.NewFakeDevice("acme/10.0.0.7", []string{"1"}, map[string]vms.Coverage{"1": {To: 100}})
	dev.MaxPlays = 2
	w := holder(t, box, func(string) vms.Device { return dev }, nil)
	addCamera(t, box, ctl, p.Items{"name": "front", "source": card})
	assignAll(t, box, ctl, "w-1")
	w.ReconcileOnce()

	got, err := w.Playback("1", 0, 10)
	if err != nil || len(got) != 10 { // a range comes back
		t.Fatal(len(got), err)
	}
	s1, _ := dev.OpenPlayback("1", 0, 1) // someone else is scrubbing
	s2, _ := dev.OpenPlayback("1", 2, 3)
	if _, err := w.Playback("1", 0, 10); !errors.Is(err, vms.ErrDeviceBusy) {
		t.Fatal("the device had no session left:", err)
	}
	dev.ClosePlayback(s1)
	dev.ClosePlayback(s2)

	srv, ln, err := w.ServePlayback("127.0.0.1:0") // the holder's own door
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	base := "http://" + ln.Addr().String()
	resp, err := http.Get(base + "/playback/1?from=0&to=5")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 || len(body) != 5 {
		t.Fatal(resp.StatusCode, len(body))
	}
	resp, _ = http.Get(base + "/playback/99?from=0&to=5")
	resp.Body.Close()
	if resp.StatusCode != 404 { // camera 99 has no archive here
		t.Fatal(resp.StatusCode)
	}
}

// noon: a wall time that is certainly inside the day, whatever the machine's zone.
func noon(now float64) float64 {
	return now + float64(12-time.Unix(int64(now), 0).Hour())*3600
}

func TestBackfillClosesOurGapsAndWhatItFetchesIsOurs(t *testing.T) {
	// The card exists because the camera kept recording while we could not, so
	// replication is the difference between two coverages — Lesson 2's loop over
	// time. What comes back is written as ours: our manifest, our epoch, our
	// retention, marked source: edge.
	box := testbox.NewBox()
	ctl := p.NewController(vms.VMS, box.Vars, box.Objects, box.Wall.Now)
	w := holder(t, box, func(k string) vms.Device {
		return vms.NewFakeDevice(k, []string{"1"}, map[string]vms.Coverage{"1": {0, 1000000, 5}})
	}, nil)
	addCamera(t, box, ctl, p.Items{"name": "front", "source": card})
	assignAll(t, box, ctl, "w-1")
	w.ReconcileOnce()
	w.HeartbeatOnce()

	recCtl := p.NewController(vms.REC, box.Vars, box.Objects, box.Wall.Now)
	setRow(t, box, "rec/recordings/1", p.Items{"id": "1", "cam": "1", "revision": "1"})
	arch := vms.NewArchiveResource(box.Spool, box.Archive, 600, box.Wall.Now)
	const now = 1000000.0 // backfill takes its own `now`; the heartbeats keep the box's
	r, err := vms.NewRecWorker("r-1", box.Vars, box.Objects, vms.NewFakeActuator(), arch,
		vms.VmsWorkerOptions{Server: "srv-1", Env: vms.Env{}, WorkerOptions: p.WorkerOptions{Clock: box.Clock.Now, Wall: box.Wall.Now}})
	if err != nil {
		t.Fatal(err)
	}
	r.Window, r.KeepDays, r.Settle = [2]int{22, 6}, 1, 1000
	r.HeartbeatOnce()
	recCtl.Assign("r-1", []string{"1"})
	r.ReconcileOnce()
	epoch := r.Epochs["1"]

	// two recordings of ours with an hour missing between them
	for _, span := range [][2]float64{{now - 80000, now - 76400}, {now - 70000, now - 66400}} {
		pth := vms.SegmentPath(box.Archive, "1", epoch, time.Unix(int64(span[0]), 0).UTC())
		os.MkdirAll(filepath.Dir(pth), 0o755)
		os.WriteFile(pth, []byte("x"), 0o644)
		rel, _ := filepath.Rel(box.Archive, pth)
		vms.NewManifest(box.Archive, "1").Append(vms.Segment{Unit: "1", Epoch: epoch, Start: span[0], End: span[1], Path: rel, Bytes: 1, Source: "live"})
	}
	eq(t, r.OurCoverage("1"), [][2]float64{{now - 80000, now - 76400}, {now - 70000, now - 66400}})

	gaps := r.Gaps("1", vms.Coverage{From: 0, To: now}, now)
	if !reflect.DeepEqual(gaps[len(gaps)-2], [2]float64{now - 76400, now - 70000}) { // the hole between the two
		t.Fatal(gaps)
	}
	for _, g := range gaps {
		if g[0] < now-86400 { // never older than our own retention
			t.Fatal("older than retention:", g)
		}
		if g[1] > now-1000 { // never fresher than the settle
			t.Fatal("inside the settle:", g)
		}
	}

	if done := r.Backfill(1, noon(now), false); len(done) != 0 {
		t.Fatal("midday local is not the window:", done)
	}
	done := r.Backfill(1, now, true) // the operator asked
	if len(done) == 0 || done[0].Segments == 0 {
		t.Fatal(done)
	}

	edge := []vms.Segment{}
	for _, s := range vms.NewManifest(box.Archive, "1").Read() {
		if s.Source == "edge" {
			edge = append(edge, s)
		}
	}
	if len(edge) == 0 {
		t.Fatal("nothing was written as ours")
	}
	for _, s := range edge {
		if s.Epoch != epoch { // the recorder's CURRENT epoch, not the device's idea of one
			t.Fatal(s)
		}
	}
	if r.Backfilled != len(edge) || !strings.Contains(r.MetricsText(), "rec_segments_backfilled") {
		t.Fatal(r.Backfilled, len(edge))
	}
	if n := r.Archive.Retain("1", 0, now+10); n < len(edge) { // ours: retention takes it like the rest
		t.Fatal(n, len(edge))
	}
}

func TestSubtractionIsOneRule(t *testing.T) {
	// The console draws with it and the recorder fetches with it; if they were two
	// functions they would drift.
	eq(t, vms.Subtract([2]float64{0, 100}, nil), [][2]float64{{0, 100}})
	eq(t, vms.Subtract([2]float64{0, 100}, [][2]float64{{0, 100}}), [][2]float64{})
	eq(t, vms.Subtract([2]float64{0, 100}, [][2]float64{{20, 40}, {60, 80}}), [][2]float64{{0, 20}, {40, 60}, {80, 100}})
	eq(t, vms.Subtract([2]float64{0, 100}, [][2]float64{{-10, 10}, {90, 200}}), [][2]float64{{10, 90}})
	eq(t, vms.Subtract([2]float64{0, 100}, [][2]float64{{40, 60}, {50, 70}}), [][2]float64{{0, 40}, {70, 100}})
	if !vms.Overlaps([][2]float64{{0, 10}}, [2]float64{5, 15}) || vms.Overlaps([][2]float64{{0, 10}}, [2]float64{10, 15}) {
		t.Fatal("a touching edge is not an overlap")
	}
}

func TestBackfillStopsWhileTheDiskIsOverTheMark(t *testing.T) {
	// Otherwise the two chase each other for ever: the resource frees space, the recorder fetches more of
	// the same hours back. KeepDays closes that trap in time; this closes it in space, and not even an
	// operator's `force` opens it — a range fetched now is a range the resource is about to delete.
	box := testbox.NewBox()
	ctl := p.NewController(vms.VMS, box.Vars, box.Objects, box.Wall.Now)
	w := holder(t, box, func(k string) vms.Device {
		return vms.NewFakeDevice(k, []string{"1"}, map[string]vms.Coverage{"1": {From: 0, To: 1_000_000, Fragments: 5}})
	}, nil)
	addCamera(t, box, ctl, p.Items{"name": "front", "source": card})
	assignAll(t, box, ctl, "w-1")
	w.ReconcileOnce()
	w.HeartbeatOnce()

	recCtl := p.NewController(vms.REC, box.Vars, box.Objects, box.Wall.Now)
	setRow(t, box, "rec/recordings/1", p.Items{"id": "1", "cam": "1", "revision": "1"})
	arch := vms.NewArchiveResource(box.Spool, box.Archive, 600, box.Wall.Now)
	r, err := vms.NewRecWorker("r-1", box.Vars, box.Objects, vms.NewFakeActuator(), arch,
		vms.VmsWorkerOptions{Server: "srv-1", Env: vms.Env{}, WorkerOptions: p.WorkerOptions{Clock: box.Clock.Now, Wall: box.Wall.Now}})
	if err != nil {
		t.Fatal(err)
	}
	r.KeepDays, r.Settle = 1, 1000
	r.HeartbeatOnce()
	recCtl.Assign("r-1", []string{"1"})
	r.ReconcileOnce()

	const now = 1_000_000.0
	if _, err := box.Vars.Put(p.SpaceKey, p.Items{"enabled": "true", "high": "0.85", "low": "0.75"}, p.Absent); err != nil {
		t.Fatal(err)
	}
	r.SpaceProbe = func(string) (int64, int64) { return 1_000_000, 100_000 } // 90 % full
	if !r.UnderPressure() {
		t.Fatal("over the mark and the recorder does not know it")
	}
	if done := r.Backfill(1, now, true); len(done) != 0 { // force does not open it either
		t.Fatal(done)
	}

	r.SpaceProbe = func(string) (int64, int64) { return 1_000_000, 500_000 } // room again…
	if r.UnderPressure() {
		t.Fatal("under the mark and still refusing")
	}
	done := r.Backfill(1, now, true) // …and the same call fetches
	if len(done) == 0 || done[0].Segments == 0 {
		t.Fatal(done)
	}
}

// -- evacuation: why there is no button -------------------------------------------------------------

// -- the rows a console and a controller would have written ------------------------------------------

// addCamera writes vms/cameras/<n> with the next free numeric id — what POST /cameras does on the Python
// side, minus everything the worker cannot see: no placement, no next_id row, no idempotency key.
func addCamera(t *testing.T, box *testbox.Box, ctl *p.Controller, items p.Items) string {
	t.Helper()
	keys, _ := box.Vars.List("vms/cameras/")
	id := strconv.Itoa(len(keys) + 1)
	items["id"], items["revision"] = id, "1"
	setRow(t, box, "vms/cameras/"+id, items)
	return id
}

// assignAll hands every camera row to one worker. Which worker, and why, is the Python controller's
// business (vms.subsystem.yaml's placement block); a worker only ever sees the answer.
func assignAll(t *testing.T, box *testbox.Box, ctl *p.Controller, worker string) {
	t.Helper()
	keys, _ := box.Vars.List("vms/cameras/")
	units := make([]string, 0, len(keys))
	for _, k := range keys {
		units = append(units, k[strings.LastIndex(k, "/")+1:])
	}
	sort.Slice(units, func(i, j int) bool { a, _ := strconv.Atoi(units[i]); b, _ := strconv.Atoi(units[j]); return a < b })
	if _, err := ctl.Assign(worker, units); err != nil {
		t.Fatal(err)
	}
}

func setRow(t *testing.T, box *testbox.Box, key string, items p.Items) {
	t.Helper()
	_, idx, _ := box.Vars.Get(key)
	if _, err := box.Vars.Put(key, items, idx); err != nil {
		t.Fatal(err)
	}
}
