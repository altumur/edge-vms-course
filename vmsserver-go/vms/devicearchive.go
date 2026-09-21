package vms

// The OTHER archive: what a device holds, span by span — and the two shapes a caller turns it into.
//
// The summary in the holder's heartbeat (`from`, `to`, `fragments`) answers "is there anything there at
// all". It cannot answer "is there anything at 10:05", and for a device recording on motion the difference
// is most of the day: between its first and its last minute there is mostly nothing.

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"time"

	p "vmsserver/w2cplatform"
)

// ErrCannotList: the holder answered 501 — this driver cannot list. NOT "the device holds nothing": a
// caller that folds the two together promises a scan minutes that do not exist, or refuses one that does.
var ErrCannotList = errors.New("this driver cannot list what the device holds")

// Index reads a device's index through its holder's door. Tests hand in their own.
type Index func(url string, t0, t1 float64) ([][2]float64, error)

// DeviceRecordings is the Index over HTTP: GET <index_url>?from&to.
func DeviceRecordings(url string, t0, t1 float64) ([][2]float64, error) {
	c := http.Client{Timeout: 10 * time.Second}
	resp, err := c.Get(fmt.Sprintf("%s?from=%s&to=%s", url, ftoa(t0), ftoa(t1)))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == 501 {
		return nil, ErrCannotList // the summary is all there is
	}
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("%s: %s", url, resp.Status)
	}
	var body struct {
		Spans []struct{ From, To float64 } `json:"spans"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, err
	}
	out := make([][2]float64, 0, len(body.Spans))
	for _, s := range body.Spans {
		out = append(out, [2]float64{s.From, s.To})
	}
	return out, nil
}

// CoveredBy: the seconds of [t0, t1) a device actually holds. `listed=false` is the caller's "we cannot
// tell", and the honest fallback is the summary — optimistic, which is right, because being wrong the other
// way refuses work that would succeed.
func CoveredBy(spans [][2]float64, listed bool, t0, t1 float64) float64 {
	if !listed {
		return max(0, t1-t0)
	}
	n := 0.0
	for _, s := range spans {
		n += max(0, min(s[1], t1)-max(s[0], t0))
	}
	return n
}

// HitSpans: moments into stretches — what to keep when the reason for keeping it is that a model fired.
//
// An event is an instant and footage is an interval, so something has to turn one into the other, and the
// three numbers are all decisions rather than tuning. `pre` and `post` are what makes the clip watchable — a
// car crossing the line at 10:04:31 is useless as a one-second file and obvious as a twenty-second one.
// `join` is what keeps a busy minute from becoming three hundred two-second clips: hits closer together than
// that are one stretch, and the gap between them is cheaper to keep than to cut out.
//
// `watched` clips the result to what was actually looked at — padding runs off the end of a span otherwise,
// and asks for minutes the device never recorded.
func HitSpans(times []float64, watched [][2]float64, pre, post, join float64) [][2]float64 {
	if len(times) == 0 {
		return [][2]float64{}
	}
	raw := make([][2]float64, 0, len(times))
	for _, t := range times {
		raw = append(raw, [2]float64{t - pre, t + post})
	}
	sort.Slice(raw, func(i, j int) bool { return raw[i][0] < raw[j][0] || (raw[i][0] == raw[j][0] && raw[i][1] < raw[j][1]) })
	merged := [][2]float64{}
	for _, r := range raw {
		if n := len(merged); n > 0 && r[0]-merged[n-1][1] <= join {
			merged[n-1][1] = max(merged[n-1][1], r[1])
		} else {
			merged = append(merged, r)
		}
	}
	out := [][2]float64{}
	for _, m := range merged {
		for _, w := range watched {
			if lo, hi := max(m[0], w[0]), min(m[1], w[1]); hi > lo {
				out = append(out, [2]float64{lo, hi})
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i][0] < out[j][0] })
	return out
}

// Frontier: how far a standing survey has watched — one number, durable, beside its events.
//
// The same argument as ScanLog and the same place for the same reason — a worker may not write
// configuration — but a different shape, because the work is different. A scan has a plan and ticks
// stretches off it; a survey has no end, and what it keeps is a moving edge.
type Frontier struct{ Path string }

func NewFrontier(archiveRoot, unit string) *Frontier {
	return &Frontier{Path: filepath.Join(p.UnitDir(archiveRoot, SurveySub, unit), "frontier.json")}
}

// Read: ok=false — never started, or the file was lost: the row decides where to begin.
func (f *Frontier) Read() (float64, bool) {
	raw, err := os.ReadFile(f.Path)
	if err != nil {
		return 0, false
	}
	var v map[string]any
	if json.Unmarshal(raw, &v) != nil {
		return 0, false
	}
	t, has := v["watched_through"].(float64)
	return t, has
}

// Set: written after the events of that stretch, and atomically. A crash between the work and the number
// costs a re-watch; a half-written number would cost the frontier itself.
func (f *Frontier) Set(t float64) error {
	if err := os.MkdirAll(filepath.Dir(f.Path), 0o755); err != nil {
		return err
	}
	raw, _ := json.Marshal(map[string]any{"watched_through": t})
	tmp := f.Path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, f.Path)
}
