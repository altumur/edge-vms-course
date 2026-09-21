package vms_test

// The survey: watching an archive we do not own, forever, and copying none of it. The index says where to
// look, the door says how often, and the frontier is an edge to keep up with rather than a plan to finish.

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/w2cplatform"
)

type surveyEvery struct{ row p.Row }

func (m *surveyEvery) Observe(now float64) []vms.Observation {
	return []vms.Observation{{Kind: p.Str(m.row["kind"]), Fields: map[string]any{"at": now}}}
}
func (m *surveyEvery) Close() {}

// A VMS worker holding the camera: the summary, and WHERE to ask for the index.
func deviceHolder(box *testbox.Box, newest float64, spans ...[2]float64) {
	box.Objects.Put("vms/heartbeats/w-1", p.Heartbeat{Worker: "w-1", Ts: box.Wall.Now(),
		Status: []map[string]any{{"id": "7", "phase": "running",
			"coverage":     map[string]any{"from": spans[0][0], "to": newest, "fragments": 9},
			"index_url":    "http://holder/recordings/7",
			"playback_url": "http://holder/playback/7"}},
		Extra: map[string]any{"server": "srv-1", "capacity": 50, "headroom": 49}}.ToBytes())
}

type door struct {
	reads  [][2]float64
	busyAt int // the n-th read (1-based) finds both sessions in use; 0: never
}

func surveyor(t *testing.T, box *testbox.Box, spans [][2]float64, d *door, perPass float64) *vms.SurveyWorker {
	t.Helper()
	w, err := vms.NewSurveyWorker("s-1", box.Vars.AsWriter("surveyworker", vms.SURVEY.ACLWorker()...), box.Objects,
		vms.SurveyOptions{Models: map[string]func(p.Row) vms.Model{"lpr": func(r p.Row) vms.Model { return &surveyEvery{r} }},
			Server: "srv-1", ArchiveRoot: box.Archive, Step: 60, SecondsPerPass: perPass,
			Fetch: func(url string, a, b float64) error {
				if d.busyAt > 0 && len(d.reads)+1 >= d.busyAt {
					return fmt.Errorf("both sessions in use: %w", vms.ErrDeviceBusy)
				}
				d.reads = append(d.reads, [2]float64{a, b})
				return nil
			},
			Index: func(url string, t0, t1 float64) ([][2]float64, error) {
				out := [][2]float64{}
				for _, s := range spans {
					if s[1] > t0 && s[0] < t1 {
						out = append(out, [2]float64{max(s[0], t0), min(s[1], t1)})
					}
				}
				return out, nil
			},
			Worker: p.WorkerOptions{Clock: box.Clock.Now, Wall: box.Wall.Now}})
	if err != nil {
		t.Fatal(err)
	}
	return w
}

func aWatch(t *testing.T, box *testbox.Box, start string, extra p.Row) *p.SpecController {
	t.Helper()
	ctl := p.NewSpecController(vms.SurveySpec, box.Vars.AsWriter("console", vms.SurveySpec.ACLConsole()...), box.Objects, 2, box.Wall.Now, "")
	row := p.Row{"name": "7-lpr", "cam": "7", "kind": "lpr", "start": start}
	for k, v := range extra {
		row[k] = v
	}
	if _, err := ctl.Create(row); err != nil {
		t.Fatal(err)
	}
	box.Vars.Put(vms.SURVEY.Assignment("s-1"), p.Items{"units": "7-lpr", "rev": "1"}, p.Absent)
	return ctl
}

func frontier(box *testbox.Box) (float64, bool) { return vms.NewFrontier(box.Archive, "7-lpr").Read() }

func surveyLines(t *testing.T, box *testbox.Box, epoch int) []p.Event {
	t.Helper()
	dir := filepath.Join(box.Archive, "survey", "7-lpr", "e"+itoa(epoch))
	entries, _ := os.ReadDir(dir)
	var out []p.Event
	for _, e := range entries {
		out = append(out, p.ReadBucket(filepath.Join(dir, e.Name()))...)
	}
	return out
}

var twoSpans = [][2]float64{{mm(0), mm(10)}, {mm(50), mm(60)}}

func TestItWatchesOnlyWhereTheDeviceActuallyRecorded(t *testing.T) {
	// Between the device's first and last minute there is mostly nothing. Walking that as if it were
	// continuous spends the whole budget on silence.
	box := testbox.NewBox()
	deviceHolder(box, mm(100), twoSpans...)
	aWatch(t, box, "earliest", nil)
	d := &door{}
	w := surveyor(t, box, twoSpans, d, mm(100)-mm(0))
	w.ReconcileOnce()
	eq(t, d.reads, twoSpans) // the two spans, and not the quiet hour between
}

func TestTheFrontierMovesOverTheWholeWindowAndNotSpanToSpan(t *testing.T) {
	// A gap in the device's own recording is nothing to watch and nothing to come back for. A frontier left
	// at the edge of the last span parks the survey in front of every quiet night for ever.
	box := testbox.NewBox()
	deviceHolder(box, mm(100), twoSpans...)
	aWatch(t, box, "earliest", nil)
	w := surveyor(t, box, twoSpans, &door{}, 1800)
	w.ReconcileOnce()
	if at, _ := frontier(box); at != mm(30) {
		t.Fatalf("the window, not the span: %v", at-mm(0))
	}
}

func TestANewWatchStartsNowUnlessTheRowSaysOtherwise(t *testing.T) {
	// `earliest` means thirty days of backlog on the day somebody enables it — a decision the row makes, not
	// a default this code picks.
	box := testbox.NewBox()
	deviceHolder(box, mm(100), twoSpans...)
	aWatch(t, box, "now", nil)
	d := &door{}
	w := surveyor(t, box, twoSpans, d, 1800)
	w.ReconcileOnce()
	if _, started := frontier(box); len(d.reads) != 0 || started {
		t.Fatal("a watch started `now` read the past")
	}
	if p.Str(w.Status("7-lpr")["phase"]) != "running" {
		t.Fatalf("%v", w.Status("7-lpr"))
	}
}

func TestTheEventsAreOursAndTheVideoIsNotCopied(t *testing.T) {
	box := testbox.NewBox()
	deviceHolder(box, mm(100), twoSpans...)
	aWatch(t, box, "earliest", nil)
	w := surveyor(t, box, twoSpans, &door{}, 1800)
	w.ReconcileOnce()
	lines := surveyLines(t, box, w.Epochs["7-lpr"])
	if len(lines) == 0 {
		t.Fatal("nothing was written")
	}
	for _, l := range lines {
		if p.ToFloat(l["cam"]) != 7 || p.Str(l["source"]) != "device" || p.Str(l["watch"]) != "7-lpr" || l.T() < mm(0) || l.T() >= mm(60) {
			t.Fatalf("%v", l)
		}
	}
	if _, err := os.Stat(filepath.Join(box.Archive, "rec")); err == nil {
		t.Fatal("something was copied")
	}
}

func TestARestartedWorkerDoesNotWatchItAgain(t *testing.T) {
	box := testbox.NewBox()
	deviceHolder(box, mm(100), twoSpans...)
	aWatch(t, box, "earliest", nil)
	surveyor(t, box, twoSpans, &door{}, 1800).ReconcileOnce()
	at, _ := frontier(box)

	d := &door{}
	surveyor(t, box, twoSpans, d, 1800).ReconcileOnce()
	eq(t, d.reads, [][2]float64{{mm(50), mm(60)}}) // only what is past the frontier
	if now, _ := frontier(box); now <= at {
		t.Fatal("the frontier did not move")
	}
}

func TestABusyDeviceIsAWaitAndTheFrontierDoesNotMove(t *testing.T) {
	// Two sessions, and they belong to the operator watching this gap and to the recorder saving it. A
	// survey is the one of the three that can wait.
	box := testbox.NewBox()
	deviceHolder(box, mm(100), twoSpans...)
	aWatch(t, box, "earliest", nil)
	w := surveyor(t, box, twoSpans, &door{busyAt: 1}, 1800)
	w.ReconcileOnce()
	st := w.Status("7-lpr")
	if p.Str(st["phase"]) != "waiting" || !strings.HasPrefix(p.Str(st["why"]), "the device has no free session") {
		t.Fatalf("%v", st)
	}
	if _, started := frontier(box); started {
		t.Fatal("nothing was watched, and something is claimed")
	}
}

func TestADoorThatClosedHalfWayKeepsWhatWasWatched(t *testing.T) {
	// The first span was watched and its events written; the second found both sessions taken. Leaving the
	// frontier where it was would watch the first span again next pass — and write every event in it twice.
	box := testbox.NewBox()
	deviceHolder(box, mm(100), twoSpans...)
	aWatch(t, box, "earliest", nil)
	w := surveyor(t, box, twoSpans, &door{busyAt: 2}, mm(100)-mm(0))
	w.ReconcileOnce()
	if p.Str(w.Status("7-lpr")["phase"]) != "waiting" {
		t.Fatalf("%v", w.Status("7-lpr"))
	}
	if at, _ := frontier(box); at != mm(10) {
		t.Fatalf("the frontier should stand at the end of what was watched: %v", at-mm(0))
	}
	first := len(surveyLines(t, box, w.Epochs["7-lpr"]))

	d := &door{}
	w.Fetch = func(url string, a, b float64) error { d.reads = append(d.reads, [2]float64{a, b}); return nil }
	w.ReconcileOnce()
	eq(t, d.reads, [][2]float64{{mm(50), mm(60)}})
	if n := len(surveyLines(t, box, w.Epochs["7-lpr"])); n != 2*first {
		t.Fatalf("%d events after both spans, %d after the first: the first was watched twice", n, first)
	}
}

func TestTheHeartbeatSaysHowFarBehindItIs(t *testing.T) {
	// A survey that cannot keep up is not broken and not finished. It is behind, and the only way anyone
	// finds out is if it says so.
	box := testbox.NewBox()
	deviceHolder(box, mm(100), twoSpans...)
	aWatch(t, box, "earliest", nil)
	w := surveyor(t, box, twoSpans, &door{}, 600)
	w.ReconcileOnce()
	st := w.Status("7-lpr")
	if p.ToFloat(st["watched_through"]) != mm(10) || p.ToFloat(st["lag"]) != mm(100)-mm(10) {
		t.Fatalf("%v", st)
	}
	w.ReconcileOnce()
	if p.ToFloat(w.Status("7-lpr")["lag"]) >= p.ToFloat(st["lag"]) {
		t.Fatal("it is not catching up")
	}
}

func TestTheNewestMinutesAreLeftAlone(t *testing.T) {
	// They are being written right now; reading them gets a torn end.
	box := testbox.NewBox()
	one := [][2]float64{{mm(0), mm(10)}}
	deviceHolder(box, mm(10), one...)
	aWatch(t, box, "earliest", nil)
	d := &door{}
	w := surveyor(t, box, one, d, 1800)
	w.ReconcileOnce()
	if len(d.reads) == 0 {
		t.Fatal("nothing was read, so nothing is proved")
	}
	for _, r := range d.reads {
		if r[1] > mm(10)-w.Settle {
			t.Fatalf("read into the minutes being written: %v", r)
		}
	}
}

func TestNobodyHoldingTheCameraIsAWaitNotASilence(t *testing.T) {
	box := testbox.NewBox()
	aWatch(t, box, "earliest", nil)
	w := surveyor(t, box, nil, &door{}, 1800)
	w.ReconcileOnce()
	st := w.Status("7-lpr")
	if p.Str(st["phase"]) != "waiting" || !strings.Contains(p.Str(st["why"]), "holds") {
		t.Fatalf("%v", st)
	}
}

func TestADriverThatCannotListIsWatchedAsTheSummarySays(t *testing.T) {
	// "Cannot list" is not "holds nothing". Read as empty, the survey would move its frontier over minutes it
	// never looked at — and say it had watched them.
	box := testbox.NewBox()
	deviceHolder(box, mm(100), twoSpans...)
	aWatch(t, box, "earliest", nil)
	d := &door{}
	w := surveyor(t, box, twoSpans, d, 1800)
	w.Index = func(string, float64, float64) ([][2]float64, error) { return nil, vms.ErrCannotList }
	w.ReconcileOnce()
	eq(t, d.reads, [][2]float64{{mm(0), mm(30)}}) // the window, as if it were continuous
}

func TestTheSurveyCarriesItsOwnBudget(t *testing.T) {
	if vms.SURVEY.HeartbeatKey("x") == vms.DET.HeartbeatKey("x") {
		t.Fatal("one heartbeat for two budgets")
	}
	if vms.SurveySpec.RetireField != "" {
		t.Fatal("a survey does not end")
	}
	if vms.SurveySpec.Near != "vms" || vms.SurveySpec.NearBy != "cam" || vms.SurveySpec.Home != "" {
		t.Fatal(vms.SurveySpec.Near, vms.SurveySpec.NearBy, vms.SurveySpec.Home)
	}
}

// -- keep: hits — watch everything, copy what a model liked -----------------------------------------------

func TestMomentsBecomeStretches(t *testing.T) {
	// An event is an instant and footage is an interval. `pre`/`post` make the clip watchable; `join` keeps a
	// busy minute from becoming three hundred two-second files; the watched spans clip the padding so nobody
	// asks for minutes the device never recorded.
	w := [][2]float64{{0, 100}}
	eq(t, vms.HitSpans([]float64{10, 12, 80}, w, 5, 5, 30), [][2]float64{{5, 17}, {75, 85}}) // two joined, one apart
	eq(t, vms.HitSpans([]float64{10, 80}, w, 5, 5, 5), [][2]float64{{5, 15}, {75, 85}})
	eq(t, vms.HitSpans([]float64{98}, w, 5, 5, 5), [][2]float64{{93, 100}}) // padding clipped to what we watched
	eq(t, vms.HitSpans(nil, w, 5, 5, 5), [][2]float64{})
	eq(t, vms.HitSpans([]float64{10}, [][2]float64{{0, 8}, {20, 30}}, 5, 5, 5), [][2]float64{{5, 8}}) // and to the gaps in it
	eq(t, vms.HitSpans([]float64{80, 10}, w, 5, 5, 5), [][2]float64{{5, 15}, {75, 85}})               // in any order
}

func TestASurveyThatKeepsReportsWhatToKeep(t *testing.T) {
	box := testbox.NewBox()
	one := [][2]float64{{mm(0), mm(10)}}
	deviceHolder(box, mm(100), one...)
	aWatch(t, box, "earliest", p.Row{"keep": "hits", "pre": 60.0, "post": 60.0, "join": 120.0})
	w := surveyor(t, box, one, &door{}, 1800)
	w.ReconcileOnce()
	w.HeartbeatOnce()
	hb := p.Heartbeats(box.Objects, "survey/")["s-1"]
	hits := hb.ExtraString("hits", "")
	if hits == "" {
		t.Fatal("the model fired and nobody was told to keep it")
	}
	for _, h := range strings.Split(hits, ",") {
		if !strings.HasPrefix(h, "7|") {
			t.Fatal(hits)
		}
	}
}

func TestASurveyThatKeepsNothingAsksForNothing(t *testing.T) {
	// `keep: none` is the default: thirty days of somebody else's NVR is not something to start copying
	// because a model was enabled.
	box := testbox.NewBox()
	one := [][2]float64{{mm(0), mm(10)}}
	deviceHolder(box, mm(100), one...)
	aWatch(t, box, "earliest", nil)
	w := surveyor(t, box, one, &door{}, 1800)
	w.ReconcileOnce()
	w.HeartbeatOnce()
	if h := p.Heartbeats(box.Objects, "survey/")["s-1"].ExtraString("hits", ""); h != "" || w.EventsWritten == 0 {
		t.Fatal(h, w.EventsWritten)
	}
}

func TestAHolderThatDoesNotAnswerIsNotABusyDevice(t *testing.T) {
	// Two different waits, and the operator acts on them differently: a busy device clears by itself, a
	// holder that does not answer is somebody's problem.
	box := testbox.NewBox()
	deviceHolder(box, mm(100), twoSpans...)
	aWatch(t, box, "earliest", nil)
	w := surveyor(t, box, twoSpans, &door{}, 1800)
	w.Fetch = func(string, float64, float64) error { return os.ErrDeadlineExceeded }
	w.ReconcileOnce()
	st := w.Status("7-lpr")
	if p.Str(st["phase"]) != "waiting" || strings.Contains(p.Str(st["why"]), "session") {
		t.Fatalf("%v", st)
	}
}

func TestASurveyWhoseLeaseLapsedWatchesNothing(t *testing.T) {
	// A pass is ten minutes of somebody's device. If the lease ran out, the controller has given the watch to
	// another worker, and two workers watching one archive write every event twice.
	box := testbox.NewBox()
	deviceHolder(box, mm(100), twoSpans...)
	aWatch(t, box, "earliest", nil)
	w := surveyor(t, box, twoSpans, &door{}, 600)
	w.LeaseTTL, w.LeaseMargin = 30, 5
	w.ReconcileOnce()
	at, _ := frontier(box)
	written := w.EventsWritten

	box.Clock.Advance(26) // past the lease, short of a renewal
	deviceHolder(box, mm(100), twoSpans...)
	if w.MayWrite("7-lpr") {
		t.Fatal("expired")
	}
	w.ReconcileOnce()
	if now, _ := frontier(box); now != at || w.EventsWritten != written {
		t.Fatalf("it watched with no lease: frontier %v -> %v, events %d -> %d", at-mm(0), now-mm(0), written, w.EventsWritten)
	}
}
