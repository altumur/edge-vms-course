package vms_test

// Lesson 22 — the rolling upgrade. Two mechanisms and one asymmetry:
//
//	home     a PREFERENCE naming the server a unit belongs to: at home when home is there, anywhere when
//	         it is not, and back one unit a pass when it returns. A label would be a filter, and a filter
//	         would stop the recording at exactly the moment it must not.
//	drain    one row holding ONE server name, written by the operator. Being noticed is the slow path; an
//	         upgrade is known in advance, so the work leaves first and the machine stops empty.
//
// And the asymmetry: of a following pair exactly one may follow. The anchor is the recording, because it
// writes to a disk and a disk does not move.

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/w2cplatform"
)

// recAlive: a recorder heartbeating on `server`, and that server's resource answering — `rec` requires
// one, so a recorder whose resource has gone quiet is not a recorder the controller will place on.
func recAlive(t *testing.T, box *testbox.Box, worker, server string, labels ...string) {
	t.Helper()
	hb, _ := json.Marshal(map[string]any{"worker": worker, "ts": box.Wall.Now(), "status": []any{},
		"server": server, "capacity": 50, "headroom": 50, "labels": strings.Join(labels, ",")})
	if err := box.Objects.Put(vms.RecSpec.Sub().HeartbeatKey(worker), hb); err != nil {
		t.Fatal(err)
	}
	res, _ := json.Marshal(map[string]any{"server": server, "ts": box.Wall.Now(),
		"url": "http://" + server, "units": map[string]any{}})
	if err := box.Objects.Put("platform/resources/"+server+"/heartbeat", res); err != nil {
		t.Fatal(err)
	}
}

func recHomeBox(t *testing.T) (*testbox.Box, *p.SpecController) {
	t.Helper()
	box := testbox.NewBox()
	rec := p.NewSpecController(vms.RecSpec, box.Vars, box.Objects, 0, box.Wall.Now, "")
	recAlive(t, box, "r-1", "srv-a")
	recAlive(t, box, "r-2", "srv-b")
	return box, rec
}

func TestARecordingPrefersItsHomeAndIsWrittenAnywhereWhenItIsDown(t *testing.T) {
	// The one thing `home` must not be is a label. A label is a filter: with `labels: [srv-a]` a recording
	// whose server is down becomes unplaceable and the recording stops — at exactly the moment it must
	// not. `home` is a preference: at home when home is there, anywhere when it is not, and back, one a
	// pass, when it returns. The footage written meanwhile stays where it was written until that server
	// needs the room (space.go).
	box, rec := recHomeBox(t)
	if _, err := rec.Create(map[string]any{"cam": "1", "home": "srv-a"}); err != nil {
		t.Fatal(err)
	}
	rec.EnsurePlaced(nil)
	if rec.Where("1") != "r-1" || !strings.Contains(rec.Placement("1").Reason, "at home on srv-a") {
		t.Fatal(rec.Where("1"), rec.Placement("1"))
	}

	box.Wall.Advance(60)             // srv-a goes away, with its resource…
	recAlive(t, box, "r-2", "srv-b") // …and only srv-b keeps saying anything
	if _, err := rec.MoveTo("1", "r-2", "srv-a gone"); err != nil {
		t.Fatal(err)
	}
	if _, err := rec.Create(map[string]any{"cam": "2", "home": "srv-a"}); err != nil { // a NEW recording of srv-a's, while it is down
		t.Fatal(err)
	}
	rec.EnsurePlaced(nil)
	if rec.Where("2") != "r-2" || !strings.Contains(rec.Placement("2").Reason, "away from home srv-a") {
		t.Fatal(rec.Where("2"), rec.Placement("2"))
	}
	if u := rec.Unplaceable(); len(u) != 0 {
		t.Fatal("a preference behaved like a filter:", u) // the point: it RECORDS, it is not "unplaceable"
	}

	recAlive(t, box, "r-1", "srv-a")
	recAlive(t, box, "r-2", "srv-b")
	moves := rec.EnsureHome(1, nil) // one a pass: every move is a new epoch and a seam in the recording
	if len(moves) != 1 || moves[0] != (p.Move{Unit: "1", From: "r-2", To: "r-1"}) {
		t.Fatal(moves)
	}
	if rec.Where("1") != "r-1" || rec.Where("2") != "r-2" {
		t.Fatal(rec.Where("1"), rec.Where("2"))
	}
	if !strings.Contains(rec.Placement("1").Reason, "home is srv-a") {
		t.Fatal(rec.Placement("1"))
	}
	if moves = rec.EnsureHome(1, nil); len(moves) != 1 || moves[0].Unit != "2" || rec.Where("2") != "r-1" {
		t.Fatal(moves)
	}
	if moves = rec.EnsureHome(1, nil); len(moves) != 0 { // everybody home: nothing to say
		t.Fatal(moves)
	}
}

func TestARecordingWithNoHomeIsNeverMovedByIt(t *testing.T) {
	// Every recording until an operator says otherwise. An empty field is not a server name: a homeless
	// recording is placed on the disk with the most room, and stays there.
	box, rec := recHomeBox(t)
	_ = box
	if _, err := rec.Create(map[string]any{"cam": "1"}); err != nil {
		t.Fatal(err)
	}
	rec.EnsurePlaced(nil)
	where := rec.Where("1")
	if where == "" || strings.Contains(rec.Placement("1").Reason, "home") {
		t.Fatal(rec.Placement("1"))
	}
	if moves := rec.EnsureHome(5, nil); len(moves) != 0 || rec.Where("1") != where {
		t.Fatal(moves, rec.Where("1"))
	}
}

func TestTheFiltersStillBeatThePreference(t *testing.T) {
	// `home` ORDERS what is already eligible; it never widens it. A recording whose labels no recorder on
	// its home server can serve is placed where they CAN be served, and EnsureHome leaves it there — a
	// preference that could overrule a filter would put a recording on a server whose disks the operator
	// ruled out.
	box, rec := recHomeBox(t)
	recAlive(t, box, "r-1", "srv-a", "disks:slow")
	recAlive(t, box, "r-2", "srv-b", "disks:fast")
	if _, err := rec.Create(map[string]any{"cam": "1", "home": "srv-a", "labels": []any{"disks:fast"}}); err != nil {
		t.Fatal(err)
	}
	rec.EnsurePlaced(nil)
	if rec.Where("1") != "r-2" || !strings.Contains(rec.Placement("1").Reason, "away from home srv-a") {
		t.Fatal(rec.Where("1"), rec.Placement("1"))
	}
	if moves := rec.EnsureHome(5, nil); len(moves) != 0 || rec.Where("1") != "r-2" {
		t.Fatal(moves, rec.Where("1"))
	}
}

func TestOnlyOneOfAFollowingPairMayBeTheFollower(t *testing.T) {
	// The asymmetry is the design, so it is asserted and not merely commented. `home: near` says "wherever
	// the thing I follow is". Two subsystems that each said it would have no anchor: every pass moves each
	// towards where the other WAS, and they swap places instead of meeting.
	if vms.Spec.Near != "rec" || vms.Spec.Home != "near" { // the camera follows
		t.Fatal(vms.Spec.Near, vms.Spec.Home)
	}
	if vms.RecSpec.Home != "home" || vms.RecSpec.Near != "none" { // the recording is the anchor, and follows nothing
		t.Fatal(vms.RecSpec.Home, vms.RecSpec.Near)
	}

	_, err := p.SpecFromMap(map[string]any{"name": "x", "unit": map[string]any{"fields": map[string]any{}},
		"placement": map[string]any{"home": "near"}})
	if err == nil || !strings.Contains(err.Error(), "needs a near to follow") {
		t.Fatal(err) // a spec that follows nothing cannot say it follows
	}
	_, err = p.SpecFromMap(map[string]any{"name": "x", "unit": map[string]any{"fields": map[string]any{}},
		"placement": map[string]any{"home": "hom"}})
	if err == nil || !strings.Contains(err.Error(), "names no field") {
		t.Fatal(err) // a home that names no field is a typo, not an empty home
	}
}

// -- draining a server -------------------------------------------------------------------------------

func TestAPlannedStopIsNotASilence(t *testing.T) {
	// A stopped process is noticed anyway — but being noticed is the SLOW path. A recorder's units move
	// when its slot has lapsed AND its server's resource is silent: two independent silences, about a
	// minute and a half of not recording. An upgrade is known in advance, so the work leaves first and the
	// machine stops empty. One row says it, and every subsystem reads it through pool and Redistribute:
	// nothing is told anything, and no subsystem learns a new word.
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars.AsWriter("vmscontroller", vms.Spec.ACLController()...), box.Objects, 0, box.Wall.Now)
	con := vms.NewVmsController(box.Vars.AsWriter("console", vms.Spec.ACLConsole()...), box.Objects, 0, box.Wall.Now)
	worker(t, box, "w-a", vms.NewFakeActuator(), vms.VmsWorkerOptions{Server: "srv-a"}).HeartbeatOnce()
	worker(t, box, "w-b", vms.NewFakeActuator(), vms.VmsWorkerOptions{Server: "srv-b"}).HeartbeatOnce()
	mustCreate(t, con, map[string]any{"name": "c0", "source": "driverpack://file/0.mp4"})
	mustCreate(t, con, map[string]any{"name": "c1", "source": "driverpack://file/1.mp4"})
	ctl.EnsurePlaced(nil)
	if ctl.Where("1") == ctl.Where("2") {
		t.Fatal("one each, by capacity:", ctl.Where("1"), ctl.Where("2"))
	}

	if err := ctl.Drain("srv-a"); !errors.Is(err, p.ErrForbidden) {
		t.Fatal("the controller wrote the operator's row:", err) // a drain is the CONSOLE's word
	}
	if err := con.Drain("srv-a"); err != nil {
		t.Fatal(err)
	}
	if ctl.Draining() != "srv-a" {
		t.Fatal(ctl.Draining())
	}
	if it, _, _ := box.Vars.Get(p.DrainKey); it["server"] != "srv-a" {
		t.Fatal(it)
	}
	var names []string
	for w := range ctl.WorkersSeen(45) {
		names = append(names, w)
	}
	if on := ctl.OnDraining(names); len(on) != 1 || on[0] != "w-a" {
		t.Fatal(on) // not placed on any more…
	}
	moves := ctl.Redistribute(nil) // …and what it has leaves, orderly
	if len(moves) != 1 || moves[0].From != "w-a" || moves[0].To != "w-b" {
		t.Fatal(moves)
	}
	if ctl.Where("1") != "w-b" || ctl.Where("2") != "w-b" {
		t.Fatal(ctl.Where("1"), ctl.Where("2"))
	}
	if !strings.Contains(ctl.Placement("1").Reason+ctl.Placement("2").Reason, "server srv-a draining") {
		t.Fatal(ctl.Placement("1"), ctl.Placement("2"))
	}

	// and a camera created while the machine is draining is not given to it either: the filter is in the
	// POOL, so it is every pass — placement, redistribution, rebalance — and not a special case in one of them
	mustCreate(t, con, map[string]any{"name": "c2", "source": "driverpack://file/2.mp4"})
	ctl.EnsurePlaced(nil)
	if ctl.Where("3") != "w-b" {
		t.Fatal("placed on a machine the operator said is about to stop:", ctl.Where("3"))
	}

	err := con.Drain("srv-b") // one machine at a time, by CAS
	var refused *p.ErrDrainRefused
	if !errors.As(err, &refused) || !strings.Contains(err.Error(), "already draining") {
		t.Fatal(err)
	}

	if err := con.Undrain(); err != nil { // the machine is back
		t.Fatal(err)
	}
	if ctl.Draining() != "" {
		t.Fatal(ctl.Draining())
	}
	if on := ctl.OnDraining(names); len(on) != 0 {
		t.Fatal(on)
	}
	if moves := ctl.Redistribute(nil); len(moves) != 0 {
		t.Fatal("nothing moves back on its own:", moves) // it is `home` and Rebalance that refill
	}
}

func TestTheDryRunAnswersBeforeTheRebootNotAfter(t *testing.T) {
	// Fifty cameras leaving a machine have to land somewhere, and "somewhere" is a fact about headroom and
	// labels. WouldStrand asks it with the machinery that will answer for real afterwards — the
	// alternative is reading /unplaceable once the server is already down.
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars, box.Objects, 0, box.Wall.Now)
	worker(t, box, "w-a", vms.NewFakeActuator(), vms.VmsWorkerOptions{Server: "srv-a", Env: vms.Env{"LABELS": "vlan:a"}}).HeartbeatOnce()
	worker(t, box, "w-b", vms.NewFakeActuator(), vms.VmsWorkerOptions{Server: "srv-b", Env: vms.Env{"LABELS": "vlan:b"}}).HeartbeatOnce()
	mustCreate(t, ctl, map[string]any{"name": "only-a", "source": "driverpack://file/1.mp4", "labels": []any{"vlan:a"}})
	mustCreate(t, ctl, map[string]any{"name": "anywhere", "source": "driverpack://file/2.mp4"})
	ctl.EnsurePlaced(nil)
	if ctl.Where("1") != "w-a" {
		t.Fatal(ctl.Where("1"))
	}

	if s := ctl.WouldStrand("srv-b", nil); len(s) != 0 { // camera 2 can go to srv-a
		t.Fatal(s)
	}
	if s := ctl.WouldStrand("srv-a", nil); len(s) != 1 || s[0] != "1" { // camera 1 cannot go anywhere else: say so BEFORE
		t.Fatal(s)
	}
	if u := ctl.Unplaceable(); len(u) != 0 { // and nothing is stranded yet — this is a question, not a state
		t.Fatal(u)
	}
}

func TestTheUpgradeScriptPollsAConditionInsteadOfSleeping(t *testing.T) {
	// `safe` is the whole point: units gone from that machine AND nothing left unwritten in a spool. A
	// recorder whose units have left promotes what they closed on its next pump — and only then may the
	// power go. Three lines and no `sleep`: POST, poll until safe, stop the machine.
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars.AsWriter("vmscontroller", vms.Spec.ACLController()...), box.Objects, 0, box.Wall.Now)
	conVars := box.Vars.AsWriter("console", append(vms.Spec.ACLConsole(), vms.RecSpec.ACLConsole()...)...)
	con := vms.NewVmsController(conVars, box.Objects, 0, box.Wall.Now)
	recCon := p.NewSpecController(vms.RecSpec, conVars, box.Objects, 0, box.Wall.Now, "")
	recCtl := p.NewSpecController(vms.RecSpec, box.Vars.AsWriter("reccontroller", vms.RecSpec.ACLController()...), box.Objects, 0, box.Wall.Now, "")

	w := worker(t, box, "w-1", vms.NewFakeActuator(), vms.VmsWorkerOptions{Server: "srv-1", ArchiveRoot: box.Archive})
	w.HeartbeatOnce()
	mustCreate(t, con, map[string]any{"name": "gate", "source": "driverpack://file/gate.mp4"})
	ctl.EnsurePlaced(nil)
	w.ReconcileOnce()
	w.HeartbeatOnce()
	if _, err := recCon.Create(map[string]any{"cam": "1"}); err != nil {
		t.Fatal(err)
	}
	arch := vms.NewArchiveResource(box.Spool, box.Archive, 600, box.Wall.Now)
	r := recorder(t, box, "r-1", "srv-1", 50)
	recorder(t, box, "r-2", "srv-2", 50) // somewhere for the work to go
	recCtl.EnsurePlaced(nil)
	r.ReconcileOnce()
	r.HeartbeatOnce()
	if recCtl.Where("1") != "r-1" {
		t.Fatal(recCtl.Where("1"))
	}
	m := vms.NewConsole(con, arch, box.Wall.Now, recCon) // the console's token: it may say "draining"

	_, rep := m.DrainRoute("GET", nil)
	if rep["draining"] != "" || rep["safe"] != true {
		t.Fatal(rep)
	}
	status, rep := m.DrainRoute("POST", map[string]string{"server": "srv-1"})
	if status != 200 || rep["draining"] != "srv-1" || rep["safe"] != false {
		t.Fatal(status, rep)
	}
	subs := rep["subsystems"].(map[string]any)
	if subs["rec"].(map[string]any)["units"] != 1 { // the recorder still carries it
		t.Fatal(subs["rec"])
	}
	// and the camera has nowhere to go at all: one vms worker in the box, and it is on this machine. An
	// upgrade script stops HERE, rather than after the reboot.
	strand := rep["would_strand"].(map[string]any)
	if ws, ok := strand["vms"].([]string); !ok || len(ws) != 1 || ws[0] != "1" {
		t.Fatal(strand)
	}
	worker(t, box, "w-2", vms.NewFakeActuator(), vms.VmsWorkerOptions{Server: "srv-2", ArchiveRoot: box.Archive}).HeartbeatOnce()
	if _, rep = m.DrainRoute("GET", nil); len(rep["would_strand"].(map[string]any)) != 0 {
		t.Fatal(rep["would_strand"]) // give it somewhere, and the dry run goes quiet
	}

	// a spool file nobody promoted yet: not safe, whatever the assignments say
	now := box.Wall.Now()
	pth := vms.SegmentPath(box.Spool, "1", r.Epochs["1"], time.Unix(int64(now-60), 0).UTC())
	os.MkdirAll(filepath.Dir(pth), 0o755)
	if err := os.WriteFile(pth, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	touch(pth, now-30) // the box's clock, not the machine's
	r.HeartbeatOnce()
	if _, rep = m.DrainRoute("GET", nil); rep["subsystems"].(map[string]any)["rec"].(map[string]any)["spool"] != 1 {
		t.Fatal(rep["subsystems"])
	}

	if moves := recCtl.Redistribute(nil); len(moves) != 1 || moves[0].To != "r-2" { // the work leaves, orderly
		t.Fatal(moves)
	}
	if moves := ctl.Redistribute(nil); len(moves) != 1 || moves[0].To != "w-2" {
		t.Fatal(moves)
	}
	r.HeartbeatOnce()
	_, rep = m.DrainRoute("GET", nil)
	mid := rep["subsystems"].(map[string]any)["rec"].(map[string]any)
	if mid["units"] != 0 || mid["spool"] != 1 || mid["safe"] != false {
		t.Fatal(mid) // nothing assigned here any more — and still not safe: a segment is unwritten
	}
	r.ReconcileOnce()
	r.PumpOnce() // …and the pump promotes what was closed
	r.HeartbeatOnce()
	_, rep = m.DrainRoute("GET", nil)
	subs = rep["subsystems"].(map[string]any)
	if subs["rec"].(map[string]any)["spool"] != 0 || subs["rec"].(map[string]any)["units"] != 0 {
		t.Fatal(subs["rec"])
	}
	if subs["vms"].(map[string]any)["units"] != 0 || rep["safe"] != true {
		t.Fatal(rep) // now the power may go
	}

	if _, rep = m.DrainRoute("DELETE", nil); rep["draining"] != "" || rep["safe"] != true {
		t.Fatal(rep)
	}
}
