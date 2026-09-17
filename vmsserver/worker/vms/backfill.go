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
	"time"

	p "vmsserver/worker/w2cplatform"
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
	return Filled{Unit: unit, Cam: cam, From: t0, To: t1, Segments: kept}
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
