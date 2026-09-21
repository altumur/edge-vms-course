package vms

// Backfill — closing OUR gaps from the device's own archive (Lesson 16).
//
// The card exists because the camera kept recording while we could not, so
// replication is not "copy everything" — it is the DIFFERENCE between two
// coverages. Desired: continuous. Actual: our manifest. The difference is the
// work. That is Lesson 2's reconcile loop, run over time instead of over
// pipelines, and it is why nothing here keeps a queue or a cursor: a crash in
// the middle costs the pass, never the plan.

import (
	"os"
	"strconv"
	"strings"
	"time"

	p "vmsserver/w2cplatform"
)

// RangeRecorder is the actuator's second verb: fetch a range through a
// playback door and write it into the spool as ordinary segments, under our
// epoch. An actuator that cannot do it simply does not implement this.
type RangeRecorder interface {
	RecordRange(unit, url string, epoch int, t0, t1 float64, spool string) []string
}

// OurCoverage: what this archive already holds for the camera, seams under
// Stitch closed over.
func (r *RecWorker) OurCoverage(unit string) [][2]float64 {
	return r.Archive.Coverage(unit, r.Stitch)
}

// Gaps: what the device has and we do not, bounded at BOTH ends. Not older than
// our own retention — otherwise backfill and retention chase each other round
// the clock, for ever. Not fresher than Settle — the last minutes are being
// written right now, are in no manifest yet, and we would be fetching what we
// are recording.
func (r *RecWorker) Gaps(unit string, cov Coverage, now float64) [][2]float64 {
	from, to := cov.From, cov.To
	if lower := now - r.KeepDays*86400; from < lower {
		from = lower
	}
	if upper := now - r.Settle; to > upper {
		to = upper
	}
	if to <= from {
		return [][2]float64{}
	}
	return Subtract([2]float64{from, to}, r.OurCoverage(unit))
}

// InWindow: local time, and the one place in the course where that is right —
// "at night" is night where the CAMERA is, not where the server is. {22, 6}
// wraps midnight; without that branch it would never arrive.
func (r *RecWorker) InWindow(now float64) bool {
	a, b := r.Window[0], r.Window[1]
	if a == b {
		return true // no window: any hour
	}
	h := time.Unix(int64(now), 0).Hour()
	if a < b {
		return h >= a && h < b
	}
	return h >= a || h < b
}

// DeviceSource: (playback door, coverage summary) for a recording whose camera
// is held by a worker that serves the device's own archive — found the way
// everything is found here, in the holder's heartbeat. No phase is asked for: a
// channel held only for its archive (`held`) answers too.
func (r *RecWorker) DeviceSource(cam string) (url string, cov Coverage, ok bool) {
	h, found := p.HolderOf(r.Objects, "vms/", cam, r.Wall(), p.HolderQuery{Field: "playback_url"})
	if !found {
		return "", Coverage{}, false
	}
	raw, isMap := h.Status["coverage"].(map[string]any)
	if !isMap {
		return "", Coverage{}, false
	}
	return p.Str(h.Status["playback_url"]),
		Coverage{From: p.ToFloat(raw["from"]), To: p.ToFloat(raw["to"]), Fragments: int(p.ToFloat(raw["fragments"]))},
		true
}

// Filled is what one fetched range came to.
type Filled struct {
	Unit     string  `json:"unit"`
	Cam      string  `json:"cam"`
	From     float64 `json:"from"`
	To       float64 `json:"to"`
	Segments int     `json:"segments"`
	Skipped  string  `json:"skipped,omitempty"`
}

// Backfill: bounded work, and by default only on request — never in the
// ordinary pass, the way rebalance(budget) is bounded (Lesson 13). Backfill
// competes with live for the device's uplink, so it gets a ceiling and an hour.
func (r *RecWorker) Backfill(budget int, now float64, force bool) []Filled {
	if budget <= 0 {
		budget = 1
	}
	if now == 0 {
		now = r.Wall()
	}
	if !force && !r.InWindow(now) {
		return []Filled{}
	}
	if r.UnderPressure() {
		return []Filled{}
	}
	done := []Filled{}
	for _, row := range r.Rows {
		if len(done) >= budget {
			break
		}
		url, cov, ok := r.DeviceSource(row.Cam) // the DEVICE is the camera's
		if !ok {
			continue
		}
		for _, g := range r.Gaps(row.ID, cov, now) { // the GAPS are this recording's
			if len(done) >= budget {
				break
			}
			done = append(done, r.Fetch(row.ID, row.Cam, url, g[0], g[1]))
		}
	}
	return done
}

// Fetch: one range — fetch it, and promote what came back as OURS: source
// "edge", our epoch, our manifest, our retention. The overlap is checked a
// SECOND time here because live recording may have reached the same minutes
// while we were fetching; a segment that would land on top of one we already
// have is dropped rather than written.
func (r *RecWorker) Fetch(unit, cam, url string, t0, t1 float64) Filled {
	if !r.MayWrite(unit) {
		return Filled{Unit: unit, Cam: cam, From: t0, To: t1, Skipped: "no lease"}
	}
	rec, ok := r.Act.(RangeRecorder)
	if !ok {
		return Filled{Unit: unit, Cam: cam, From: t0, To: t1, Skipped: "this actuator cannot fetch ranges"}
	}
	paths := rec.RecordRange(unit, url+"?from="+ftoa(t0)+"&to="+ftoa(t1), r.Epochs[unit], t0, t1, r.Archive.Spool)
	have, kept := r.OurCoverage(unit), 0
	for _, pth := range paths {
		span := [2]float64{t0, t1}
		if _, _, start, parsed := Parse(pth, r.Archive.Spool); parsed {
			end := t1
			if st, err := os.Stat(pth); err == nil {
				end = float64(st.ModTime().UnixNano()) / 1e9
			}
			span = [2]float64{float64(start.Unix()), end}
		}
		if Overlaps(have, span) { // live recording got there while we were fetching
			os.Remove(pth)
			continue
		}
		r.Archive.Promote(pth, 0, "edge")
		kept++
	}
	r.Backfilled += kept
	if kept > 0 {
		// What arrived is now ordinary footage — and a hole in the DETECTIONS, because nothing was watching
		// this camera while nothing was recording it. The console turns each of these into a scan (М10B
		// Lesson 22), so the two holes close together. Reported here and not written anywhere: a worker's
		// token writes no configuration. `kept == 0` is not reported: those minutes were already ours, and
		// already watched by the live detector.
		r.Closed = tail(append(r.Closed, unit+"|"+strconv.FormatFloat(t0, 'f', 0, 64)+"|"+strconv.FormatFloat(t1, 'f', 0, 64)), ClosedReported)
	}
	return Filled{Unit: unit, Cam: cam, From: t0, To: t1, Segments: kept}
}

// Requests: what an operator asked for — `rec/requests/<id>`, written by the console.
//
// The ordinary pass is bounded by a budget and an hour because backfill competes with live for the device's
// uplink. A range a PERSON asked for is different work: they are looking at that gap now, and the night is
// not a useful answer. So these are fetched outside both — but not outside UnderPressure, because a disk
// that is being emptied this minute cannot be given more.
//
// The request is not cleared here. A worker's token writes its slot and its epochs, never configuration
// (М10A Lesson 10), so the recorder REPORTS what it fetched in its heartbeat and the console removes the row
// — the same division as a scan that finishes (М10B Lesson 21).
func (r *RecWorker) Requests(budget int) []Filled {
	done := []Filled{}
	if r.UnderPressure() {
		return done
	}
	mine := map[string]bool{}
	for _, row := range r.Rows {
		mine[row.ID] = true
	}
	keys, _ := r.Vars.List(REC.RequestsPrefix())
	for _, key := range keys {
		if len(done) >= budget {
			break
		}
		it, _, _ := r.Vars.Get(key)
		if it == nil || !mine[it["unit"]] {
			continue // another recorder's recording: not ours to fetch — and its lease would refuse it one call later
		}
		cam := it["cam"]
		if cam == "" {
			cam = it["unit"]
		}
		url, _, ok := r.DeviceSource(cam)
		if !ok {
			continue // nobody holds the device right now; ask again next pass
		}
		t0, _ := strconv.ParseFloat(it["from"], 64)
		t1, _ := strconv.ParseFloat(it["to"], 64)
		f := r.Fetch(it["unit"], cam, url, t0, t1)
		if f.Skipped != "" {
			continue // not fetched — reporting it would have the console delete a request nobody served
		}
		r.Fetched = append(r.Fetched, key[strings.LastIndex(key, "/")+1:]) // the heartbeat says so; the console removes the row
		done = append(done, f)
	}
	return done
}

func ftoa(f float64) string { return strconv.FormatFloat(f, 'f', -1, 64) }

// UnderPressure: the disk is over its high mark, the resource is freeing space this minute, and backfill
// exists to bring more in. Without this the two chase each other for ever on a full disk — the same trap
// KeepDays closes in time, closed here in space. Not a force override either: an operator asking for a
// range cannot be given one the resource is about to delete.
func (r *RecWorker) UnderPressure() bool {
	knob := p.GetSpaceSettings(r.Vars)
	if !knob.Enabled {
		return false
	}
	total, free := r.SpaceProbe(r.Archive.Root)
	return total > 0 && float64(total-free) > float64(total)*knob.High
}
