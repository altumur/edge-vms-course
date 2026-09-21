package vms

// Reading an INTERVAL of the archive, and remembering how far you got.
//
//	<archive>/rec/<unit>/manifest.jsonl        what the recorder wrote: the media index
//	<archive>/detjob/<job>/manifest.jsonl      what a scan has processed: one line per stretch
//
// The live detector never needed this file. It reads a fan-out with no beginning and no end, and "where
// am I" is "now". A scan over recorded footage has both ends, so it needs the two things the live path
// never asked for: which segments cover `[from, to)`, and which of them are already behind it.
//
// Both answers come out of the manifest and nothing else — no Variables, no heartbeat, no call to another
// server. That matters for footage from last March: who currently holds the recording says nothing about
// who won the race for those minutes, and the archive may be read on a box where no recorder runs at all.

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"

	p "vmsserver/w2cplatform"
)

const (
	// ScanSub is the scan's own tree, beside rec/ and vms/ on the same resource.
	ScanSub = "detjob"
	// SurveySub is the standing survey's own tree, beside it.
	SurveySub = "survey"
)

// Scan is one stretch of one segment: the footage, and the part of it this scan asked for.
type Scan struct {
	Seg    Segment
	T0, T1 float64 // unix seconds; the stretch, not the segment — they differ at both ends
}

func (s Scan) Seconds() float64 {
	if d := s.T1 - s.T0; d > 0 {
		return d
	}
	return 0
}

// Key is this stretch's identity in the log. The PATH and the stretch, not the segment alone: a segment
// can appear twice with two stretches when a newer epoch owns the minutes between them, and a log keyed by
// path alone would call the second stretch done when only the first was.
func (s Scan) Key() string { return fmt.Sprintf("%s@%.3f-%.3f", s.Seg.Path, s.T0, s.T1) }

// Accepts: whether an event the model reports at `ts` belongs to this scan. Decoding starts at the
// segment's START — a segment opened at 10:00 must be decoded from 10:00 even when the operator asked from
// 10:05 — so the frames before T0 are seen, and what they produce is not what was asked for. Without this
// the answer to "what happened between 10:05 and 10:12" quietly contains 10:00.
func (s Scan) Accepts(ts float64) bool { return ts >= s.T0 && ts < s.T1 }

// Plan: what a scan of `[t0, t1)` on this recording has to read, in order.
func Plan(archiveRoot, unit string, t0, t1 float64) []Scan {
	return authoritative(NewManifest(archiveRoot, unit).Read(), t0, t1)
}

// authoritative: every stretch of `[t0, t1)` that footage covers, each given to the HIGHEST EPOCH that
// covers it.
//
// Two epochs overlap in the archive whenever a recorder was fenced with footage in flight: the zombie's
// segments and the survivor's describe the same minutes. Neither "read everything" nor "drop everything
// fenced" is right. Read both and those minutes are scanned twice, so the operator gets every car counted
// twice. Drop every segment belonging to an older epoch and the minutes BEFORE the takeover — which only
// the older epoch ever held, and which are perfectly good footage — disappear from the answer.
//
// So the unit of the decision is the stretch, not the segment: walk the boundaries, and give each
// elementary interval to the highest epoch present there. A segment can come back in two pieces, or in
// none. Adjacent pieces of the same segment are merged back so the caller opens the file once.
func authoritative(segs []Segment, t0, t1 float64) []Scan {
	edges := map[float64]bool{t0: true, t1: true}
	for _, s := range segs {
		edges[s.Start], edges[s.End] = true, true
	}
	cuts := make([]float64, 0, len(edges))
	for e := range edges {
		if e >= t0 && e <= t1 {
			cuts = append(cuts, e)
		}
	}
	sort.Float64s(cuts)

	out := []Scan{}
	for i := 0; i+1 < len(cuts); i++ {
		lo, hi := cuts[i], cuts[i+1]
		if hi <= lo {
			continue
		}
		best, found := Segment{}, false
		for _, s := range segs {
			if s.Start <= lo && s.End >= hi && (!found || s.Epoch > best.Epoch) {
				best, found = s, true
			}
		}
		if !found {
			continue // a gap: nothing was recorded here, and a scan cannot invent it
		}
		if n := len(out); n > 0 && out[n-1].Seg == best && out[n-1].T1 == lo {
			out[n-1].T1 = hi // the same writer either side of a boundary: one stretch
			continue
		}
		out = append(out, Scan{best, lo, hi})
	}
	return out
}

// Covered: seconds of `[t0, t1)` the plan actually covers. The operator asked for an hour; if forty
// minutes were recorded, a finished scan that found nothing has to be able to say WHICH — "nothing
// happened" and "nothing was recorded" are different answers and only one of them is about the footage.
func Covered(scans []Scan) float64 {
	total := 0.0
	for _, s := range scans {
		total += s.Seconds()
	}
	return total
}

// ScanLog is what a scan has already processed: `<archive>/detjob/<job>/manifest.jsonl`, one line per
// stretch, appended AFTER the stretch is scanned and its events are written. Durable, so a worker
// restarting on this server resumes instead of starting again; and on the resource rather than in a row,
// because a worker's ACL is `[<name>/epoch/*, <name>/slots/*]` — it may not write configuration.
type ScanLog struct{ Path string }

func NewScanLog(archiveRoot, job string) *ScanLog {
	return &ScanLog{filepath.Join(p.UnitDir(archiveRoot, ScanSub, job), "manifest.jsonl")}
}

func (l *ScanLog) Append(s Scan, events int, at float64) error {
	if err := os.MkdirAll(filepath.Dir(l.Path), 0o755); err != nil {
		return err
	}
	line, err := json.Marshal(map[string]any{"kind": "scan", "key": s.Key(), "path": s.Seg.Path,
		"epoch": s.Seg.Epoch, "from": s.T0, "to": s.T1, "events": events, "at": at})
	if err != nil {
		return err
	}
	f, err := os.OpenFile(l.Path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = f.Write(append(line, '\n'))
	return err
}

func (l *ScanLog) Read() []map[string]any {
	out := []map[string]any{}
	raw, err := os.ReadFile(l.Path)
	if err != nil {
		return out
	}
	for _, line := range splitLines(string(raw)) {
		var d map[string]any
		if json.Unmarshal([]byte(line), &d) == nil {
			out = append(out, d)
		}
	}
	return out
}

func (l *ScanLog) Done() map[string]bool {
	done := map[string]bool{}
	for _, d := range l.Read() {
		done[p.Str(d["key"])] = true
	}
	return done
}

// DoneThrough is for the operator's progress, and for nothing else. It is the far end of the furthest
// stretch recorded, which is NOT a point everything before is finished at: resuming from it would skip a
// stretch that failed while a later one succeeded. Remaining is the authoritative answer.
func (l *ScanLog) DoneThrough() float64 {
	out := 0.0
	for _, d := range l.Read() {
		if to := p.ToFloat(d["to"]); to > out {
			out = to
		}
	}
	return out
}

func (l *ScanLog) Events() int {
	n := 0
	for _, d := range l.Read() {
		n += int(p.ToFloat(d["events"]))
	}
	return n
}

// Remaining: the plan minus what the log says is behind us — what a restarted worker picks up.
func Remaining(scans []Scan, log *ScanLog) []Scan {
	done := log.Done()
	out := []Scan{}
	for _, s := range scans {
		if !done[s.Key()] {
			out = append(out, s)
		}
	}
	return out
}
