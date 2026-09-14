package vms_test

// Lesson 3 — the archive as a resource: promote, manifest, repair, retention.

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	p "vmsserver/psimplatform"
	"vmsserver/testbox"
	"vmsserver/vms"
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
	pth := vms.SegmentPath(root, cam, epoch, utc(start))
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
	cam, epoch, start, ok := vms.Parse("/a/vms/7/e5/20260912T101000Z.mp4", "/a")
	if !ok || cam != 7 || epoch != 5 || !start.Equal(utc("2026-09-12T10:10:00")) {
		t.Fatal(cam, epoch, start, ok)
	}
	if _, _, _, ok := vms.Parse("/a/vms/7/e5/manifest.jsonl", "/a"); ok {
		t.Fatal("manifest is not a segment")
	}
	if _, _, _, ok := vms.Parse("/a/7/e5/20260912T101000Z.mp4", "/a"); ok {
		t.Fatal("not under vms/")
	}
}

func TestPromoteIsTheAcknowledgementOrder(t *testing.T) {
	box := testbox.NewBox()
	res := vms.NewArchiveResource(box.Spool, box.Archive, 600, nil)
	pth := writeSegment(t, box.Spool, 7, 3, "2026-09-12T10:00:00", 1000, ts("2026-09-12T10:10:00"))
	seg, err := res.Promote(pth, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(pth); err == nil { // 3. gone from the spool, last
		t.Fatal("still in the spool")
	}
	if _, err := os.Stat(filepath.Join(box.Archive, seg.Path)); err != nil { // 1. in the archive, whole
		t.Fatal(err)
	}
	lines := vms.NewManifest(box.Archive, 7).Read() // 2. named in the manifest
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
		res.Promote(pth, 0)
	}
	late := writeSegment(t, box.Spool, 7, 3, "2026-09-12T12:00:00", 1000, now-60)   // closed, worker died before promote
	writeSegment(t, box.Spool, 7, 3, "2026-09-12T12:10:00", 10, now-5)              // the open one
	if got := res.ClosedInSpool(30, now); !reflect.DeepEqual(got, []string{late}) { // what the restart promotes
		t.Fatal(got)
	}
	res.Promote(late, 0)
	if len(vms.NewManifest(box.Archive, 7).Read()) != 7 {
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
		res.Promote(writeSegment(t, box.Spool, 7, 3, "2026-09-12T"+m, 1000, 0), 0)
	}
	orig := vms.NewManifest(box.Archive, 7).Read()
	os.Remove(vms.NewManifest(box.Archive, 7).Path) // the index did not travel
	if rep := res.Repair(); rep != (vms.RepairReport{3, 0}) {
		t.Fatal(rep)
	}
	rebuilt := vms.NewManifest(box.Archive, 7).Read()
	for i := range orig {
		if rebuilt[i].Path != orig[i].Path || rebuilt[i].Epoch != orig[i].Epoch || rebuilt[i].Start != orig[i].Start {
			t.Fatal(rebuilt, orig)
		}
	}
	os.Remove(filepath.Join(box.Archive, orig[0].Path)) // a file went missing under a line
	if rep := res.Repair(); rep != (vms.RepairReport{0, 1}) || len(vms.NewManifest(box.Archive, 7).Read()) != 2 {
		t.Fatal(rep)
	}
	if rep := res.Repair(); rep != (vms.RepairReport{0, 0}) { // idempotent
		t.Fatal(rep)
	}
}

func TestTimelineMarksAFencedEpochAndSpansTwoResources(t *testing.T) {
	box := testbox.NewBox()
	res := vms.NewArchiveResource(box.Spool, box.Archive, 600, nil)
	res.Promote(writeSegment(t, box.Spool, 7, 3, "2026-09-12T10:00:00", 1000, ts("2026-09-12T10:10:00")), 0)
	res.Promote(writeSegment(t, box.Spool, 7, 4, "2026-09-12T10:10:00", 1000, ts("2026-09-12T10:20:00")), 0)
	res.Promote(writeSegment(t, box.Spool, 7, 3, "2026-09-12T10:10:00", 1000, ts("2026-09-12T10:15:00")), 0) // the zombie's
	tl := vms.NewManifest(box.Archive, 7).Timeline(ts("2026-09-12T10:05:00"), ts("2026-09-12T10:30:00"), 4)
	var got [][2]any
	for _, s := range tl {
		got = append(got, [2]any{s.Epoch, s.Fenced})
	}
	if !reflect.DeepEqual(got, [][2]any{{3, true}, {3, true}, {4, false}}) {
		t.Fatal(got)
	}
	// a second resource (another server) holds later footage: the console merges two manifests
	other := vms.NewArchiveResource(box.Spool+"2", box.Archive+"2", 600, nil)
	other.Promote(writeSegment(t, other.Spool, 7, 5, "2026-09-12T10:20:00", 1000, ts("2026-09-12T10:30:00")), 0)
	merged := append(vms.NewManifest(box.Archive, 7).Timeline(0, 1e12, 0), vms.NewManifest(other.Root, 7).Timeline(0, 1e12, 0)...)
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
		res.Promote(writeSegment(t, box.Spool, 7, 3, "2026-10-"+day+"T10:00:00", 1000, ts("2026-10-"+day+"T10:10:00")), 0)
	}
	if res.Retain(7, 8, now) != 2 { // cutoff 12 Oct: the 1st and the 10th go
		t.Fatal("retain")
	}
	left := vms.NewManifest(box.Archive, 7).Read()
	if _, err := os.Stat(filepath.Join(box.Archive, "vms", "7", "e3", "20261001T100000Z.mp4")); len(left) != 1 || err == nil {
		t.Fatal(left)
	}
	if res.Usage() != 1000 {
		t.Fatal(res.Usage())
	}
}

func TestEventsAreBucketsOnTheResourceRecordingOrNot(t *testing.T) {
	// The archive's unit is a time span under an epoch, not a media file.
	box := testbox.NewBox()
	res := vms.NewArchiveResource(box.Spool, box.Archive, 600, box.Wall.Now)
	t0 := ts("2026-09-12T10:00:00")
	box.Wall.Set(t0 + 2000)                                                 // the resource's clock: every bucket below is over
	log := vms.EventLogFor(box.Archive, 7, 3, 600)                          // the worker holds epoch 3 for camera 7
	pth, _ := log.Append(t0+12.5, "motion", map[string]any{"zone": "gate"}) // not recording: still an event
	if sub, unit, epoch, start, ok := p.ParseBucket(pth, box.Archive); !ok || sub != "vms" || unit != "7" || epoch != 3 || start != t0 || p.ReadBucket(pth)[0]["zone"] != "gate" {
		t.Fatal(pth)
	}
	log.Append(t0+40.0, "silent", nil)                                    // the event with no segment, by definition
	p2, _ := log.Append(t0+700.0, "person", map[string]any{"score": 0.9}) // the next bucket: rolled by the clock
	for _, f := range []string{pth, p2} {
		touch(f, t0+1250) // quiet for the grace (the test's clock is not the disk's)
	}
	if len(vms.NewManifest(box.Archive, 7).Buckets()) != 0 { // durable already; not yet indexed
		t.Fatal("indexed early")
	}
	closed := res.CloseBuckets(t0+1300, 30)
	if len(closed) != 2 || closed[0].Events != 2 || closed[1].Events != 1 { // both spans over and quiet: indexed
		t.Fatal(closed)
	}
	if len(res.CloseBuckets(t0+1300, 30)) != 0 { // idempotent
		t.Fatal("twice")
	}
	tl := vms.NewManifest(box.Archive, 7).Timeline(t0, t0+1200, 4)
	if len(tl) != 2 || tl[0].Media != "" || tl[0].Events != 2 || !tl[0].Fenced || tl[1].Events != 1 || !tl[1].Fenced { // watched, not recorded; fenced
		t.Fatal(tl)
	}
	// now the camera IS recorded for the second span: the events count onto the media
	res.Promote(writeSegment(t, box.Spool, 7, 4, "2026-09-12T10:10:00", 1000, t0+1200), 0)
	p4, _ := vms.EventLogFor(box.Archive, 7, 4, 600).Append(t0+650.0, "motion", nil)
	touch(p4, t0+1900)
	res.CloseBuckets(t0+2000, 30)
	tl = vms.NewManifest(box.Archive, 7).Timeline(t0+600, t0+1200, 4)
	if len(tl) != 2 || tl[0].Media != "" || tl[0].Epoch != 3 || tl[0].Events != 1 || !tl[0].Fenced ||
		tl[1].Media == "" || tl[1].Epoch != 4 || tl[1].Events != 1 || tl[1].Fenced {
		t.Fatal(tl)
	}
	// repair rebuilds both kinds from the files; media retention is the VMS's, bucket retention the platform's
	os.Remove(vms.NewManifest(box.Archive, 7).Path)
	if rep := res.Repair(); rep != (vms.RepairReport{4, 0}) {
		t.Fatal(rep)
	}
	if res.Retain(7, 1, t0+3*86400) != 1 || len(vms.NewManifest(box.Archive, 7).Buckets()) != 3 {
		t.Fatal("retain")
	}
	box.Vars.Put("vms/retention/7", p.Items{"days": "30"}, p.NoCAS) // what the controller writes for a camera's events
	platform := p.NewResource(box.Archive, "box", "http://box", box.Vars, box.Objects, 600, func() float64 { return t0 + 40*86400 }, nil)
	if platform.Retain() != 3 { // files, by the platform...
		t.Fatal("platform retain")
	}
	if _, err := os.Stat(pth); err == nil {
		t.Fatal("bucket still there")
	}
	if rep := res.Repair(); rep != (vms.RepairReport{0, 3}) { // ...lines, by the VMS's own pass
		t.Fatal(rep)
	}
}
