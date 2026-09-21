package vms

// The console's side of a subsystem whose work ENDS.
//
// A worker is the only participant that can know a job is finished: the plan and the progress are files on
// the server it ran on, and its ACL — [<name>/epoch/*, <name>/slots/*] — forbids it to write the row. A
// controller could write it (its grant is <name>/*), but then one row has two writers and they collide
// exactly when the operator edits a job the controller is finishing.
//
// So the console does it, in a pass of its own beside the blob sweep: it reads the heartbeats it already
// reads, and moves the row's `state` when the worker holding the job says the work is over. One writer per
// row, and the operator sees `done` in the row they created.
//
// Why the state must be DURABLE and not simply read off the heartbeat, now that the placement predicate
// lands: un-placing a finished job makes its worker drop it, which makes the next heartbeat stop
// mentioning it, which makes the evidence of `done` disappear — and the job is placed again, and scans the
// archive from the top, for ever. A finished job has to be finished somewhere that does not depend on its
// still being assigned.

import (
	"log"
	"sort"
	"strconv"
	"strings"

	p "vmsserver/w2cplatform"
)

// MIRRORED: what a job's row may say, and what the worker's phase is allowed to move it to. The console
// mirrors the phase into the row not because the row is a better heartbeat — it is a worse one — but
// because the row is what SURVIVES the job being un-placed, and what the operator opened. `waiting` and
// `unsupported` are the worker's news and stay in the heartbeat: nothing downstream acts on them.
var MIRRORED = map[string]bool{"fetching": true, "running": true, "done": true, "failed": true}

// Reap: one pass. Returns how many rows it moved into each terminal state.
//
// Only the worker the CONTROLLER placed the job on is believed. A heartbeat object outlives its worker, so
// a slot that finished this job yesterday, before it was moved to another server, still says `done` in the
// store; taking that at face value would end a scan that is running right now, somewhere else, from the
// beginning.
func Reap(ctl *p.SpecController, lostAfter float64) map[string]int {
	moved := map[string]int{"done": 0, "failed": 0}
	placed := map[string]string{}
	state := map[string]string{}
	for _, row := range ctl.Units() {
		uid := p.Str(row["id"])
		if terminal[p.Str(row["state"])] {
			continue // already moved; the predicate un-places it, not us
		}
		state[uid] = p.Str(row["state"])
		if pl := ctl.Placement(uid); pl != nil {
			placed[uid] = pl.Worker
		}
	}
	for _, st := range ctl.ReadModel(lostAfter) {
		uid, phase := p.Str(st["id"]), p.Str(st["phase"])
		if !MIRRORED[phase] || placed[uid] != p.Str(st["worker"]) {
			continue
		}
		if state[uid] == phase {
			continue // already says it: a row that moves every pass is a revision that moves every pass,
		} // for every reader downstream
		if _, err := ctl.Update(uid, p.Row{"state": phase}); err != nil {
			log.Printf("%s %s: could not move the row to %s: %v", ctl.Spec.Name, uid, phase, err)
			continue
		}
		if terminal[phase] {
			moved[phase]++
			log.Printf("%s %s: %s (%.0f s of footage, %d event(s))", ctl.Spec.Name, uid, phase,
				p.ToFloat(st["covered"]), int(p.ToFloat(st["events"])))
		}
	}
	return moved
}

// heartbeatsOf: a subsystem's heartbeats in a stable order — the passes below write rows, and two consoles
// running the same pass should write them in the same order.
func heartbeatsOf(ctl *p.SpecController) []p.Heartbeat {
	all := p.Heartbeats(ctl.Objects, ctl.Spec.Name+"/")
	names := make([]string, 0, len(all))
	for n := range all {
		names = append(names, n)
	}
	sort.Strings(names)
	out := make([]p.Heartbeat, 0, len(names))
	for _, n := range names {
		out = append(out, all[n])
	}
	return out
}

// spansIn: `<unit>|<from>|<to>,…` — the shape the recorder's `closed` and the survey's `hits` share.
func spansIn(s string) (out []struct {
	Unit   string
	T0, T1 float64
}) {
	for _, span := range strings.Split(s, ",") {
		parts := strings.Split(span, "|")
		if len(parts) != 3 {
			continue
		}
		t0, e0 := strconv.ParseFloat(parts[1], 64)
		t1, e1 := strconv.ParseFloat(parts[2], 64)
		if e0 != nil || e1 != nil || t1 <= t0 {
			continue
		}
		out = append(out, struct {
			Unit   string
			T0, T1 float64
		}{parts[0], t0, t1})
	}
	return out
}

// rangeID: the request's id IS the range, so a pass every thirty seconds writes one row, not a queue.
func rangeID(unit string, t0, t1 float64) string {
	return unit + "-" + strconv.FormatInt(int64(t0), 10) + "-" + strconv.FormatInt(int64(t1), 10)
}

// ClearRequests: the other half of `<name>/requests/<id>`. The worker fetched it and said so in its
// heartbeat; the row goes.
//
// Same division as Reap, and for the same reason — a worker writes no configuration. Cheaper still, because
// a request has no state to move: once the work named in it is done the row has nothing left to say, and a
// store that keeps every range anyone ever asked for is a store that grows without anybody deciding it
// should.
func ClearRequests(ctl *p.SpecController) int {
	fetched := map[string]bool{}
	for _, hb := range heartbeatsOf(ctl) {
		for _, r := range strings.Split(hb.ExtraString("fetched", ""), ",") {
			if r != "" {
				fetched[r] = true
			}
		}
	}
	gone := 0
	keys, _ := ctl.Vars.List(ctl.Sub.RequestsPrefix())
	for _, key := range keys {
		if fetched[key[strings.LastIndex(key, "/")+1:]] {
			if err := ctl.Vars.Delete(key, p.NoCAS); err == nil {
				gone++
			}
		}
	}
	return gone
}

// askRecorder writes one request row unless it is there already — the recorder's own heartbeat clears it
// once it is fetched, and until then a second pass has nothing to add. Create-only, in the store: two
// consoles running the same pass race for one row and one of them loses, which is the right answer.
func askRecorder(rec *p.SpecController, unit, cam string, t0, t1, at float64, by string) bool {
	_, err := rec.Vars.Put(rec.Sub.RequestKey(rangeID(unit, t0, t1)), p.Items{"unit": unit, "cam": cam,
		"from": ftoa(t0), "to": ftoa(t1), "at": ftoa(at), "by": by}, p.Absent)
	return err == nil // an error here is almost always "already asked"; the recorder clears it when it is fetched
}

// AskForFootage: a job that cannot run because the footage is still on the device — ask the recorder for it.
//
// The scan does NOT read the device itself, and that is the design and not a shortcut. The playback door
// admits two sessions per device (М10B Lesson 15), and those two belong to the operator watching the gap and
// to the recorder saving it; a scan is exactly the greedy third. Worse, a card keeps three days: a search that
// races the device's own retention can find a car whose evidence is gone by the time anyone clicks. So the
// range is fetched ONCE, into our archive, with our epoch and our retention — and the scan then runs over
// footage we own, with scan.go unchanged.
func AskForFootage(jobCtl, recCtl *p.SpecController, lostAfter float64) int {
	asked := 0
	for _, st := range jobCtl.ReadModel(lostAfter) {
		if p.Str(st["phase"]) != "fetching" {
			continue
		}
		unit, t0, t1 := p.Str(st["rec"]), p.ToFloat(st["from"]), p.ToFloat(st["to"])
		if st["rec"] == nil || unit == "" || t1 <= t0 {
			continue
		}
		cam := p.Str(st["cam"])
		if st["cam"] == nil || cam == "" {
			cam = unit
		}
		if askRecorder(recCtl, unit, cam, t0, t1, jobCtl.Wall(), jobCtl.Spec.Name+"/"+p.Str(st["id"])) {
			asked++
			log.Printf("%s %s: asking the recorder for %s [%.0f, %.0f)", jobCtl.Spec.Name, p.Str(st["id"]), unit, t0, t1)
		}
	}
	return asked
}

// ScanWhatArrived: what the recorder just fetched from a device is a hole in the DETECTIONS too — nothing was
// watching the camera while nothing was recording it. This closes the second hole with the first.
//
// One scan per (range × detector), because "no hole" means every model that runs live on that camera also
// ran over those minutes — and with the SAME settings, so `params` and `mask` are copied from the detector's
// own row rather than re-entered.
//
// The id is the range and the detector, so the pass is idempotent by construction: the recorder keeps
// reporting a range for as long as it stays in its window, and the second pass finds the row already there.
// A row an operator DELETED stays deleted — the marker outlives the row, and Create is create-only.
func ScanWhatArrived(recCtl, detCtl, jobCtl *p.SpecController) int {
	made := 0
	for _, hb := range heartbeatsOf(recCtl) {
		for _, sp := range spansIn(hb.ExtraString("closed", "")) {
			rec := recCtl.Unit(sp.Unit)
			if rec == nil {
				continue
			}
			cam := p.Str(rec["cam"])
			if rec["cam"] == nil || cam == "" {
				cam = sp.Unit
			}
			for _, d := range detCtl.Units() {
				if p.Str(d["cam"]) != cam || !d.Bool("enabled") {
					continue
				}
				jid := sp.Unit + "-" + p.Str(d["kind"]) + "-" + strconv.FormatInt(int64(sp.T0), 10) + "-" + strconv.FormatInt(int64(sp.T1), 10)
				// Made already, or deleted on purpose. Create would refuse either — it is create-only, and a
				// deleted row's marker is still its key — so this is not the guard; it is what keeps the log
				// from saying "could not queue" for every deleted range, every thirty seconds.
				if it, _, _ := jobCtl.Vars.Get(jobCtl.Sub.Config(jobCtl.Spec.Rows, jid)); it != nil {
					continue
				}
				body := p.Row{"name": jid, "cam": cam, "rec": sp.Unit, "kind": p.Str(d["kind"]), "from": sp.T0, "to": sp.T1}
				for _, f := range []string{"params", "mask"} { // the same settings the live detector runs with
					if v := d[f]; v != nil && p.Str(v) != "" {
						body[f] = v
					}
				}
				if _, err := jobCtl.Create(body); err != nil {
					log.Printf("%s: could not queue %s: %v", jobCtl.Spec.Name, jid, err)
					continue
				}
				made++
				log.Printf("%s: scanning %s [%.0f, %.0f) with %s — the footage arrived from a device",
					jobCtl.Spec.Name, sp.Unit, sp.T0, sp.T1, p.Str(d["kind"]))
			}
		}
	}
	return made
}

// KeepWhatFired: the fourth way to use a device archive — watch everything, keep what a model liked.
//
// The survey reports the stretches; this turns them into the request the recorder already understands. The
// same division as everywhere here — the worker knows and may not write, the console writes — and the same
// reason the request's id is the range.
//
// What lands in rec/<cam>/ this way is ordinary footage with the ordinary retention, and that is the point
// rather than an omission: when only the interesting minutes are copied, everything on the server is
// interesting, and "evidence" needs no second archive and no second lifetime.
func KeepWhatFired(surveyCtl, recCtl *p.SpecController) int {
	asked := 0
	for _, hb := range heartbeatsOf(surveyCtl) {
		for _, sp := range spansIn(hb.ExtraString("hits", "")) {
			cam, unit := sp.Unit, ""
			for _, r := range recCtl.Units() {
				rc := p.Str(r["cam"])
				if r["cam"] == nil || rc == "" {
					rc = p.Str(r["id"])
				}
				if rc == cam {
					unit = p.Str(r["id"])
					break
				}
			}
			if unit == "" {
				continue // nothing on this server records that camera: nowhere to put it
			}
			if askRecorder(recCtl, unit, cam, sp.T0, sp.T1, surveyCtl.Wall(), surveyCtl.Spec.Name+"/"+cam) {
				asked++
				log.Printf("%s: keeping %s [%.0f, %.0f) — a model liked it", surveyCtl.Spec.Name, unit, sp.T0, sp.T1)
			}
		}
	}
	return asked
}
