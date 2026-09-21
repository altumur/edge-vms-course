package vms_test

// Lesson 14 (and М10B Lesson 9 in the Python port) — detectors as the third subsystem: a unit is one
// model on one camera, placed on a GPU-labelled worker by stream headroom, its events in buckets under
// det/<unit>/e<epoch>/ on the resource, under the epoch the worker holds.

import (
	"os"
	"path/filepath"
	"testing"

	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/w2cplatform"
)

func detWorker(t *testing.T, box *testbox.Box, name string, o vms.DetOptions) *vms.DetWorker {
	t.Helper()
	o.Worker.Clock, o.Worker.Wall = box.Clock.Now, box.Wall.Now
	if o.ArchiveRoot == "" {
		o.ArchiveRoot = box.Archive
	}
	if o.Server == "" {
		o.Server = "srv-1"
	}
	d, err := vms.NewDetWorker(name, box.Vars.AsWriter("detworker", "det/epoch/*", "det/slots/*"), box.Objects, o)
	if err != nil {
		t.Fatal(err)
	}
	return d
}

// A VMS worker holding the camera and publishing its fan-out — found in the heartbeat, never by calling it.
func holdingCamera(box *testbox.Box, cam string) {
	box.Objects.Put("vms/heartbeats/w-1", p.Heartbeat{Worker: "w-1", Ts: box.Wall.Now(),
		Status: []map[string]any{{"id": cam, "phase": "running", "live_url": "rtsp://srv-1:8554/" + cam}},
		Extra:  map[string]any{"server": "srv-1", "capacity": 50, "headroom": 49}}.ToBytes())
}

func detUnit(t *testing.T, box *testbox.Box, name, cam, kind string, extra p.Row) string {
	t.Helper()
	con := p.NewSpecController(vms.DetSpec, box.Vars.AsWriter("console", vms.DetSpec.ACLConsole()...), box.Objects, 8, box.Wall.Now, "cluster-a")
	row := p.Row{"name": name, "cam": cam, "kind": kind}
	for k, v := range extra {
		row[k] = v
	}
	if _, err := con.Create(row); err != nil {
		t.Fatal(err)
	}
	box.Vars.Put(vms.DetSpec.Sub().Assignment("d-1"), p.Items{"units": name, "rev": "1"}, p.Absent)
	return name
}

func bucketLines(t *testing.T, root, sub, unit string, epoch int) []p.Event {
	t.Helper()
	dir := filepath.Join(root, sub, unit, "e"+itoa(epoch))
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var out []p.Event
	for _, e := range entries {
		out = append(out, p.ReadBucket(filepath.Join(dir, e.Name()))...)
	}
	return out
}

func itoa(n int) string { return p.Str(n) }

func TestADetectorWritesWhatItSawUnderItsOwnEpoch(t *testing.T) {
	box := testbox.NewBox()
	holdingCamera(box, "7")
	detUnit(t, box, "7-linecross", "7", "linecross", nil)
	d := detWorker(t, box, "d-1", vms.DetOptions{})

	for i := 0; i < 3; i++ { // FakeModel fires on every third pass
		d.ReconcileOnce()
	}
	lines := bucketLines(t, box.Archive, "det", "7-linecross", d.Epochs["7-linecross"])
	if len(lines) == 0 {
		t.Fatal("the model fired and nothing was written")
	}
	for _, l := range lines {
		if p.Str(l["kind"]) != "linecross" || p.ToFloat(l["cam"]) != 7 {
			t.Fatalf("%v", l)
		}
	}
	if st := d.Status("7-linecross"); p.Str(st["phase"]) != "running" || p.Str(st["source"]) == "" {
		t.Fatalf("%v", st)
	}
}

func TestACameraNobodyHoldsLeavesTheModelWaitingNotFailed(t *testing.T) {
	box := testbox.NewBox()
	detUnit(t, box, "7-lpr", "7", "lpr", nil) // no holder heartbeat at all
	d := detWorker(t, box, "d-1", vms.DetOptions{})
	d.ReconcileOnce()

	st := d.Status("7-lpr")
	if p.Str(st["phase"]) != "waiting" || p.Str(st["why"]) == "" {
		t.Fatalf("a camera held by nobody is not a failure, and the reason has to say which: %v", st)
	}
	if _, took := d.Epochs["7-lpr"]; took {
		t.Fatal("an epoch was taken for a unit that cannot run")
	}
}

func TestAModelThisBuildDoesNotHaveIsSaidOutLoud(t *testing.T) {
	box := testbox.NewBox()
	holdingCamera(box, "7")
	detUnit(t, box, "7-anpr", "7", "anpr", nil) // no such kind in the registry
	d := detWorker(t, box, "d-1", vms.DetOptions{})
	d.ReconcileOnce()
	if st := d.Status("7-anpr"); p.Str(st["phase"]) != "unsupported" {
		t.Fatalf("a half-upgraded cluster must say so rather than look idle: %v", st)
	}
}

func TestADisabledDetectorStopsAndKeepsItsLine(t *testing.T) {
	box := testbox.NewBox()
	holdingCamera(box, "7")
	detUnit(t, box, "7-motion", "7", "motion", nil)
	d := detWorker(t, box, "d-1", vms.DetOptions{})
	d.ReconcileOnce()
	if p.Str(d.Status("7-motion")["phase"]) != "running" {
		t.Fatal("it did not start")
	}

	con := p.NewSpecController(vms.DetSpec, box.Vars.AsWriter("console", vms.DetSpec.ACLConsole()...), box.Objects, 8, box.Wall.Now, "cluster-a")
	con.Update("7-motion", p.Row{"enabled": false})
	d.ReconcileOnce()
	st := d.Status("7-motion")
	if p.Str(st["phase"]) != "pending" {
		t.Fatalf("a disabled detector stops; it does not vanish: %v", st)
	}
}

func TestTheHeartbeatSaysWhatTheSchedulerReads(t *testing.T) {
	box := testbox.NewBox()
	holdingCamera(box, "7")
	detUnit(t, box, "7-lpr", "7", "lpr", nil)
	d := detWorker(t, box, "d-1", vms.DetOptions{Capacity: 8})
	d.ReconcileOnce()
	if err := d.HeartbeatOnce(); err != nil {
		t.Fatal(err)
	}
	raw, _ := box.Objects.Get(vms.DetSpec.Sub().HeartbeatKey("d-1"))
	hb, _ := p.HeartbeatFromBytes(raw)
	if p.ToFloat(hb.Extra["capacity"]) != 8 || p.ToFloat(hb.Extra["headroom"]) != 7 {
		t.Fatalf("capacity and headroom are what the autoscaler reads: %v", hb.Extra)
	}
	if hb.ExtraString("labels", "") != "gpu" {
		t.Fatalf("a detector says where it may run: %v", hb.Extra)
	}
}

func TestTheDetectorFollowsTheRecorderHoldingItsCamera(t *testing.T) {
	// `near: {sub: rec, by: cam}` — the unit is `7-linecross` and no recorder ever reports that id, so the
	// affinity has to follow the camera named in the row. Ported with the spec; see the parity tests.
	if vms.DetSpec.Near != "rec" || vms.DetSpec.NearBy != "cam" {
		t.Fatal(vms.DetSpec.Near, vms.DetSpec.NearBy)
	}
	if vms.DetSpec.Home != "" {
		t.Fatal("a detector has no home: nobody owns the answer to which server it belongs on")
	}
}
