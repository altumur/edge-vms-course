package vms_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/w2cplatform"
)

// The claim this file is here to check: two recordings of one camera, on two servers, cost a YAML edit
// and nothing else. Three lines change — `id`, the `cam` field, `spread_by` — and no Go at all.
const twoCopiesYAML = `
name: rec
unit:
  rows: recordings
  id: name                                           # was: cam — the unit is now named, not numbered
  fields:
    name:           {type: string, required: true}   # "7-main", "7-backup"
    cam:            {type: string, required: true}   # whose fan-out this recording subscribes to
    retention_days: {type: int,    default: 30}
    enabled:        {type: bool,   default: true}
    labels:         {type: list}
placement:
  capacity:   {from: capacity, fallback: 50}
  headroom:   {from: headroom}
  constraint: labels-subset
  requires:   none
  servers:    shared
  tie_break:  most-free-capacity
  near:       vms
  spread_by:  cam                                    # new: two copies of one camera go on different servers
  rebalance:  {dead_band: 0.10}
snapshot: [name, cam, retention_days, enabled, labels]
console:
  running: recordings_running
`

// Not a rehearsal for a change: the change itself, run against the real types.
//
// The spec below is `rec.subsystem.yaml` with three lines different. Everything it drives —
// SpecController, the archive tree, the console's timeline — is the shipped code, used unchanged.
// If any of it still assumed "a recording is named by its camera", this test would not pass, and
// until Camera.ID became a string it could not have.
func TestTwoRecordingsOfOneCameraAreAYamlEdit(t *testing.T) {
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars, box.Objects, 0, box.Wall.Now)

	d, err := p.ParseYAML(twoCopiesYAML)
	if err != nil {
		t.Fatal(err)
	}
	spec, err := p.SpecFromMap(d.(map[string]any))
	if err != nil {
		t.Fatal(err)
	}
	if spec.ID != "name" || spec.SpreadBy != "cam" {
		t.Fatal(spec.ID, spec.SpreadBy)
	}

	rec := p.NewSpecController(spec, box.Vars, box.Objects, 0, box.Wall.Now, "")
	for w, server := range map[string]string{"r-1": "srv-1", "r-2": "srv-2"} {
		hb := p.Heartbeat{Worker: w, Ts: box.Wall.Now(), Extra: map[string]any{"server": server, "capacity": 50, "headroom": 50}}
		box.Objects.Put(spec.Sub().HeartbeatKey(w), hb.ToBytes())
	}

	mustCreate(t, ctl, map[string]any{"name": "gate", "source": "driverpack://file/gate.mp4"}) // camera 1
	if _, err := rec.Create(map[string]any{"name": "1-main", "cam": "1"}); err != nil {
		t.Fatal(err)
	}
	if _, err := rec.Create(map[string]any{"name": "1-backup", "cam": "1"}); err != nil {
		t.Fatal(err)
	}
	if _, err := rec.EnsurePlaced(nil); err != nil {
		t.Fatal(err)
	}

	main, backup := rec.Placement("1-main"), rec.Placement("1-backup")
	if main == nil || backup == nil || rec.ServerOf(main.Worker) == rec.ServerOf(backup.Worker) { // the point of the exercise
		t.Fatal(main, backup)
	}

	// each copy writes its own tree, under its own name, with its own retention
	now := box.Wall.Now()
	for _, unit := range []string{"1-main", "1-backup"} {
		pth := vms.SegmentPath(box.Archive, unit, 1, time.Unix(int64(now-600), 0).UTC())
		os.MkdirAll(filepath.Dir(pth), 0o755)
		os.WriteFile(pth, []byte("x"), 0o644)
		rel, _ := filepath.Rel(box.Archive, pth)
		if err := vms.NewManifest(box.Archive, unit).Append(vms.Segment{Unit: unit, Epoch: 1, Start: now - 600, End: now, Path: rel, Bytes: 1}); err != nil {
			t.Fatal(err)
		}
	}

	arch := vms.NewArchiveResource(box.Spool, box.Archive, 0, box.Wall.Now)
	eq(t, arch.Units(), []string{"1-backup", "1-main"}) // two directories, not one
	if len(arch.Coverage("1-main", 0)) != 1 || len(arch.Coverage("1-backup", 0)) != 1 {
		t.Fatal(arch.Coverage("1-main", 0), arch.Coverage("1-backup", 0))
	}

	// and the camera's timeline is both of them: the console resolves camera -> recordings
	eq(t, vms.RecordingsOf(rec, "1"), []string{"1-backup", "1-main"})
	spans := []vms.Span{}
	for _, unit := range vms.RecordingsOf(rec, "1") {
		spans = append(spans, vms.NewManifest(box.Archive, unit).Timeline(0, 1e12, 0)...)
	}
	if len(spans) != 2 {
		t.Fatal(spans)
	}

	// retention is per recording, because the row is per recording
	if _, err := rec.Update("1-backup", map[string]any{"retention_days": 1}); err != nil {
		t.Fatal(err)
	}
	if p.ToFloat(rec.Unit("1-main")["retention_days"]) != 30 || p.ToFloat(rec.Unit("1-backup")["retention_days"]) != 1 {
		t.Fatal(rec.Unit("1-main"), rec.Unit("1-backup"))
	}
}

// The placement gap the archive's unit-keyed tree opens up. Two units that name the same
// camera exist to survive ONE server dying, so a second copy beside the first is not a
// compromise — it is the failure the operator was insuring against. `spread_by` is therefore
// a FILTER: unplaceable is the honest answer, co-located is not.
//
// It also has to beat `near`, which pulls a recorder towards the camera's holder and would
// otherwise pull both copies to the same place.
func TestSpreadByKeepsTwoCopiesOffOneServer(t *testing.T) {
	box := testbox.NewBox()
	spec, err := p.SpecFromMap(map[string]any{
		"name": "copy",
		"unit": map[string]any{"rows": "copies", "id": "name",
			"fields": map[string]any{
				"name": map[string]any{"type": "string", "required": true},
				"cam":  map[string]any{"type": "string", "required": true},
			}},
		"placement": map[string]any{"capacity": map[string]any{"from": "capacity", "fallback": 50}, "spread_by": "cam"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if spec.SpreadBy != "cam" {
		t.Fatal(spec.SpreadBy)
	}

	admin := p.NewSpecController(spec, box.Vars, box.Objects, 0, box.Wall.Now, "")
	for w, server := range map[string]string{"w-1": "srv-1", "w-2": "srv-2"} {
		hb := p.Heartbeat{Worker: w, Ts: box.Wall.Now(), Extra: map[string]any{"server": server, "capacity": 50, "headroom": 50}}
		box.Objects.Put(spec.Sub().HeartbeatKey(w), hb.ToBytes())
	}
	for _, f := range []map[string]any{{"name": "7-main", "cam": "7"}, {"name": "7-backup", "cam": "7"}, {"name": "8-main", "cam": "8"}} {
		if _, err := admin.Create(f); err != nil {
			t.Fatal(err)
		}
	}
	admin.EnsurePlaced(nil)

	main, backup := admin.Placement("7-main"), admin.Placement("7-backup")
	if main == nil || backup == nil || main.Worker == backup.Worker { // the whole point: different servers
		t.Fatal(main, backup)
	}
	if admin.ServerOf(main.Worker) == admin.ServerOf(backup.Worker) {
		t.Fatal(admin.ServerOf(main.Worker))
	}
	if admin.Placement("8-main") == nil { // another camera is unaffected
		t.Fatal("8-main unplaced")
	}

	// a third copy of camera 7 has nowhere to go, and says so instead of doubling up
	if _, err := admin.Create(map[string]any{"name": "7-third", "cam": "7"}); err != nil {
		t.Fatal(err)
	}
	admin.EnsurePlaced(nil)
	unplaceable := []string{}
	for _, u := range admin.Unplaceable() {
		unplaceable = append(unplaceable, p.Str(u.ID))
	}
	if admin.Placement("7-third") != nil || !contains(unplaceable, "7-third") {
		t.Fatal(admin.Placement("7-third"), unplaceable)
	}
}

// Where the text actually comes from: a subsystem whose id is a FIELD (`rec`, `id: cam`)
// takes the unit's id verbatim from the operator's body — only the numeric next-id path
// protects a numbered one. From there the same string becomes the key `rec/recordings/<id>`,
// the prefix the console's token is matched against (`rec/recordings/*` — which
// `rec/recordings/../../…` passes), and a directory on the resource (UnitDir). Refused at
// the door, as a 400.
func TestAUnitsIDIsANameAndNotAPath(t *testing.T) {
	box := testbox.NewBox()
	rec := p.NewSpecController(vms.RecSpec, box.Vars, box.Objects, 0, box.Wall.Now, "")

	for _, bad := range []string{"../../cameras/7", "a/b", ".."} {
		_, err := rec.Create(map[string]any{"cam": bad})
		if err == nil || !strings.Contains(err.Error(), "name, not a path") {
			t.Fatalf("a unit id was accepted as a path: %q (%v)", bad, err)
		}
	}

	r, err := rec.Create(map[string]any{"cam": "7"}) // the ordinary case is untouched
	if err != nil || r.ID() != "7" {
		t.Fatal(r, err)
	}
	if it, _, _ := box.Vars.Get("rec/recordings/7"); it["cam"] != "7" {
		t.Fatal(it)
	}

	// and one layer down the store refuses the same shapes on its own, whoever calls it
	for _, bad := range []string{"rec/recordings/../../cameras/7", "/rec/recordings/7"} {
		if _, err := box.Vars.Put(bad, p.Items{"cam": "7"}, p.Absent); err == nil {
			t.Fatalf("the store accepted %q", bad)
		}
	}
}

func contains(xs []string, x string) bool {
	for _, s := range xs {
		if s == x {
			return true
		}
	}
	return false
}
