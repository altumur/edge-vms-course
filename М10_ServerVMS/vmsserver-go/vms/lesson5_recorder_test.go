package vms_test

// Lesson 5 — the recorder: the fourth subsystem, and the only one placed on
// top of the archive. A recording is a unit named by its camera; the recorder
// subscribes to the tee of whichever worker holds the camera — from the
// heartbeat, never a second connection: shared memory on the same server, the
// RTSP fan-out from another — takes its own epoch, and writes footage into
// rec/<cam>/e<epoch>/ on ITS server's archive. The worker that holds the
// camera may be anywhere; the recorder must be where the disks are, and
// prefers to be beside the worker. Two trees, two writers, one camera.

import (
	"errors"
	"os"
	"strings"
	"testing"

	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/w2cplatform"
)

type recBox struct {
	box            *testbox.Box
	ctl, con       *vms.VmsController
	recCon, recCtl *p.SpecController
	w              *vms.VmsWorker
}

func recSetup(t *testing.T) *recBox {
	t.Helper()
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars.AsWriter("vmscontroller", vms.Spec.ACLController()...), box.Objects, 0, box.Wall.Now)
	conVars := box.Vars.AsWriter("vmsconsole", append(vms.Spec.ACLConsole(), vms.RecSpec.ACLConsole()...)...)
	con := vms.NewVmsController(conVars, box.Objects, 0, box.Wall.Now)
	recCon := p.NewSpecController(vms.RecSpec, conVars, box.Objects, 0, box.Wall.Now, "") // the console's door to recordings
	recCtl := p.NewSpecController(vms.RecSpec, box.Vars.AsWriter("reccontroller", vms.RecSpec.ACLController()...), box.Objects, 0, box.Wall.Now, "")
	w := worker(t, box, "w-1", vms.NewFakeActuator(), vms.VmsWorkerOptions{Server: "srv-1", ArchiveRoot: box.Archive})
	w.HeartbeatOnce()
	mustCreate(t, con, map[string]any{"name": "gate", "source": "driverpack://file/gate.mp4"})
	ctl.EnsurePlaced(nil)
	w.ReconcileOnce()
	w.HeartbeatOnce()
	return &recBox{box, ctl, con, recCon, recCtl, w}
}

func recorder(t *testing.T, box *testbox.Box, name, server string, capacity int) *vms.RecWorker {
	t.Helper()
	arch := vms.NewArchiveResource(box.Spool, box.Archive, 600, box.Wall.Now)
	r, err := vms.NewRecWorker(name, box.Vars.AsWriter("recworker", "rec/epoch/*", "rec/slots/*"), box.Objects, vms.NewFakeActuator(), arch,
		vms.VmsWorkerOptions{WorkerOptions: p.WorkerOptions{Clock: box.Clock.Now, Wall: box.Wall.Now}, Server: server, Capacity: capacity, Env: vms.Env{}})
	if err != nil {
		t.Fatal(err)
	}
	r.HeartbeatOnce()
	return r
}

func fake(a vms.Actuator) *vms.FakeActuator { return a.(*vms.FakeActuator) }

func TestARecordingIsAUnitPlacedOnTheArchiveAndFedByTheWorkersTee(t *testing.T) {
	rb := recSetup(t)
	box, ctl, recCon, recCtl, w := rb.box, rb.ctl, rb.recCon, rb.recCtl, rb.w
	if vms.RecSpec.Requires != "resource" || recCtl.Policy()["servers"] != "distinct" || ctl.Policy()["servers"] != "shared" { // the recorder must be where the disks are, one per server; workers are not
		t.Fatal(recCtl.Policy(), ctl.Policy())
	}
	if vms.RecSpec.Near != "vms" { // and it prefers the server whose worker holds the camera: an affinity, not a filter
		t.Fatal(vms.RecSpec.Near)
	}
	r, r2 := recorder(t, box, "r-1", "srv-1", 50), recorder(t, box, "r-2", "srv-2", 1) // two servers with archives; the camera's worker is on srv-1
	if slots, _ := box.Vars.List("rec/slots/"); r.Name != "r-1" || r.Sub.Name != "rec" || len(slots) != 2 {
		t.Fatal(r.Name, slots)
	}
	// the operator records camera 1: a row under rec/, the console's token; placement is the rec controller's pass
	row, err := recCon.Create(map[string]any{"cam": "1", "retention_days": 7})
	if err != nil || row.ID() != "1" || row.Int("retention_days") != 7 {
		t.Fatal(row, err)
	}
	if _, err := recCon.Place("1", nil); !errors.Is(err, p.ErrForbidden) { // a console token never writes placement
		t.Fatal(err)
	}
	pls, _ := recCtl.EnsurePlaced(nil)
	if pls[0].Worker != "r-1" || !strings.HasSuffix(pls[0].Reason, "on srv-1, whose resource is unknown, beside w-1 holding it") { // the affinity, and the reason says so
		t.Fatal(pls[0])
	}
	// the recorder's pass: the pipeline is built from the worker's tee — its shared-memory branch, since the worker is on THIS
	// server (no RTSP hop, no fan-out process on the recording path) — under the RECORDER's epoch
	if acts := r.ReconcileOnce(); len(acts) != 1 || acts[0] != (vms.Action{Verb: "start", ID: 1}) {
		t.Fatal(acts)
	}
	started := fake(r.Act).Started[1]
	if started.Source != vms.LiveShm(1, vms.ShmDir) || started.Source != "shm:///run/vms/1.shm" || started.Via != "shm" || started.SourceServer != "srv-1" {
		t.Fatal(started)
	}
	if vms.LiveURL("srv-1", 1) != "rtsp://srv-1:8554/1" { // what a recorder on another server would read
		t.Fatal(vms.LiveURL("srv-1", 1))
	}
	if it, _, _ := box.Vars.Get("rec/epoch/1"); started.Epoch != 1 || it["epoch"] != "1" || w.Epochs["1"] != 1 { // two epochs, two writers, one camera
		t.Fatal(started.Epoch, it, w.Epochs)
	}
	if started.Spool != box.Spool || started.Archive != box.Archive {
		t.Fatal(started)
	}
	r.HeartbeatOnce()
	st := recCtl.WorkersSeen(45)["r-1"].Status[0]
	if st["phase"] != "running" || st["cam"] != "1" || st["source"] != "shm:///run/vms/1.shm" || st["via"] != "shm" || p.ToFloat(st["epoch"]) != 1 {
		t.Fatal(st)
	}
	if !strings.Contains(r.MetricsText(), "rec_recordings_running 1") {
		t.Fatal(r.MetricsText())
	}
	// a segment the pipeline closed is promoted into rec/<cam>/e<epoch>/ and indexed beside it — the worker's tree stays events-only
	pth := writeSegment(t, box.Spool, 1, 1, "2026-09-12T10:00:00", 100, box.Wall.Now()-600)
	r.PumpOnce()
	if _, err := os.Stat(pth); r.Promoted != 1 || err == nil || vms.NewManifest(box.Archive, 1).Read()[0].Path != "rec/1/e1/20260912T100000Z.mp4" {
		t.Fatal(r.Promoted, err)
	}
	w.Observe(1, "motion", nil)
	if under := p.SubsystemsUnder(box.Archive); len(under) != 2 || under["rec"][0] != "1" || under["vms"][0] != "1" {
		t.Fatal(under)
	}
	// the camera's worker fails over to srv-2: the recorder re-subscribes — to the RTSP fan-out now, the worker is on another
	// server — same recorder, same disks, same tree; a new pipeline is a new epoch (e1 before the move, e2 after, both here)
	w2 := worker(t, box, "w-2", vms.NewFakeActuator(), vms.VmsWorkerOptions{Server: "srv-2", ArchiveRoot: box.Archive})
	ctl.MoveTo(1, "w-2", "test")
	w2.ReconcileOnce()
	w2.HeartbeatOnce()
	w.ReconcileOnce()
	w.HeartbeatOnce()
	if moved := r.Resubscribe(); len(moved) != 1 || moved[0] != 1 || fake(r.Act).Calls[len(fake(r.Act).Calls)-1] != (vms.Action{Verb: "stop", ID: 1}) {
		t.Fatal(moved)
	}
	box.Clock.Advance(10)
	if acts := r.ReconcileOnce(); len(acts) != 1 || acts[0] != (vms.Action{Verb: "start", ID: 1}) {
		t.Fatal(acts)
	}
	started = fake(r.Act).Started[1]
	if started.Source != "rtsp://srv-2:8554/1" || started.Via != "rtsp" || started.Epoch != 2 || recCtl.Where("1") != "r-1" || w2.Epochs["1"] != 2 { // the recording did not move; its source did
		t.Fatal(started, recCtl.Where("1"))
	}
	// two more cameras, both held on srv-2 by w-2. The affinity puts the first recording beside w-2 — r-2 reads shared memory
	// there; r-2 (capacity 1) is then full, so the second goes to r-1 and reads the fan-out — the reason says where it would rather be
	mustCreate(t, rb.con, map[string]any{"name": "yard", "source": "driverpack://file/yard.mp4"})
	mustCreate(t, rb.con, map[string]any{"name": "dock", "source": "driverpack://file/dock.mp4"})
	ctl.EnsurePlaced(nil)
	ctl.MoveTo(2, "w-2", "test")
	ctl.MoveTo(3, "w-2", "test")
	w2.ReconcileOnce()
	w2.HeartbeatOnce()
	w.ReconcileOnce()
	w.HeartbeatOnce()
	recCon.Create(map[string]any{"cam": "2"})
	recCon.Create(map[string]any{"cam": "3"})
	pls, _ = recCtl.EnsurePlaced(nil) // the pass returns every placement, camera 1's first
	pl2, pl3 := pls[len(pls)-2], pls[len(pls)-1]
	if pl2.Worker != "r-2" || pl3.Worker != "r-1" {
		t.Fatal(pl2, pl3)
	}
	if !strings.HasSuffix(pl2.Reason, "on srv-2, whose resource is unknown, beside w-2 holding it") || !strings.HasSuffix(pl3.Reason, "on srv-1, whose resource is unknown, away from w-2 on srv-2 (no room there)") {
		t.Fatal(pl2.Reason, pl3.Reason)
	}
	if acts := r2.ReconcileOnce(); len(acts) != 1 || acts[0] != (vms.Action{Verb: "start", ID: 2}) || fake(r2.Act).Started[2].Via != "shm" || fake(r2.Act).Started[2].Source != "shm:///run/vms/2.shm" {
		t.Fatal(acts, fake(r2.Act).Started[2])
	}
	if acts := r.ReconcileOnce(); len(acts) != 1 || acts[0] != (vms.Action{Verb: "start", ID: 3}) || fake(r.Act).Started[3].Via != "rtsp" || fake(r.Act).Started[3].Source != "rtsp://srv-2:8554/3" {
		t.Fatal(acts, fake(r.Act).Started[3])
	}
}

func TestARecordingWaitsWhileNobodyHoldsTheCameraAndRecordsWhenSomeoneDoes(t *testing.T) {
	rb := recSetup(t)
	box, ctl, con, recCon, recCtl, w := rb.box, rb.ctl, rb.con, rb.recCon, rb.recCtl, rb.w
	mustCreate(t, con, map[string]any{"name": "yard", "source": "driverpack://file/yard.mp4"}) // camera 2: created, not placed yet
	r := recorder(t, box, "r-1", "srv-1", 50)
	recCon.Create(map[string]any{"cam": "2"})
	recCtl.EnsurePlaced(nil)
	if acts := r.ReconcileOnce(); len(acts) != 1 || acts[0] != (vms.Action{Verb: "failed", ID: 2}) { // no source to subscribe to
		t.Fatal(acts)
	}
	r.HeartbeatOnce()
	st := recCtl.WorkersSeen(45)["r-1"].Status[0]
	if st["phase"] != "waiting" || st["why"] != "camera held by nobody" || st["source"] != nil {
		t.Fatal(st)
	}
	ctl.EnsurePlaced(nil) // the worker takes it
	w.ReconcileOnce()
	w.HeartbeatOnce()
	box.Clock.Advance(10)
	if acts := r.ReconcileOnce(); len(acts) != 1 || acts[0] != (vms.Action{Verb: "start", ID: 2}) || fake(r.Act).Started[2].Source != "shm:///run/vms/2.shm" { // held here: the tee's shared memory
		t.Fatal(acts, fake(r.Act).Started[2])
	}
	// a camera with no recording is watched, not recorded: it is held (live, detection, events), and has no rec/ tree
	units := recCtl.Units()
	if len(units) != 1 || units[0].ID() != "2" || units[0].Int("retention_days") != 30 || !units[0].Bool("enabled") {
		t.Fatal(units)
	}
	if cams := ctl.Cameras(); len(cams) != 2 || len(p.SubsystemsUnder(box.Archive)) != 0 {
		t.Fatal(cams)
	}
	w.Observe(1, "motion", nil)
	if under := p.SubsystemsUnder(box.Archive); len(under) != 1 || under["vms"][0] != "1" {
		t.Fatal(under)
	}
	// stop recording: the row goes, the placement is taken back on the next pass, the footage stays until retention
	recCon.Delete("2")
	recCtl.UnplaceDeleted()
	if a := recCtl.Assignment("r-1"); len(a.Units) != 0 {
		t.Fatal(a)
	}
	if acts := r.ReconcileOnce(); len(acts) != 1 || acts[0] != (vms.Action{Verb: "stop", ID: 2}) {
		t.Fatal(acts)
	}
}
