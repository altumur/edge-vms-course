package vms_test

// Reading an interval of the archive: which segments cover it, which epoch owns each stretch, and what a
// restarted worker still has to do. The Python port's tests, ported with the code — the epoch rule is the
// part worth having twice.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"vmsserver/testbox"
	"vmsserver/vms"
)

const scanT = 1_757_500_000.0

func mm(n float64) float64 { return scanT + n*60 }

func seg(t *testing.T, box *testbox.Box, unit string, epoch int, a, b float64) vms.Segment {
	t.Helper()
	s := vms.Segment{Unit: unit, Epoch: epoch, Start: mm(a), End: mm(b),
		Path: "rec/" + unit + "/e" + itoa(epoch) + "/" + itoa(int(mm(a))) + ".mp4", Bytes: 1000, Source: "live"}
	if err := vms.NewManifest(box.Archive, unit).Append(s); err != nil {
		t.Fatal(err)
	}
	return s
}

func stretches(p []vms.Scan) [][3]float64 {
	out := make([][3]float64, 0, len(p))
	for _, s := range p {
		out = append(out, [3]float64{float64(s.Seg.Epoch), s.T0, s.T1})
	}
	return out
}

func eqStretches(t *testing.T, got [][3]float64, want [][3]float64) {
	t.Helper()
	g, _ := json.Marshal(got)
	w, _ := json.Marshal(want)
	if string(g) != string(w) {
		t.Fatalf("stretches\n got %s\nwant %s", g, w)
	}
}

func TestThePlanIsTheSegmentsTheIntervalTouches(t *testing.T) {
	box := testbox.NewBox()
	seg(t, box, "7", 1, 0, 10)
	seg(t, box, "7", 1, 10, 20)
	seg(t, box, "7", 1, 20, 30)
	plan := vms.Plan(box.Archive, "7", mm(10), mm(20))
	if len(plan) != 1 || vms.Covered(plan) != 600 {
		t.Fatalf("the one it touches, and no neighbour: %v", stretches(plan))
	}
}

func TestAStretchIsClippedToWhatWasAskedAndTheRestIsRefused(t *testing.T) {
	box := testbox.NewBox()
	seg(t, box, "7", 1, 0, 10)
	plan := vms.Plan(box.Archive, "7", mm(5), mm(8))
	if len(plan) != 1 || plan[0].T0 != mm(5) || plan[0].T1 != mm(8) || plan[0].Seconds() != 180 {
		t.Fatalf("%v", stretches(plan))
	}
	s := plan[0]
	if s.Accepts(mm(4)) || !s.Accepts(mm(5)) || !s.Accepts(mm(7)) || s.Accepts(mm(8)) {
		t.Fatal("the file is read from its head; what the model saw before the window is not the answer")
	}
}

func TestTwoEpochsOverTheSameMinutesTheLaterOneOwnsThem(t *testing.T) {
	// e1 wrote 0-20, e2 took over at 10 and wrote 10-30. Minutes 0-10 exist only under e1 and are good
	// footage; minutes 10-20 exist twice and belong to the writer that won.
	box := testbox.NewBox()
	seg(t, box, "7", 1, 0, 20)
	seg(t, box, "7", 2, 10, 30)
	plan := vms.Plan(box.Archive, "7", mm(0), mm(30))
	eqStretches(t, stretches(plan), [][3]float64{{1, mm(0), mm(10)}, {2, mm(10), mm(30)}})
	if vms.Covered(plan) != 1800 {
		t.Fatalf("thirty minutes, counted once: %v", vms.Covered(plan))
	}
}

func TestAGapIsAGapAndTheScanDoesNotInventIt(t *testing.T) {
	box := testbox.NewBox()
	seg(t, box, "7", 1, 0, 10)
	seg(t, box, "7", 1, 20, 30)
	plan := vms.Plan(box.Archive, "7", mm(0), mm(30))
	if len(plan) != 2 || vms.Covered(plan) != 1200 {
		t.Fatalf("twenty of the thirty minutes asked for: %v", stretches(plan))
	}
}

func TestOneSegmentCanComeBackInTwoPieces(t *testing.T) {
	box := testbox.NewBox()
	long := seg(t, box, "7", 1, 0, 30)
	seg(t, box, "7", 2, 10, 20)
	plan := vms.Plan(box.Archive, "7", mm(0), mm(30))
	eqStretches(t, stretches(plan), [][3]float64{{1, mm(0), mm(10)}, {2, mm(10), mm(20)}, {1, mm(20), mm(30)}})
	pieces := []vms.Scan{}
	for _, s := range plan {
		if s.Seg == long {
			pieces = append(pieces, s)
		}
	}
	if len(pieces) != 2 || pieces[0].Key() == pieces[1].Key() {
		t.Fatal("which is why the log is keyed by the stretch and not by the file")
	}
}

func TestAdjacentStretchesOfOneSegmentAreOneStretch(t *testing.T) {
	// A zombie under the OLDER epoch wrote 10-20 while the survivor wrote 0-30. Its edges are boundaries
	// all the same, and the survivor wins on both sides of both of them.
	box := testbox.NewBox()
	seg(t, box, "7", 2, 0, 30)
	seg(t, box, "7", 1, 10, 20)
	plan := vms.Plan(box.Archive, "7", mm(0), mm(30))
	if len(plan) != 1 || plan[0].Seg.Epoch != 2 || plan[0].T0 != mm(0) || plan[0].T1 != mm(30) {
		t.Fatalf("one file opened once: %v", stretches(plan))
	}
}

func TestWhatIsLoggedIsNotDoneAgain(t *testing.T) {
	box := testbox.NewBox()
	seg(t, box, "7", 1, 0, 10)
	seg(t, box, "7", 1, 10, 20)
	seg(t, box, "7", 1, 20, 30)
	plan := vms.Plan(box.Archive, "7", mm(0), mm(30))
	log := vms.NewScanLog(box.Archive, "job-1")
	if len(vms.Remaining(plan, log)) != 3 {
		t.Fatal("nothing logged: everything to do")
	}
	log.Append(plan[0], 2, mm(31))
	log.Append(plan[1], 0, mm(32))
	left := vms.Remaining(plan, log)
	if len(left) != 1 || left[0].Key() != plan[2].Key() || log.Events() != 2 {
		t.Fatalf("%v %d", stretches(left), log.Events())
	}
}

func TestTheLogIsOnTheDiskNotInTheProcess(t *testing.T) {
	box := testbox.NewBox()
	seg(t, box, "7", 1, 0, 10)
	seg(t, box, "7", 1, 10, 20)
	plan := vms.Plan(box.Archive, "7", mm(0), mm(20))
	vms.NewScanLog(box.Archive, "job-1").Append(plan[0], 1, mm(21))

	raw, err := os.ReadFile(filepath.Join(box.Archive, "detjob", "job-1", "manifest.jsonl"))
	if err != nil {
		t.Fatal("a file, readable by a process that never ran this scan:", err)
	}
	var d map[string]any
	json.Unmarshal([]byte(splitFirstLine(string(raw))), &d)
	if d["key"] != plan[0].Key() {
		t.Fatalf("%v", d)
	}
	if left := vms.Remaining(plan, vms.NewScanLog(box.Archive, "job-1")); len(left) != 1 {
		t.Fatal("a different instance, same answer")
	}
}

func TestProgressIsNotAResumePoint(t *testing.T) {
	// Stretch 1 failed and stretch 2 succeeded: resuming from DoneThrough would skip the failure silently.
	box := testbox.NewBox()
	seg(t, box, "7", 1, 0, 10)
	seg(t, box, "7", 1, 10, 20)
	plan := vms.Plan(box.Archive, "7", mm(0), mm(20))
	log := vms.NewScanLog(box.Archive, "job-1")
	log.Append(plan[1], 0, mm(21))
	if log.DoneThrough() != mm(20) {
		t.Fatalf("it says twenty minutes: %v", log.DoneThrough())
	}
	if left := vms.Remaining(plan, log); len(left) != 1 || left[0].Key() != plan[0].Key() {
		t.Fatal("…and the first ten are still to do")
	}
}

func TestAScanOfARecordingThatWasNeverMadeIsEmptyNotAnError(t *testing.T) {
	box := testbox.NewBox()
	if plan := vms.Plan(box.Archive, "7", mm(0), mm(30)); len(plan) != 0 || vms.Covered(plan) != 0 {
		t.Fatalf("%v", stretches(plan))
	}
}

func splitFirstLine(s string) string {
	for i := 0; i < len(s); i++ {
		if s[i] == '\n' {
			return s[:i]
		}
	}
	return s
}
