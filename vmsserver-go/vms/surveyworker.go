package vms

// The survey worker — the sixth subsystem's worker, and the only one whose input is an archive we do not own.
//
// It watches a camera's DEVICE archive — the card, the NVR — forever, and copies nothing. The video stays
// where it is; what comes out is events:
//
//	survey/watches/<name>                             the unit: cam, kind, params — written by the console
//	<archive>/survey/<name>/e<epoch>/…events.jsonl    what the model saw
//	<archive>/survey/<name>/frontier.json             how far it has watched
//
// Three things shape it, and all three come from the archive being somebody else's.
//
// The INDEX says where to look. A device recording on motion has mostly nothing between its first and last
// minute, and walking that as if it were continuous would spend the whole budget on silence.
//
// The DOOR is the limit, not the GPU. A device allows two playback sessions and they are shared with the
// operator watching and the recorder saving; when they are gone this worker waits and says so, rather than
// retrying into a wall.
//
// And the frontier MOVES ON. It is not a plan to finish but an edge to keep up with — so what matters is not
// "did we cover everything" but how far behind the device's newest minute we are, which is what `lag` in the
// heartbeat says.

import (
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	p "vmsserver/w2cplatform"
)

// SURVEY is the subsystem's name — the keys its slot, epoch, assignment and heartbeat live under.
var SURVEY = SurveySpec.Sub()

// Fetch is one read through the holder's door: what costs a session. ErrDeviceBusy when both are in use.
type Fetch func(url string, t0, t1 float64) error

// FetchBytes: the door over HTTP. The bytes are not kept: this subsystem copies nothing, and what it took
// the session for is the decoding, not the file.
func FetchBytes(url string, t0, t1 float64) error {
	if url == "" {
		return fmt.Errorf("no playback url: %w", ErrDeviceBusy)
	}
	c := http.Client{Timeout: 30 * time.Second}
	resp, err := c.Get(url + "?from=" + ftoa(t0) + "&to=" + ftoa(t1))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	if resp.StatusCode == 503 {
		return fmt.Errorf("%s: %w", url, ErrDeviceBusy) // the device's ceiling, not ours
	}
	if resp.StatusCode != 200 {
		return fmt.Errorf("%s: %s", url, resp.Status)
	}
	return nil
}

type SurveyWorker struct {
	*p.Worker
	Models      map[string]func(p.Row) Model
	Capacity    int
	Server      string
	Labels      []string
	ArchiveRoot string
	// SecondsPerPass: seconds of MEDIA one pass may consume per watch. The same argument as the scan's budget
	// of stretches: a pass that ran until it caught up is a worker that stops heartbeating while it does.
	SecondsPerPass float64
	Step           float64 // seconds of media between two looks
	// Settle: never closer to the device's newest minute than this — it is still being written.
	Settle float64
	Fetch  Fetch // the door: reading, and what costs a session
	Index  Index // the listing: where the footage is, and it costs none

	// Hits: `<cam>|<from>|<to>` a model liked — the console asks the recorder. The same window as the
	// recorder's `closed`, and for the same reason: a heartbeat is one object under a ceiling, and the
	// console's decision is idempotent on its own because the request's id is the range.
	Hits          []string
	EventsWritten int

	running      map[string]Model
	statusByUnit map[string]map[string]any
}

// HitsReported: how many kept stretches the heartbeat carries.
const HitsReported = 32

type SurveyOptions struct {
	Models         map[string]func(p.Row) Model
	Capacity       int
	Server         string
	Labels         []string
	ArchiveRoot    string
	SecondsPerPass float64
	Step           float64
	Fetch          Fetch
	Index          Index
	Worker         p.WorkerOptions
}

func NewSurveyWorker(name string, vars p.Variables, objects p.ObjectStore, o SurveyOptions) (*SurveyWorker, error) {
	wo := o.Worker
	wo.Name = ""
	w := p.NewWorker(SURVEY, vars, objects, wo)
	if _, err := w.ClaimSlot(slotOr(name, "s")); err != nil {
		return nil, err
	}
	s := &SurveyWorker{Worker: w, Models: o.Models, Capacity: o.Capacity, Server: o.Server, Labels: o.Labels,
		ArchiveRoot: o.ArchiveRoot, SecondsPerPass: o.SecondsPerPass, Step: o.Step, Settle: 30,
		Fetch: o.Fetch, Index: o.Index, running: map[string]Model{}, statusByUnit: map[string]map[string]any{}}
	if s.Models == nil {
		s.Models = map[string]func(p.Row) Model{"motion": NewFakeModel, "linecross": NewFakeModel, "lpr": NewFakeModel}
	}
	if s.Capacity == 0 {
		s.Capacity = 2
	}
	if s.Labels == nil {
		s.Labels = []string{"gpu"}
	}
	if s.ArchiveRoot == "" {
		s.ArchiveRoot = "/data/archive"
	}
	if s.Server == "" {
		s.Server, _ = os.Hostname()
	}
	if s.SecondsPerPass == 0 {
		s.SecondsPerPass = 600
	}
	if s.Step == 0 {
		s.Step = 1
	}
	if s.Fetch == nil {
		s.Fetch = FetchBytes
	}
	if s.Index == nil {
		s.Index = DeviceRecordings
	}
	return s, nil
}

func (s *SurveyWorker) watchRow(unit string) p.Row {
	items, _, _ := s.Vars.Get(SURVEY.Config("watches", unit))
	if items == nil || items["deleted"] == "true" {
		return nil
	}
	return SurveySpec.RowOf(items)
}

// device: where the device's footage is and where to read it — (index_url, playback_url, oldest, newest),
// all from the HOLDER's heartbeat, which is how everything here is found.
func (s *SurveyWorker) device(cam string) (indexURL, playURL string, oldest, newest float64, ok bool) {
	h, found := p.HolderOf(s.Objects, "vms/", cam, s.Wall(), p.HolderQuery{Field: "coverage"})
	if !found {
		return "", "", 0, 0, false
	}
	cov, isMap := h.Status["coverage"].(map[string]any)
	idx, _ := h.Status["index_url"].(string)
	if !isMap || idx == "" {
		return "", "", 0, 0, false
	}
	play, _ := h.Status["playback_url"].(string)
	return idx, play, p.ToFloat(cov["from"]), p.ToFloat(cov["to"]), true
}

// ReconcileOnce: advance every watch by at most a budget of media seconds.
func (s *SurveyWorker) ReconcileOnce() []string {
	wanted := map[string]bool{}
	units := append([]string{}, s.Assignment().Units...)
	sort.Strings(units)
	for _, unit := range units {
		wanted[unit] = true
		row := s.watchRow(unit)
		if row == nil {
			continue
		}
		if !row.Bool("enabled") {
			s.stop(unit)
			s.statusByUnit[unit] = s.status(unit, row, "pending", "", nil, 0)
			continue
		}
		factory, known := s.Models[p.Str(row["kind"])]
		if !known {
			s.statusByUnit[unit] = s.status(unit, row, "unsupported", "", nil, 0)
			continue
		}
		cam := p.Str(row["cam"])
		indexURL, playURL, oldest, newest, ok := s.device(cam)
		if !ok {
			s.stop(unit)
			s.statusByUnit[unit] = s.status(unit, row, "waiting", "nobody holds this camera, or its device has no archive", nil, 0)
			continue
		}
		front := NewFrontier(s.ArchiveRoot, unit)
		at, started := front.Read()
		if !started {
			// The first time. `earliest` means thirty days of backlog on the day somebody enables it, which
			// is a decision the row makes and not a default this code picks.
			at = newest
			if p.Str(row["start"]) == "earliest" {
				at = oldest
			}
		}
		edge := newest - s.Settle // the last minutes are being written; reading them gets a torn end
		if at >= edge {
			s.statusByUnit[unit] = s.status(unit, row, "running", "", front, newest)
			continue
		}
		want := [2]float64{at, min(at+s.SecondsPerPass, edge)}
		spans, err := s.Index(indexURL, want[0], want[1])
		switch {
		case errors.Is(err, ErrCannotList):
			spans = [][2]float64{want} // the summary is all there is: watch the window as if it were continuous
		case err != nil:
			s.statusByUnit[unit] = s.status(unit, row, "waiting", "the holder is not answering", front, newest)
			continue
		}

		if _, have := s.Epochs[unit]; !have {
			if _, err := s.TakeEpoch(unit); err != nil { // one writer of survey/<unit>/… at a time
				continue
			}
		}
		model, up := s.running[unit]
		if !up {
			model = factory(row)
			s.running[unit] = model
		}
		if !s.MayWrite(unit) {
			continue
		}

		var busy error
		var fired []float64
		var watched [][2]float64
		camN, _ := strconv.Atoi(cam)
		for _, sp := range spans {
			if err := s.Fetch(playURL, sp[0], sp[1]); err != nil { // the door: this is what costs a session
				busy = err // either way nothing past here was watched, and the frontier must not claim it
				break
			}
			watched = append(watched, sp)
			for ts := sp[0]; ts < sp[1]; ts += s.Step {
				for _, obs := range model.Observe(ts) {
					fields := map[string]any{"cam": camN, "watch": unit, "source": "device"}
					for k, v := range obs.Fields {
						if k != "cam" {
							fields[k] = v
						}
					}
					if _, err := p.NewEventLog(s.ArchiveRoot, SURVEY.Name, unit, s.Epochs[unit], 0).Append(ts, obs.Kind, fields); err != nil {
						continue
					}
					s.EventsWritten++
					fired = append(fired, ts)
				}
			}
		}
		// The frontier moves over the whole window, not from span to span. A gap in the device's own recording
		// is nothing to watch and nothing to come back for — leaving the frontier at its edge would park the
		// survey in front of every quiet night for ever. When the door closed half way, it moves to the end
		// of what WAS watched: those events are written, and watching them again would write them twice.
		through, moved := want[1], true
		if busy != nil {
			moved = len(watched) > 0
			if moved {
				through = watched[len(watched)-1][1]
			}
		}
		if moved {
			front.Set(through)
			if p.Str(row["keep"]) == "hits" && len(fired) > 0 {
				// Watch everything, copy what a model liked. The stretches are reported, not written: a
				// worker's token writes no configuration, and the row that asks the recorder for a range is
				// configuration (`rec/requests/<id>`). The console turns these into requests.
				for _, h := range HitSpans(fired, watched, p.ToFloat(row["pre"]), p.ToFloat(row["post"]), p.ToFloat(row["join"])) {
					s.Hits = tail(append(s.Hits, cam+"|"+strconv.FormatFloat(h[0], 'f', 0, 64)+"|"+strconv.FormatFloat(h[1], 'f', 0, 64)), HitsReported)
				}
			}
		}
		if busy != nil {
			// Not a failure and not a retry: the two sessions belong to the operator watching this gap and to
			// the recorder saving it, and a survey is the one of the three that can wait.
			why := "the device has no free session"
			if !errors.Is(busy, ErrDeviceBusy) {
				why = "the holder is not answering: " + busy.Error()
			}
			s.statusByUnit[unit] = s.status(unit, row, "waiting", why, front, newest)
			continue
		}
		s.statusByUnit[unit] = s.status(unit, row, "running", "", front, newest)
	}
	for unit := range s.running {
		if !wanted[unit] {
			s.stop(unit)
			s.Release(unit)
		}
	}
	for unit := range s.statusByUnit {
		if !wanted[unit] {
			delete(s.statusByUnit, unit)
		}
	}
	s.RenewLeases()
	out := make([]string, 0, len(s.running))
	for u := range s.running {
		out = append(out, u)
	}
	sort.Strings(out)
	return out
}

// status: `lag` is the answer this subsystem exists to give about itself — how far behind the device's
// newest minute we are. A survey that cannot keep up is not broken and not finished: it is behind, and the
// only way anyone finds out is if it says so.
func (s *SurveyWorker) status(unit string, row p.Row, phase, why string, front *Frontier, newest float64) map[string]any {
	st := map[string]any{"id": unit, "cam": p.Str(row["cam"]), "kind": p.Str(row["kind"]), "phase": phase}
	if why != "" {
		st["why"] = why
	}
	if front != nil {
		at, ok := front.Read()
		st["watched_through"] = at
		if ok {
			st["lag"] = max(0, newest-at)
		}
	}
	st["events"] = s.EventsWritten
	return st
}

func (s *SurveyWorker) stop(unit string) {
	if m, ok := s.running[unit]; ok {
		m.Close()
		delete(s.running, unit)
	}
}

func (s *SurveyWorker) Headroom() int {
	if n := s.Capacity - len(s.running); n > 0 {
		return n
	}
	return 0
}

func (s *SurveyWorker) HeartbeatOnce() error {
	names := make([]string, 0, len(s.statusByUnit))
	for u := range s.statusByUnit {
		names = append(names, u)
	}
	sort.Strings(names)
	status := make([]map[string]any, 0, len(names))
	for _, u := range names {
		status = append(status, s.statusByUnit[u])
	}
	return s.HeartbeatWith(status, map[string]any{
		"server": s.Server, "instance": s.Instance, "labels": strings.Join(s.Labels, ","),
		"capacity": s.Capacity, "headroom": s.Headroom(), "conflicts": s.Conflicts(), "events": s.EventsWritten,
		"hits": strings.Join(s.Hits, ","),
	})
}

// Status is what the tests read: the per-unit entry this worker would publish.
func (s *SurveyWorker) Status(unit string) map[string]any { return s.statusByUnit[unit] }
