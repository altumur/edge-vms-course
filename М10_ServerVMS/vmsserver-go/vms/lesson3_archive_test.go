package vms_test

// Lesson 3 — the archive as a resource: promote, manifest, repair, retention.

import (
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"testing"
	"time"

	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/w2cplatform"
)

func utc(s string) time.Time {
	t, err := time.Parse("2006-01-02T15:04:05", s)
	if err != nil {
		panic(err)
	}
	return t.UTC()
}

func ts(s string) float64 { return float64(utc(s).Unix()) }

func writeSegment(t *testing.T, root string, cam, epoch int, start string, size int, mtime float64) string {
	pth := vms.SegmentPath(root, strconv.Itoa(cam), epoch, utc(start))
	os.MkdirAll(filepath.Dir(pth), 0o755)
	if err := os.WriteFile(pth, make([]byte, size), 0o644); err != nil {
		t.Fatal(err)
	}
	if mtime != 0 {
		touch(pth, mtime)
	}
	return pth
}

func touch(pth string, mtime float64) {
	tm := time.Unix(0, int64(mtime*1e9))
	os.Chtimes(pth, tm, tm)
}

func TestParseAndPaths(t *testing.T) {
	// The middle segment is the UNIT, and it comes back as a string: the path grammar does not know that
	// `id: cam` makes today's unit a camera number, and a recording named "7-backup" parses the same way.
	unit, epoch, start, ok := vms.Parse("/a/rec/7/e5/20260912T101000Z.mp4", "/a")
	if !ok || unit != "7" || epoch != 5 || !start.Equal(utc("2026-09-12T10:10:00")) {
		t.Fatal(unit, epoch, start, ok)
	}
	if u, _, _, ok := vms.Parse("/a/rec/7-backup/e5/20260912T101000Z.mp4", "/a"); !ok || u != "7-backup" {
		t.Fatal(u, ok)
	}
	if _, _, _, ok := vms.Parse("/a/rec/7/e5/manifest.jsonl", "/a"); ok {
		t.Fatal("manifest is not a segment")
	}
	if _, _, _, ok := vms.Parse("/a/vms/7/e5/20260912T101000Z.mp4", "/a"); ok {
		t.Fatal("not under rec/ — vms/ is the worker's events tree")
	}
}

func TestPromoteIsTheAcknowledgementOrder(t *testing.T) {
	box := testbox.NewBox()
	res := vms.NewArchiveResource(box.Spool, box.Archive, 600, nil)
	pth := writeSegment(t, box.Spool, 7, 3, "2026-09-12T10:00:00", 1000, ts("2026-09-12T10:10:00"))
	seg, err := res.Promote(pth, 0, "live")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(pth); err == nil { // 3. gone from the spool, last
		t.Fatal("still in the spool")
	}
	if _, err := os.Stat(filepath.Join(box.Archive, seg.Path)); err != nil { // 1. in the archive, whole
		t.Fatal(err)
	}
	lines := vms.NewManifest(box.Archive, "7").Read() // 2. named in the manifest
	if len(lines) != 1 || lines[0].Epoch != 3 || lines[0].End-lines[0].Start != 600 || lines[0].Bytes != 1000 {
		t.Fatal(lines)
	}
}

func TestKillMidSegmentOpenLostClosedKept(t *testing.T) {
	// Seven minutes of a ten-minute segment length: six closed segments
	// promoted (or still in the spool, closed), one open segment lost.
	box := testbox.NewBox()
	res := vms.NewArchiveResource(box.Spool, box.Archive, 600, nil)
	now := ts("2026-09-12T12:00:00")
	for m := 0; m < 60; m += 10 { // 6 closed, promoted in time
		pth := writeSegment(t, box.Spool, 7, 3, utc("2026-09-12T11:00:00").Add(time.Duration(m)*time.Minute).Format("2006-01-02T15:04:05"), 1000, now-3600+float64(m+10)*60)
		res.Promote(pth, 0, "live")
	}
	late := writeSegment(t, box.Spool, 7, 3, "2026-09-12T12:00:00", 1000, now-60)   // closed, worker died before promote
	writeSegment(t, box.Spool, 7, 3, "2026-09-12T12:10:00", 10, now-5)              // the open one
	if got := res.ClosedInSpool(30, now); !reflect.DeepEqual(got, []string{late}) { // what the restart promotes
		t.Fatal(got)
	}
	res.Promote(late, 0, "live")
	if len(vms.NewManifest(box.Archive, "7").Read()) != 7 {
		t.Fatal("seven")
	}
	if len(res.ClosedInSpool(30, now)) != 0 { // the open segment is what a kill loses
		t.Fatal("open")
	}
}

func TestManifestRebuiltFromTheFilesAlone(t *testing.T) {
	box := testbox.NewBox()
	res := vms.NewArchiveResource(box.Spool, box.Archive, 600, nil)
	for _, m := range []string{"10:00:00", "10:10:00", "10:20:00"} {
		res.Promote(writeSegment(t, box.Spool, 7, 3, "2026-09-12T"+m, 1000, 0), 0, "live")
	}
	orig := vms.NewManifest(box.Archive, "7").Read()
	os.Remove(vms.NewManifest(box.Archive, "7").Path) // the index did not travel
	if rep := res.Repair(); rep != (vms.RepairReport{3, 0}) {
		t.Fatal(rep)
	}
	rebuilt := vms.NewManifest(box.Archive, "7").Read()
	for i := range orig {
		if rebuilt[i].Path != orig[i].Path || rebuilt[i].Epoch != orig[i].Epoch || rebuilt[i].Start != orig[i].Start {
			t.Fatal(rebuilt, orig)
		}
	}
	os.Remove(filepath.Join(box.Archive, orig[0].Path)) // a file went missing under a line
	if rep := res.Repair(); rep != (vms.RepairReport{0, 1}) || len(vms.NewManifest(box.Archive, "7").Read()) != 2 {
		t.Fatal(rep)
	}
	if rep := res.Repair(); rep != (vms.RepairReport{0, 0}) { // idempotent
		t.Fatal(rep)
	}
}

func TestTimelineMarksAFencedEpochAndSpansTwoResources(t *testing.T) {
	box := testbox.NewBox()
	res := vms.NewArchiveResource(box.Spool, box.Archive, 600, nil)
	res.Promote(writeSegment(t, box.Spool, 7, 3, "2026-09-12T10:00:00", 1000, ts("2026-09-12T10:10:00")), 0, "live")
	res.Promote(writeSegment(t, box.Spool, 7, 4, "2026-09-12T10:10:00", 1000, ts("2026-09-12T10:20:00")), 0, "live")
	res.Promote(writeSegment(t, box.Spool, 7, 3, "2026-09-12T10:10:00", 1000, ts("2026-09-12T10:15:00")), 0, "live") // the zombie's
	tl := vms.NewManifest(box.Archive, "7").Timeline(ts("2026-09-12T10:05:00"), ts("2026-09-12T10:30:00"), 4)
	var got [][2]any
	for _, s := range tl {
		got = append(got, [2]any{s.Epoch, s.Fenced})
	}
	if !reflect.DeepEqual(got, [][2]any{{3, true}, {3, true}, {4, false}}) {
		t.Fatal(got)
	}
	// a second resource (another server) holds later footage: the console merges two manifests
	other := vms.NewArchiveResource(box.Spool+"2", box.Archive+"2", 600, nil)
	other.Promote(writeSegment(t, other.Spool, 7, 5, "2026-09-12T10:20:00", 1000, ts("2026-09-12T10:30:00")), 0, "live")
	merged := append(vms.NewManifest(box.Archive, "7").Timeline(0, 1e12, 0), vms.NewManifest(other.Root, "7").Timeline(0, 1e12, 0)...)
	var epochs []int
	for _, s := range merged {
		epochs = append(epochs, s.Epoch)
	}
	if !reflect.DeepEqual(epochs, []int{3, 3, 4, 5}) {
		t.Fatal(epochs)
	}
}

func TestRetentionIsAPolicyOnTheResource(t *testing.T) {
	box := testbox.NewBox()
	res := vms.NewArchiveResource(box.Spool, box.Archive, 600, nil)
	now := ts("2026-10-20T00:00:00")
	for _, day := range []string{"01", "10", "19"} {
		res.Promote(writeSegment(t, box.Spool, 7, 3, "2026-10-"+day+"T10:00:00", 1000, ts("2026-10-"+day+"T10:10:00")), 0, "live")
	}
	if res.Retain("7", 8, now) != 2 { // cutoff 12 Oct: the 1st and the 10th go
		t.Fatal("retain")
	}
	left := vms.NewManifest(box.Archive, "7").Read()
	if _, err := os.Stat(filepath.Join(box.Archive, "rec", "7", "e3", "20261001T100000Z.mp4")); len(left) != 1 || err == nil {
		t.Fatal(left)
	}
	if res.Usage() != 1000 {
		t.Fatal(res.Usage())
	}
}

func TestEventsAreBucketsOnTheResourceRecordingOrNot(t *testing.T) {
	// An event is an observation, written by the WORKER holding the camera's epoch, into the camera's
	// bucket on the worker's server's resource — vms/<cam>/e<epoch>/ — recorded or not. Footage is the
	// RECORDER's, under its own epoch in rec/<cam>/e<epoch>/, indexed by the manifest beside it. Two trees,
	// two writers, one camera: a camera that is watched and never recorded has buckets and no rec/ tree;
	// the manifest indexes media only, the resource's event database indexes the buckets; each is retained
	// by its own policy. No controller wrote any of it.
	box := testbox.NewBox()
	res := vms.NewArchiveResource(box.Spool, box.Archive, 600, box.Wall.Now)
	t0 := ts("2026-09-12T10:00:00")
	box.Wall.Set(t0 + 2000)
	log := vms.EventLogFor(box.Archive, 7, 3, 600)                          // the worker holds epoch 3 for camera 7
	pth, _ := log.Append(t0+12.5, "motion", map[string]any{"zone": "gate"}) // not recorded: still an event
	if sub, unit, epoch, start, ok := p.ParseBucket(pth, box.Archive); !ok || sub != "vms" || unit != "7" || epoch != 3 || start != t0 || p.ReadBucket(pth)[0]["zone"] != "gate" {
		t.Fatal(pth)
	}
	log.Append(t0+40.0, "silent", nil)                                                                               // the event with no segment, by definition
	p2, _ := log.Append(t0+700.0, "person", map[string]any{"score": 0.9})                                            // the next bucket: rolled by the clock
	if under := p.SubsystemsUnder(box.Archive); len(under) != 1 || len(under["vms"]) != 1 || len(res.Units()) != 0 { // watched, not recorded: buckets, no rec/ tree
		t.Fatal(under)
	}
	if tl := vms.NewManifest(box.Archive, "7").Timeline(t0, t0+1200, 0); len(tl) != 0 { // the manifest indexes media, and there is none
		t.Fatal(tl)
	}
	db := p.NewEventDatabase(box.Archive, "box", box.Wall.Now, 600)
	if rep := db.Rebuild(); rep.Added != 3 {
		t.Fatal(rep)
	}
	kinds := []string{}
	for _, e := range db.Query(p.Query{T0: t0, T1: t0 + 1200, Cam: p.IntPtr(7)}).Events {
		kinds = append(kinds, e.Kind)
	}
	eqs(t, kinds, []string{"motion", "silent", "person"})
	// now a recorder records the camera under ITS epoch, into rec/: the timeline has a span, the events are still the worker's
	seg, _ := res.Promote(writeSegment(t, box.Spool, 7, 4, "2026-09-12T10:10:00", 1000, t0+1200), 0, "live")
	if seg.Path != "rec/7/e4/20260912T101000Z.mp4" || len(res.Units()) != 1 {
		t.Fatal(seg)
	}
	tl := vms.NewManifest(box.Archive, "7").Timeline(t0, t0+1200, 4)
	if len(tl) != 1 || tl[0].Media == "" || tl[0].Epoch != 4 || tl[0].Fenced {
		t.Fatal(tl)
	}
	if under := p.SubsystemsUnder(box.Archive); len(under) != 2 || under["rec"][0] != "7" || under["vms"][0] != "7" { // two trees, two writers, one camera
		t.Fatal(under)
	}
	// repair rebuilds the manifest from the files; media retention is the recorder's, bucket retention the platform's
	os.Remove(vms.NewManifest(box.Archive, "7").Path)
	if rep := res.Repair(); rep != (vms.RepairReport{1, 0}) {
		t.Fatal(rep)
	}
	if res.Retain("7", 1, t0+3*86400) != 1 || len(vms.NewManifest(box.Archive, "7").Read()) != 0 {
		t.Fatal("retain")
	}
	for _, f := range []string{pth, p2} { // the recorder's retention never touches the worker's buckets
		if _, err := os.Stat(f); err != nil {
			t.Fatal(f)
		}
	}
	box.Vars.Put("vms/retention/7", p.Items{"days": "30"}, p.NoCAS) // what the VMS controller writes for a camera's events
	platform := p.NewResource(box.Archive, "box", "http://box", box.Vars, box.Objects, 600, func() float64 { return t0 + 40*86400 }, nil)
	platform.Database = db
	if platform.Retain() != 2 { // files, by the platform — and the database forgets
		t.Fatal("platform retain")
	}
	if _, err := os.Stat(pth); err == nil {
		t.Fatal("bucket still there")
	}
	if len(db.Query(p.Query{T0: t0, T1: t0 + 1200, Cam: p.IntPtr(7)}).Events) != 0 {
		t.Fatal("the database remembers a removed bucket")
	}
}

func eqs(t *testing.T, got, want []string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatal(got, want)
	}
	for i := range got {
		if got[i] != want[i] {
			t.Fatal(got, want)
		}
	}
}
