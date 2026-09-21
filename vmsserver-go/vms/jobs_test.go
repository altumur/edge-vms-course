package vms_test

// The reaper: a job's row follows the worker that finished it — and only that worker. Plus the question
// this port has now got wrong twice: is it called?

import (
	"os"
	"strings"
	"testing"

	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/w2cplatform"
)

// Two controllers over one store, as the cluster really has them: the console's token writes the
// operator's rows, the controller's token writes placement. The reaper runs with the CONSOLE's, which is
// the whole question this file asks.
func jobCtls(box *testbox.Box) (con, adm *p.SpecController) {
	con = p.NewSpecController(vms.DetJobSpec, box.Vars.AsWriter("console", vms.DetJobSpec.ACLConsole()...), box.Objects, 2, box.Wall.Now, "")
	adm = p.NewSpecController(vms.DetJobSpec, box.Vars.AsWriter("detjobcontroller", vms.DetJobSpec.ACLController()...), box.Objects, 2, box.Wall.Now, "")
	return con, adm
}

func jobHeartbeat(box *testbox.Box, worker, server string, status ...map[string]any) {
	if status == nil {
		status = []map[string]any{}
	}
	box.Objects.Put(vms.DetJobSpec.Sub().HeartbeatKey(worker), p.Heartbeat{Worker: worker, Ts: box.Wall.Now(),
		Status: status, Extra: map[string]any{"server": server, "capacity": 2, "headroom": 2, "labels": "gpu"}}.ToBytes())
}

func aJob(t *testing.T, ctl *p.SpecController, name string) string {
	t.Helper()
	if _, err := ctl.Create(p.Row{"name": name, "cam": "7", "rec": "7", "kind": "lpr", "from": 100.0, "to": 200.0}); err != nil {
		t.Fatal(err)
	}
	return name
}

func reaped(t *testing.T, ctl *p.SpecController, done, failed int) {
	t.Helper()
	m := vms.Reap(ctl, 60)
	if m["done"] != done || m["failed"] != failed {
		t.Fatalf("reaped %v, wanted done=%d failed=%d", m, done, failed)
	}
}

func TestAFinishedJobIsFinishedInTheRowTheOperatorCreated(t *testing.T) {
	box := testbox.NewBox()
	ctl, adm := jobCtls(box)
	jobHeartbeat(box, "j-1", "srv-1")
	job := aJob(t, ctl, "7-lpr-1")
	if _, err := adm.EnsurePlaced(nil); err != nil {
		t.Fatal(err)
	}
	if p.Str(ctl.Unit(job)["state"]) != "queued" {
		t.Fatalf("%v", ctl.Unit(job))
	}

	jobHeartbeat(box, "j-1", "srv-1", map[string]any{"id": job, "phase": "done", "covered": 600.0, "events": 3})
	reaped(t, ctl, 1, 0)
	if p.Str(ctl.Unit(job)["state"]) != "done" {
		t.Fatalf("%v", ctl.Unit(job))
	}
}

func TestAJobStillRunningIsNotCountedAsFinished(t *testing.T) {
	// Its row follows the phase — the operator opened the row, not the heartbeat — but nothing is counted,
	// and the predicate leaves it placed.
	box := testbox.NewBox()
	ctl, adm := jobCtls(box)
	jobHeartbeat(box, "j-1", "srv-1")
	job := aJob(t, ctl, "7-lpr-1")
	adm.EnsurePlaced(nil)
	jobHeartbeat(box, "j-1", "srv-1", map[string]any{"id": job, "phase": "running", "done_through": 150.0})
	reaped(t, ctl, 0, 0)
	if p.Str(ctl.Unit(job)["state"]) != "running" || adm.Placement(job) == nil {
		t.Fatalf("%v", ctl.Unit(job))
	}
	rev := p.Str(ctl.Unit(job)["revision"]) // and it keeps saying `running` for hours: the row must not
	reaped(t, ctl, 0, 0)                    // take a new revision every thirty seconds for every reader downstream
	if p.Str(ctl.Unit(job)["revision"]) != rev {
		t.Fatalf("a running job moved its row again: %s -> %s", rev, p.Str(ctl.Unit(job)["revision"]))
	}
}

func TestAFinishedRowIsNotPulledBackByAHeartbeatFromBefore(t *testing.T) {
	// The row is `done` and the predicate has not un-placed the job yet. The heartbeat in the store was
	// published a moment earlier and still says `running`. Mirroring that would restart a finished scan —
	// un-retire the row, place it again, and start it from the top, for ever.
	box := testbox.NewBox()
	ctl, adm := jobCtls(box)
	jobHeartbeat(box, "j-1", "srv-1")
	job := aJob(t, ctl, "7-lpr-1")
	adm.EnsurePlaced(nil)
	jobHeartbeat(box, "j-1", "srv-1", map[string]any{"id": job, "phase": "done", "covered": 600.0})
	reaped(t, ctl, 1, 0)
	rev := p.Str(ctl.Unit(job)["revision"])

	jobHeartbeat(box, "j-1", "srv-1", map[string]any{"id": job, "phase": "running", "done_through": 150.0})
	reaped(t, ctl, 0, 0)
	if p.Str(ctl.Unit(job)["state"]) != "done" || p.Str(ctl.Unit(job)["revision"]) != rev {
		t.Fatalf("a finished job was pulled back: %v", ctl.Unit(job))
	}
}

func TestAFailureIsRecordedAsAFailureAndNotAsDone(t *testing.T) {
	box := testbox.NewBox()
	ctl, adm := jobCtls(box)
	jobHeartbeat(box, "j-1", "srv-1")
	job := aJob(t, ctl, "7-lpr-1")
	adm.EnsurePlaced(nil)
	jobHeartbeat(box, "j-1", "srv-1", map[string]any{"id": job, "phase": "failed", "why": "the model would not load"})
	reaped(t, ctl, 0, 1)
	if p.Str(ctl.Unit(job)["state"]) != "failed" {
		t.Fatalf("%v", ctl.Unit(job))
	}
}

func TestOnlyTheWorkerTheJobIsPlacedOnIsBelieved(t *testing.T) {
	// A heartbeat object outlives its worker. `j-9` finished this job yesterday, before it moved; taking
	// its word now would end the scan `j-1` is running right this second, from the beginning.
	box := testbox.NewBox()
	ctl, adm := jobCtls(box)
	jobHeartbeat(box, "j-1", "srv-1")
	job := aJob(t, ctl, "7-lpr-1")
	adm.EnsurePlaced(nil)
	if adm.Placement(job).Worker != "j-1" {
		t.Fatal("placed elsewhere")
	}

	jobHeartbeat(box, "j-9", "srv-9", map[string]any{"id": job, "phase": "done", "covered": 600.0})
	jobHeartbeat(box, "j-1", "srv-1", map[string]any{"id": job, "phase": "running", "done_through": 150.0})
	reaped(t, ctl, 0, 0)
	if p.Str(ctl.Unit(job)["state"]) != "running" { // the placed worker's word, not the ghost's
		t.Fatalf("%v", ctl.Unit(job))
	}
}

func TestTheRowIsMovedOnceAndThenLeftAlone(t *testing.T) {
	// The pass runs every thirty seconds and the worker keeps saying `done` until something un-places it.
	// A row that moves every pass is a revision that moves every pass, and every reader downstream
	// re-reads it for nothing.
	box := testbox.NewBox()
	ctl, adm := jobCtls(box)
	jobHeartbeat(box, "j-1", "srv-1")
	job := aJob(t, ctl, "7-lpr-1")
	adm.EnsurePlaced(nil)
	jobHeartbeat(box, "j-1", "srv-1", map[string]any{"id": job, "phase": "done", "covered": 600.0})
	vms.Reap(ctl, 60)
	rev := p.Str(ctl.Unit(job)["revision"])
	reaped(t, ctl, 0, 0)
	if p.Str(ctl.Unit(job)["revision"]) != rev {
		t.Fatalf("the row moved again: %s -> %s", rev, p.Str(ctl.Unit(job)["revision"]))
	}
}

func TestTheConsolesTokenMayActuallyWriteThatRow(t *testing.T) {
	// The reaper runs with the console's grant. If `detjob` were missing from it the pass would be
	// forbidden every thirty seconds and no job would ever close — the kind of thing that is found in
	// production, not here.
	box := testbox.NewBox()
	acl := append(append(append([]string{}, vms.Spec.ACLConsole()...), vms.RecSpec.ACLConsole()...), vms.DetJobSpec.ACLConsole()...)
	ctl := p.NewSpecController(vms.DetJobSpec, box.Vars.AsWriter("console", acl...), box.Objects, 2, box.Wall.Now, "")
	_, adm := jobCtls(box)
	jobHeartbeat(box, "j-1", "srv-1")
	job := aJob(t, ctl, "7-lpr-1")
	adm.EnsurePlaced(nil)
	jobHeartbeat(box, "j-1", "srv-1", map[string]any{"id": job, "phase": "done"})
	reaped(t, ctl, 1, 0) // not forbidden
	if p.Str(ctl.Unit(job)["state"]) != "done" {
		t.Fatalf("%v", ctl.Unit(job))
	}
}

// Twice in this port a pass was written, tested and never called. Reading the source is weak evidence and
// this says so out loud — but it is the evidence that catches exactly that.
func TestTheConsoleProcessActuallyRunsTheReaper(t *testing.T) {
	src, err := os.ReadFile("../cmd/vms/main.go")
	if err != nil {
		t.Fatal(err)
	}
	text := string(src)
	i := strings.Index(text, `case "console"`)
	j := strings.Index(text[i:], `case "resource"`)
	if i < 0 || j < 0 {
		t.Fatal("no console entry point")
	}
	console := text[i : i+j]
	for _, want := range []string{"go reapLoop(", "jobCtl", "DetJobSpec.ACLConsole()"} {
		if !strings.Contains(console, want) {
			t.Fatalf("the console process does not run the reaper: %q is missing", want)
		}
	}
	if !strings.Contains(text, "vms.Reap(") {
		t.Fatal("nothing in this port calls the reaper — no job would ever close")
	}
}

func TestTheWorkersNewsIsNotTheOperatorsRow(t *testing.T) {
	// `waiting` and `unsupported` are news: the job was placed where the footage is not, or where the model
	// is not. Nothing downstream acts on either, and both are the CONTROLLER's problem — so they stay in
	// the heartbeat. A row that took them would show the operator a state their job never has, and would
	// flip it, with a fresh revision each time, every time the job is moved.
	box := testbox.NewBox()
	ctl, adm := jobCtls(box)
	jobHeartbeat(box, "j-1", "srv-1")
	job := aJob(t, ctl, "7-lpr-1")
	adm.EnsurePlaced(nil)
	rev := p.Str(ctl.Unit(job)["revision"])

	for _, news := range []string{"waiting", "unsupported"} {
		jobHeartbeat(box, "j-1", "srv-1", map[string]any{"id": job, "phase": news, "why": "not here"})
		reaped(t, ctl, 0, 0)
		if p.Str(ctl.Unit(job)["state"]) != "queued" || p.Str(ctl.Unit(job)["revision"]) != rev {
			t.Fatalf("%q reached the operator's row: %v", news, ctl.Unit(job))
		}
	}
}
