package vms_test

// The scan worker: work with both ends. A budget per pass, media time on the events, progress that
// survives a restart, and the only worker that can say done.

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/w2cplatform"
)

// A model that fires on every look and reports HOW MANY looks it has had. The count is what makes "the
// file is decoded from its head" observable: a model handed only the asked-for window would be on its
// first look there.
type everyLook struct {
	row   p.Row
	looks int
}

func newEveryLook(row p.Row) vms.Model { return &everyLook{row: row} }

func (m *everyLook) Observe(now float64) []vms.Observation {
	m.looks++
	return []vms.Observation{{Kind: p.Str(m.row["kind"]), Fields: map[string]any{"at": now, "looks": m.looks}}}
}
func (m *everyLook) Close() {}

func jobWorker(t *testing.T, box *testbox.Box, name string) *vms.DetJobWorker {
	t.Helper()
	w, err := vms.NewDetJobWorker(name, box.Vars.AsWriter("detjobworker", "detjob/epoch/*", "detjob/slots/*"), box.Objects,
		vms.DetJobOptions{Models: map[string]func(p.Row) vms.Model{"lpr": newEveryLook},
			Server: "srv-1", ArchiveRoot: box.Archive, Step: 60,
			Worker: p.WorkerOptions{Clock: box.Clock.Now, Wall: box.Wall.Now, LeaseTTL: 30, LeaseMargin: 5}})
	if err != nil {
		t.Fatal(err)
	}
	return w
}

func makeJob(t *testing.T, box *testbox.Box, name string, from, to float64) *p.SpecController {
	t.Helper()
	con := p.NewSpecController(vms.DetJobSpec, box.Vars.AsWriter("console", vms.DetJobSpec.ACLConsole()...), box.Objects, 2, box.Wall.Now, "cluster-a")
	if _, err := con.Create(p.Row{"name": name, "cam": "7", "rec": "7", "kind": "lpr", "from": from, "to": to}); err != nil {
		t.Fatal(err)
	}
	box.Vars.Put(vms.DetJobSpec.Sub().Assignment("j-1"), p.Items{"units": name, "rev": "1"}, p.Absent)
	return con
}

// assign rewrites a slot's assignment — the controller's side of a job moving.
func assign(t *testing.T, box *testbox.Box, slot, units, rev string) {
	t.Helper()
	key := vms.DetJobSpec.Sub().Assignment(slot)
	_, idx, _ := box.Vars.Get(key)
	if _, err := box.Vars.Put(key, p.Items{"units": units, "rev": rev}, idx); err != nil {
		t.Fatal(err)
	}
}

func jobLines(t *testing.T, box *testbox.Box, job string, epoch int) []p.Event {
	t.Helper()
	dir := filepath.Join(box.Archive, "detjob", job, "e"+itoa(epoch))
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

func TestAScanWritesWhatItSawIntoItsOwnTreeUnderItsEpoch(t *testing.T) {
	box := testbox.NewBox()
	seg(t, box, "7", 1, 0, 10)
	makeJob(t, box, "7-lpr-1", mm(0), mm(10))
	w := jobWorker(t, box, "j-1")
	w.ReconcileOnce()

	lines := jobLines(t, box, "7-lpr-1", w.Epochs["7-lpr-1"])
	if len(lines) == 0 {
		t.Fatal("the scan ran and wrote nothing")
	}
	for _, l := range lines {
		if p.ToFloat(l["cam"]) != 7 || p.Str(l["source"]) != "archive" || p.Str(l["job"]) != "7-lpr-1" {
			t.Fatalf("%v", l)
		}
	}
	if _, err := os.Stat(filepath.Join(box.Archive, "det", "7-lpr-1")); err == nil {
		t.Fatal("never the live detector's tree")
	}
}

func TestTheEventsCarryMediaTimeNotTheClock(t *testing.T) {
	// The scan runs a day after the footage was recorded. Every event it writes is dated by the footage.
	box := testbox.NewBox()
	seg(t, box, "7", 1, 0, 10)
	makeJob(t, box, "7-lpr-1", mm(0), mm(10))
	box.Wall.Set(mm(0) + 86400)
	w := jobWorker(t, box, "j-1")
	w.ReconcileOnce()
	lines := jobLines(t, box, "7-lpr-1", w.Epochs["7-lpr-1"])
	if len(lines) == 0 {
		t.Fatal("the scan ran and wrote nothing")
	}
	for _, l := range lines {
		if l.T() < mm(0) || l.T() >= mm(10) {
			t.Fatalf("a scan of last Tuesday writes events dated last Tuesday: %v", l.T())
		}
	}
}

func TestWhatTheOperatorDidNotAskForDoesNotBecomeAnEvent(t *testing.T) {
	// The segment opens at 10:00 and the job asks from 10:05. The file is read from its head — the model
	// sees 10:00 — and none of it is in the answer.
	box := testbox.NewBox()
	seg(t, box, "7", 1, 0, 10)
	makeJob(t, box, "7-lpr-1", mm(5), mm(8))
	w := jobWorker(t, box, "j-1")
	w.ReconcileOnce()

	lines := jobLines(t, box, "7-lpr-1", w.Epochs["7-lpr-1"])
	if len(lines) == 0 {
		t.Fatal("nothing was written at all")
	}
	first := lines[0]
	for _, l := range lines {
		if l.T() < mm(5) || l.T() >= mm(8) {
			t.Fatalf("outside the window: %v", l.T())
		}
		if l.T() < first.T() {
			first = l
		}
	}
	if p.ToFloat(first["looks"]) <= 1 {
		t.Fatal("the model was handed the window, not the file — then nothing needed clipping")
	}
}

func TestALongJobAdvancesByABudget(t *testing.T) {
	box := testbox.NewBox()
	for i := 0.0; i < 6; i++ {
		seg(t, box, "7", 1, i*10, (i+1)*10)
	}
	makeJob(t, box, "7-lpr-1", mm(0), mm(60))
	w := jobWorker(t, box, "j-1")

	w.ReconcileOnce()
	st := w.Status("7-lpr-1")
	if p.Str(st["phase"]) != "running" || p.ToFloat(st["done_through"]) != mm(40) {
		t.Fatalf("four of six: %v", st)
	}
	w.ReconcileOnce()
	if p.ToFloat(w.Status("7-lpr-1")["done_through"]) != mm(60) {
		t.Fatalf("%v", w.Status("7-lpr-1"))
	}
	w.ReconcileOnce()
	if p.Str(w.Status("7-lpr-1")["phase"]) != "done" {
		t.Fatalf("%v", w.Status("7-lpr-1"))
	}
}

func TestARestartedWorkerResumesAndDoesNotDoubleTheEvents(t *testing.T) {
	box := testbox.NewBox()
	for i := 0.0; i < 3; i++ {
		seg(t, box, "7", 1, i*10, (i+1)*10)
	}
	makeJob(t, box, "7-lpr-1", mm(0), mm(30))
	w := jobWorker(t, box, "j-1")
	w.ReconcileOnce()
	before := vms.NewScanLog(box.Archive, "7-lpr-1").Events()

	w2 := jobWorker(t, box, "j-1") // same slot, new process
	w2.ReconcileOnce()
	if vms.NewScanLog(box.Archive, "7-lpr-1").Events() != before {
		t.Fatal("something was scanned twice")
	}
	if p.Str(w2.Status("7-lpr-1")["phase"]) != "done" {
		t.Fatalf("%v", w2.Status("7-lpr-1"))
	}
}

func TestNoFootageHereIsNotNoEvents(t *testing.T) {
	box := testbox.NewBox()
	makeJob(t, box, "7-lpr-1", mm(0), mm(10)) // no manifest on this server
	w := jobWorker(t, box, "j-1")
	w.ReconcileOnce()
	st := w.Status("7-lpr-1")
	if p.Str(st["phase"]) != "waiting" || p.Str(st["why"]) == "" {
		t.Fatalf("which silence it is decides what happens next: %v", st)
	}
}

func TestTheHeartbeatSaysHowMuchOfTheIntervalHadFootage(t *testing.T) {
	box := testbox.NewBox()
	seg(t, box, "7", 1, 0, 20)
	seg(t, box, "7", 1, 40, 60)
	makeJob(t, box, "7-lpr-1", mm(0), mm(60))
	w := jobWorker(t, box, "j-1")
	w.ReconcileOnce()
	st := w.Status("7-lpr-1")
	if p.ToFloat(st["asked"]) != 3600 || p.ToFloat(st["covered"]) != 2400 {
		t.Fatalf("nothing happened and nothing was recorded are different answers: %v", st)
	}
}

func TestATerminalRowIsReportedAndNotWorkedOn(t *testing.T) {
	box := testbox.NewBox()
	for i := 0.0; i < 6; i++ {
		seg(t, box, "7", 1, i*10, (i+1)*10)
	}
	con := makeJob(t, box, "7-lpr-1", mm(0), mm(60))
	w := jobWorker(t, box, "j-1")
	w.ReconcileOnce()
	done := len(vms.NewScanLog(box.Archive, "7-lpr-1").Read())
	if done == 0 || done >= 6 {
		t.Fatalf("the job has to be really unfinished for this test to mean anything: %d", done)
	}

	con.Update("7-lpr-1", p.Row{"state": "done"})
	w2 := jobWorker(t, box, "j-1")
	w2.ReconcileOnce()
	if p.Str(w2.Status("7-lpr-1")["phase"]) != "done" {
		t.Fatalf("%v", w2.Status("7-lpr-1"))
	}
	if _, took := w2.Epochs["7-lpr-1"]; took {
		t.Fatal("an epoch was taken for work that is over")
	}
	if len(vms.NewScanLog(box.Archive, "7-lpr-1").Read()) != done {
		t.Fatal("a stretch was worked on after the row went terminal")
	}
}

func TestTheScanRetiresAndTheSurveyWouldNot(t *testing.T) {
	if vms.DetJobSpec.RetireField != "state" || len(vms.DetJobSpec.RetireValues) != 2 {
		t.Fatal(vms.DetJobSpec.RetireField, vms.DetJobSpec.RetireValues)
	}
	if vms.DetJobSpec.OlderEpochs != "earlier-run" {
		t.Fatal("a finished earlier run is not a writer that lost the race")
	}
	if vms.DetJobSpec.Near != "rec" || vms.DetJobSpec.NearBy != "rec" {
		t.Fatal("a scan follows the RECORDING it reads, not its own name")
	}
}

// allJobLines: every event of a job, under every epoch. The takeover test needs the TOTAL — a worker that
// re-took the epoch and went on writing would leave the old epoch's count untouched.
func allJobLines(t *testing.T, box *testbox.Box, job string) int {
	t.Helper()
	entries, err := os.ReadDir(filepath.Join(box.Archive, "detjob", job))
	if err != nil {
		return 0
	}
	n := 0
	for _, e := range entries {
		ep, _ := strconv.Atoi(strings.TrimPrefix(e.Name(), "e"))
		n += len(jobLines(t, box, job, ep))
	}
	return n
}

func TestAWorkerWhoseLeaseLapsedStopsWriting(t *testing.T) {
	// A pass of this worker is hours of video, not one frame. If its lease ran out the controller has long
	// since called it dead and given the job to somebody else — and a second writer of one job does not
	// lose events, it DOUBLES them. The lease is checked before the budget is spent, not after.
	box := testbox.NewBox()
	for i := 0.0; i < 6; i++ {
		seg(t, box, "7", 1, i*10, (i+1)*10)
	}
	makeJob(t, box, "7-lpr-1", mm(0), mm(60))
	w := jobWorker(t, box, "j-1")
	w.ReconcileOnce()
	mine, wrote := w.Epochs["7-lpr-1"], allJobLines(t, box, "7-lpr-1")
	done := len(vms.NewScanLog(box.Archive, "7-lpr-1").Read())
	if wrote == 0 || done >= 6 {
		t.Fatalf("the job has to be really unfinished for this test to mean anything: %d lines, %d stretches", wrote, done)
	}

	box.Clock.Advance(26) // past the lease, short of a renewal
	if w.MayWrite("7-lpr-1") {
		t.Fatal("expired")
	}
	w.ReconcileOnce()
	if w.Epochs["7-lpr-1"] != mine {
		t.Fatal("the worker took a fresh epoch for itself instead of stopping")
	}
	if n := allJobLines(t, box, "7-lpr-1"); n != wrote {
		t.Fatalf("it spent a budget of stretches with no lease: %d -> %d", wrote, n)
	}
	if len(vms.NewScanLog(box.Archive, "7-lpr-1").Read()) != done {
		t.Fatal("and called them done")
	}
}

func TestADeletedJobIsNotWorkedOn(t *testing.T) {
	// The operator cancelled it. The row is gone before the controller unplaced it, and a worker that reads
	// the row every pass has no reason to finish an interval nobody asked for any more.
	box := testbox.NewBox()
	for i := 0.0; i < 6; i++ {
		seg(t, box, "7", 1, i*10, (i+1)*10)
	}
	con := makeJob(t, box, "7-lpr-1", mm(0), mm(60))
	w := jobWorker(t, box, "j-1")
	w.ReconcileOnce()
	done := len(vms.NewScanLog(box.Archive, "7-lpr-1").Read())
	if done == 0 || done >= 6 {
		t.Fatalf("the job has to be really unfinished for this test to mean anything: %d", done)
	}

	if err := con.Delete("7-lpr-1"); err != nil {
		t.Fatal(err)
	}
	w.ReconcileOnce()
	if len(vms.NewScanLog(box.Archive, "7-lpr-1").Read()) != done {
		t.Fatal("a stretch was worked on after the row was deleted")
	}
}

func TestAFinishedJobGivesItsCapacityBack(t *testing.T) {
	// Work that ends has to be let go of. The model is closed and the job leaves the running set, or a
	// worker that scanned all day reports no headroom and placement never sends it anything again.
	box := testbox.NewBox()
	for i := 0.0; i < 6; i++ {
		seg(t, box, "7", 1, i*10, (i+1)*10)
	}
	makeJob(t, box, "7-lpr-1", mm(0), mm(60))
	w := jobWorker(t, box, "j-1")
	var running []string
	for i := 0; i < 3; i++ {
		running = w.ReconcileOnce()
	}
	if p.Str(w.Status("7-lpr-1")["phase"]) != "done" {
		t.Fatalf("the job has to be finished for this test to mean anything: %v", w.Status("7-lpr-1"))
	}
	if len(running) != 0 {
		t.Fatalf("a finished job is still being run: %v", running)
	}
	if w.Headroom() != w.Capacity {
		t.Fatalf("headroom %d of %d after the only job ended", w.Headroom(), w.Capacity)
	}
}

func TestAJobThatComesBackTakesAFreshEpoch(t *testing.T) {
	// The job went to another worker and came back. The epoch it holds now is not the one it held then —
	// and a worker that kept the old one would hold a fenced lease and quietly never finish the job.
	box := testbox.NewBox()
	for i := 0.0; i < 6; i++ {
		seg(t, box, "7", 1, i*10, (i+1)*10)
	}
	makeJob(t, box, "7-lpr-1", mm(0), mm(60))
	w := jobWorker(t, box, "j-1")
	w.ReconcileOnce()
	mine, wrote := w.Epochs["7-lpr-1"], allJobLines(t, box, "7-lpr-1")

	other := jobWorker(t, box, "j-2") // somebody else ran it meanwhile
	if _, err := other.TakeEpoch("7-lpr-1"); err != nil {
		t.Fatal(err)
	}
	assign(t, box, "j-1", "", "2")
	w.ReconcileOnce()

	assign(t, box, "j-1", "7-lpr-1", "3")
	w.ReconcileOnce()
	if w.Epochs["7-lpr-1"] <= mine {
		t.Fatalf("it came back under the epoch it left with: %d", w.Epochs["7-lpr-1"])
	}
	if allJobLines(t, box, "7-lpr-1") <= wrote {
		t.Fatal("the job came back and nothing more was scanned")
	}
}

func TestAKindThisBuildCannotRunIsSaidSoAndNotFaked(t *testing.T) {
	// A job asking for a model this build does not have is not a job for a stand-in. It is reported, and
	// no epoch is taken, so the console can place it where the model exists.
	box := testbox.NewBox()
	seg(t, box, "7", 1, 0, 10)
	con := p.NewSpecController(vms.DetJobSpec, box.Vars.AsWriter("console", vms.DetJobSpec.ACLConsole()...), box.Objects, 2, box.Wall.Now, "cluster-a")
	if _, err := con.Create(p.Row{"name": "7-face-1", "cam": "7", "rec": "7", "kind": "face", "from": mm(0), "to": mm(10)}); err != nil {
		t.Fatal(err)
	}
	box.Vars.Put(vms.DetJobSpec.Sub().Assignment("j-1"), p.Items{"units": "7-face-1", "rev": "1"}, p.Absent)
	w := jobWorker(t, box, "j-1")
	w.ReconcileOnce()

	if p.Str(w.Status("7-face-1")["phase"]) != "unsupported" {
		t.Fatalf("%v", w.Status("7-face-1"))
	}
	if _, took := w.Epochs["7-face-1"]; took {
		t.Fatal("an epoch was taken for a model we do not have")
	}
	if allJobLines(t, box, "7-face-1") != 0 || len(vms.NewScanLog(box.Archive, "7-face-1").Read()) != 0 {
		t.Fatal("something was scanned by a stand-in model")
	}
}
