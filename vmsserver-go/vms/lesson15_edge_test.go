package vms_test

// Lessons 15 and 16 — the archive we did not write, and backfill from the edge.
//
// A camera can have a card of its own, and behind one DriverPack connection
// there can be an NVR with thirty-two channels. The holder holds the DEVICE,
// not the channel: one session, N cameras, two kinds of output — the stream now
// (live_url) and the footage the device already has (playback_url + coverage).
// Reading that footage is not a subsystem: it wants exactly the reachability the
// holder already has, and a subsystem is earned by a DIFFERENT placement axis,
// not by different work.
//
// What IS the recorder's is copying it: the card exists because the camera kept
// recording while we could not, so replication is the difference between two
// coverages — Lesson 2's loop over time. What it fetches becomes ours: our
// manifest, our epoch, our retention, marked source: edge.

import (
	"bytes"
	"encoding/json"
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

	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/w2cplatform"
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
	ctl := vms.NewVmsController(box.Vars, box.Objects, 0, box.Wall.Now)
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
		mustCreate(t, ctl, map[string]any{"name": "nvr-" + ch, "source": nvr + ch})
	}
	mustCreate(t, ctl, map[string]any{"name": "front", "source": card})
	ctl.EnsurePlaced(nil)
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
	ctl := vms.NewVmsController(box.Vars, box.Objects, 0, box.Wall.Now)
	act := vms.NewFakeActuator()
	w := holder(t, box, func(k string) vms.Device {
		return vms.NewFakeDevice(k, []string{"1", "2"}, map[string]vms.Coverage{"1": {To: 9}, "2": {To: 9}})
	}, act)
	mustCreate(t, ctl, map[string]any{"name": "watched", "source": nvr + "1"})
	mustCreate(t, ctl, map[string]any{"name": "archive-only", "source": nvr + "2", "live": "on-demand"})
	ctl.EnsurePlaced(nil)

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
	ctl := vms.NewVmsController(box.Vars, box.Objects, 0, box.Wall.Now)
	dev := vms.NewFakeDevice("acme/10.0.0.7", []string{"1"}, map[string]vms.Coverage{"1": {To: 100}})
	dev.MaxPlays = 2
	w := holder(t, box, func(string) vms.Device { return dev }, nil)
	mustCreate(t, ctl, map[string]any{"name": "front", "source": card})
	ctl.EnsurePlaced(nil)
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

func TestTheConsoleDrawsTheDeviceOnlyWhereWeHaveNothing(t *testing.T) {
	// Our footage wins; the device's coverage is drawn in the holes. The same
	// subtraction the recorder fetches by — one rule, two uses. A span that exists
	// only on the device is the one that will disappear when the ring wraps.
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars, box.Objects, 0, box.Wall.Now)
	w := holder(t, box, func(k string) vms.Device {
		return vms.NewFakeDevice(k, []string{"1"}, map[string]vms.Coverage{"1": {0, 1000, 7}})
	}, nil)
	mustCreate(t, ctl, map[string]any{"name": "front", "source": card})
	ctl.EnsurePlaced(nil)
	w.ReconcileOnce()
	w.HeartbeatOnce()

	ours := []map[string]any{{"start": 200.0, "end": 400.0}, {"start": 600.0, "end": 700.0}}
	spans := vms.DeviceSpans(box.Objects, "1", ours, 0, 1000, box.Wall.Now())
	got := [][2]float64{}
	for _, s := range spans {
		got = append(got, [2]float64{p.ToFloat(s["start"]), p.ToFloat(s["end"])})
		if s["source"] != "device" || s["media"] != nil {
			t.Fatal(s)
		}
	}
	eq(t, got, [][2]float64{{0, 200}, {400, 600}, {700, 1000}})

	// and the whole timeline over HTTP: ours and the device's, sorted, in one answer
	arch := vms.NewArchiveResource(box.Spool, box.Archive, 600, box.Wall.Now)
	recCon := p.NewSpecController(vms.RecSpec, box.Vars, box.Objects, 0, box.Wall.Now, "")
	srv, ln, err := vms.Serve(ctl, arch, "127.0.0.1:0", box.Wall.Now, recCon)
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	base := "http://" + ln.Addr().String()

	resp, _ := http.Get(base + "/timeline/1?from=0&to=1000")
	var timeline []map[string]any
	json.NewDecoder(resp.Body).Decode(&timeline)
	resp.Body.Close()
	if len(timeline) != 1 || timeline[0]["source"] != "device" { // nothing of ours yet: all of it is theirs
		t.Fatal(timeline)
	}

	resp, _ = http.Get(base + "/segment?cam=1&from=10&to=20")
	var seg map[string]any
	json.NewDecoder(resp.Body).Decode(&seg)
	resp.Body.Close()
	if seg["playback"] != "http://srv-1:8083/playback/1?from=10&to=20" {
		t.Fatal(seg)
	}

	// and the operator's ask: accepted, not done here — the recorder does the work. Accepted means STORED:
	// this answered 202 for a long time and wrote nothing down.
	resp, _ = http.Post(base+"/backfill", "application/json", jsonBody(map[string]any{"cam": 1, "from": 0, "to": 100}))
	resp.Body.Close()
	if resp.StatusCode != 202 {
		t.Fatal(resp.StatusCode)
	}
	if it, _, _ := box.Vars.Get(vms.RecSpec.Sub().RequestKey("1-0-100")); it == nil {
		t.Fatal("202, and nothing was asked of anybody")
	}
}

func jsonBody(v any) io.Reader {
	b, _ := json.Marshal(v)
	return bytes.NewReader(b)
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
	ctl := vms.NewVmsController(box.Vars, box.Objects, 0, box.Wall.Now)
	w := holder(t, box, func(k string) vms.Device {
		return vms.NewFakeDevice(k, []string{"1"}, map[string]vms.Coverage{"1": {0, 1000000, 5}})
	}, nil)
	mustCreate(t, ctl, map[string]any{"name": "front", "source": card})
	ctl.EnsurePlaced(nil)
	w.ReconcileOnce()
	w.HeartbeatOnce()

	recCtl := p.NewSpecController(vms.RecSpec, box.Vars, box.Objects, 0, box.Wall.Now, "")
	if _, err := recCtl.Create(map[string]any{"cam": "1"}); err != nil {
		t.Fatal(err)
	}
	arch := vms.NewArchiveResource(box.Spool, box.Archive, 600, box.Wall.Now)
	const now = 1000000.0 // backfill takes its own `now`; the heartbeats keep the box's
	r, err := vms.NewRecWorker("r-1", box.Vars, box.Objects, vms.NewFakeActuator(), arch,
		vms.VmsWorkerOptions{Server: "srv-1", Env: vms.Env{}, WorkerOptions: p.WorkerOptions{Clock: box.Clock.Now, Wall: box.Wall.Now}})
	if err != nil {
		t.Fatal(err)
	}
	r.Window, r.KeepDays, r.Settle = [2]int{22, 6}, 1, 1000
	r.HeartbeatOnce()
	recCtl.EnsurePlaced(nil)
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
