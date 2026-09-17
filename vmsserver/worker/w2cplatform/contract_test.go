package w2cplatform_test

// The subsystem contract — the surface both halves of the platform must keep, byte for byte.
//
// This is the Go side of it. Everything here is checked again, in the same words, by the Python suite;
// the two are only really held together by tests/cross/ (a Go worker against a Python controller over one
// store), because a green suite on each side proves each side self-consistent and nothing about whether
// they agree.

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"

	"vmsserver/worker/testbox"
	p "vmsserver/worker/w2cplatform"
)

func TestTheConfigStoreSurvivesARestartAndRefusesAStaleCAS(t *testing.T) {
	box := testbox.NewBox()
	// create-only: the path must not exist yet. Nothing here reads the index as a
	// number — it is opaque, and the test may only compare it for equality.
	idx, err := box.Vars.Put("vms/cameras/7", p.Items{"name": "gate", "revision": "1"}, p.Absent)
	if err != nil || !idx.Exists() {
		t.Fatal(idx, err)
	}
	if _, err := box.Vars.Put("vms/cameras/7", p.Items{"name": "again"}, p.Absent); !errors.Is(err, p.ErrConflict) {
		t.Fatal("create-only must refuse a second creation")
	}
	again, _ := p.NewFileVariables(box.Vars.Root) // a new process, same directory
	items, idx2, _ := again.Get("vms/cameras/7")
	if !reflect.DeepEqual(items, p.Items{"name": "gate", "revision": "1"}) || idx2 != idx {
		t.Fatal(items, idx2)
	}
	moved, err := again.Put("vms/cameras/7", p.Items{"name": "x"}, idx) // the index I read: mine
	if err != nil || moved == idx {
		t.Fatal(moved, err)
	}
	// `idx` is now stale — and staleness is equality against the current version,
	// never "smaller than". There is no arithmetic to do on an opaque index.
	if _, err := again.Put("vms/cameras/7", p.Items{"name": "y"}, idx); !errors.Is(err, p.ErrConflict) {
		t.Fatal("must conflict")
	}
	l, _ := again.List("vms/")
	none, z, _ := again.Get("nope")
	if !reflect.DeepEqual(l, []string{"vms/cameras/7"}) || none != nil || z != p.Absent || z.Exists() {
		t.Fatal(l, none, z)
	}
}

func TestTwoProcessesOneCASWinner(t *testing.T) {
	box := testbox.NewBox()
	var mu sync.Mutex
	var wg sync.WaitGroup
	wins := 0
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			v, _ := p.NewFileVariables(box.Vars.Root) // each "process" opens the store itself
			for j := 0; j < 40; j++ {
				items, idx, _ := v.Get("counter")
				n := 1
				if items != nil {
					n, _ = strconv.Atoi(items["n"])
					n++
				}
				if _, err := v.Put("counter", p.Items{"n": strconv.Itoa(n)}, idx); err == nil {
					mu.Lock()
					wins++
					mu.Unlock()
				}
			}
		}()
	}
	wg.Wait()
	items, _, _ := box.Vars.Get("counter")
	n, _ := strconv.Atoi(items["n"])
	if n != wins { // every successful write counted exactly once
		t.Fatal(n, wins)
	}
}

func TestOneWriterPerPrefix(t *testing.T) {
	box := testbox.NewBox()
	ctl := box.Vars.AsWriter("vmscontroller", "vms/*")
	wrk := box.Vars.AsWriter("vmsworker-1", "vms/epoch/*")
	if _, err := ctl.Put("vms/cameras/7", p.Items{"name": "gate"}, p.NoCAS); err != nil {
		t.Fatal(err)
	}
	if _, err := wrk.Put("vms/epoch/7", p.Items{"epoch": "1"}, p.NoCAS); err != nil {
		t.Fatal(err)
	}
	if _, err := wrk.Put("vms/cameras/7", p.Items{"name": "mine now"}, p.NoCAS); !errors.Is(err, p.ErrForbidden) {
		t.Fatal("a worker never writes configuration")
	}
}

func TestEpochIssuerNeverReusesANumber(t *testing.T) {
	box := testbox.NewBox()
	var mu sync.Mutex
	var wg sync.WaitGroup
	var issued []int
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			v, _ := p.NewFileVariables(box.Vars.Root)
			for j := 0; j < 25; j++ {
				e, _, err := p.NextEpoch(v, "vms/epoch/7")
				if err != nil {
					t.Error(err)
				}
				mu.Lock()
				issued = append(issued, e)
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	sort.Ints(issued)
	for i, e := range issued {
		if e != i+1 {
			t.Fatal(issued)
		}
	}
	if cur, _ := p.CurrentEpoch(box.Vars, "vms/epoch/7"); cur != 100 {
		t.Fatal(cur)
	}
}

func TestLeaseOnAMonotonicClock(t *testing.T) {
	box := testbox.NewBox()
	clk := testbox.NewClock(1000)
	e, _, _ := p.NextEpoch(box.Vars, "vms/epoch/7")
	lease := p.NewLease(box.Vars, "vms/epoch/7", e, 30, 5, clk.Now)
	clk.Advance(24.9)
	if !lease.MayWrite() {
		t.Fatal("may write at 24.9")
	}
	clk.Advance(0.2)
	if lease.MayWrite() || lease.SecondsLeft() != 0 {
		t.Fatal("must not write at 25.1")
	}
	if !lease.Renew() || !lease.MayWrite() {
		t.Fatal("renew")
	}
	p.NextEpoch(box.Vars, "vms/epoch/7") // somebody else took camera 7
	if lease.Renew() || !lease.Fenced || lease.Conflicts != 1 {
		t.Fatal("fenced")
	}
}

func TestThePlatformKnowsNothingAboutVideo(t *testing.T) {
	// No import from vms/ anywhere under w2cplatform/ — not even the word "camera".
	ents, _ := os.ReadDir(".")
	for _, e := range ents {
		if !strings.HasSuffix(e.Name(), ".go") || strings.HasSuffix(e.Name(), "_test.go") {
			continue
		}
		src, _ := os.ReadFile(filepath.Join(".", e.Name()))
		code := []string{} // the code, not the notes
		for _, l := range strings.Split(string(src), "\n") {
			if !strings.HasPrefix(strings.TrimSpace(l), "//") {
				code = append(code, l)
			}
		}
		s := strings.ToLower(strings.Join(code, "\n"))
		if strings.Contains(s, "vmsserver/vms\"") || strings.Contains(s, "camera") {
			t.Fatal(e.Name())
		}
	}
	sub := p.Subsystem{Name: "vms"}
	if sub.Assignment("w-1") != "vms/workers/w-1" || sub.EpochKey("7") != "vms/epoch/7" ||
		sub.HeartbeatKey("w-1") != "vms/w-1/heartbeat" || !reflect.DeepEqual(sub.ACLController(), []string{"vms/*"}) {
		t.Fatal(sub)
	}
}

func TestControllerAndWorkerBasesSpeakOnlyTheContract(t *testing.T) {
	box := testbox.NewBox()
	sub := p.Subsystem{Name: "thing"}
	ctl := p.NewController(sub, box.Vars, box.Objects, box.Wall.Now)
	w := p.NewWorker(sub, box.Vars, box.Objects, p.WorkerOptions{Name: "t-1", Clock: box.Clock.Now, Wall: box.Wall.Now})
	if len(ctl.WorkersSeen(45)) != 0 { // nobody has heartbeaten
		t.Fatal("seen")
	}
	w.HeartbeatWith([]map[string]any{{"id": 1, "phase": "running"}}, map[string]any{"server": "srv-1"})
	seen := ctl.WorkersSeen(45)
	if len(seen) != 1 || seen["t-1"].Extra["server"] != "srv-1" {
		t.Fatal(seen)
	}
	a, _ := ctl.Assign("t-1", []string{"3", "1", "2"})
	if !reflect.DeepEqual(a.Units, []string{"1", "2", "3"}) || a.Rev != 1 || !reflect.DeepEqual(w.Assignment().Units, []string{"1", "2", "3"}) {
		t.Fatal(a)
	}
	if a2, _ := ctl.Assign("t-1", []string{"1"}); a2.Rev != 2 {
		t.Fatal(a2)
	}
	if e, _ := w.TakeEpoch("1"); e != 1 || !w.MayWrite("1") {
		t.Fatal(e)
	}
	box.Wall.Advance(100)
	if len(ctl.WorkersSeen(45)) != 0 { // a silent worker is not a worker
		t.Fatal("silent")
	}
}

func TestIdentityByClaimIsAPlatformPiece(t *testing.T) {
	// A name is a slot: taken by CAS, renewed, released on purpose or lapsed by
	// silence. Two processes claiming without a preference get two names; a
	// third, after the first lapsed, gets the first's name back — and its assignment.
	box := testbox.NewBox()
	sub := p.Subsystem{Name: "thing"}
	ctl := p.NewController(sub, box.Vars, box.Objects, box.Wall.Now)
	mk := func(inst string) *p.Worker {
		return p.NewWorker(sub, box.Vars, box.Objects, p.WorkerOptions{Clock: box.Clock.Now, Wall: box.Wall.Now, Instance: inst})
	}
	a, b := mk("A"), mk("B")
	na, _ := a.ClaimSlot("")
	nb, _ := b.ClaimSlot("")
	if na != "w-1" || nb != "w-2" { // `count = 2`: two names, in order
		t.Fatal(na, nb)
	}
	ctl.Assign("w-1", []string{"1", "2"})
	if !a.RenewSlot() || !b.RenewSlot() {
		t.Fatal("renew")
	}
	box.Wall.Advance(46) // A went silent for longer than the slot TTL
	c := mk("C")
	if nc, _ := c.ClaimSlot(""); nc != "w-1" || !reflect.DeepEqual(c.Assignment().Units, []string{"1", "2"}) { // the replacement inherits
		t.Fatal(nc)
	}
	if a.RenewSlot() { // A, if it is still alive, finds out
		t.Fatal("A must be fenced")
	}
	if len(ctl.ReleasedSlots()) != 0 { // a lapse is not a release
		t.Fatal(ctl.ReleasedSlots())
	}
	b.ReleaseSlot() // scale-in: B is told to stop and says so
	ctl.Assign("w-2", []string{"3"})
	if !reflect.DeepEqual(ctl.ReleasedSlots(), []string{"w-2"}) { // what the subsystem redistributes
		t.Fatal(ctl.ReleasedSlots())
	}
	d := mk("D")
	if nd, _ := d.ClaimSlot("w-7"); nd != "w-7" { // the scheduler's index wins, and creates
		t.Fatal(nd)
	}
	var names []string
	for n := range ctl.Slots() {
		names = append(names, n)
	}
	sort.Strings(names)
	if !reflect.DeepEqual(names, []string{"w-1", "w-2", "w-7"}) || sub.SlotKey("w-1") != "thing/slots/w-1" ||
		!reflect.DeepEqual(sub.ACLWorker(), []string{"thing/epoch/*", "thing/slots/*"}) {
		t.Fatal(names)
	}
}

func TestTheCatalogueAnswersOnlyForHoldersThatAreStillHere(t *testing.T) {
	// Heartbeats is the READ MODEL: it shows every worker's last heartbeat
	// whatever its age, because a console must be able to say "silent for four
	// minutes". Holders is what a caller uses before it goes and TALKS to one —
	// a subscriber, a playback door, a backfill fetch. Handing those a silent
	// worker is how a gateway ends up subscribing to a process that is gone.
	objects, _ := p.NewFsObjectStore(t.TempDir())
	sub := p.Subsystem{Name: "vms"}
	beat := func(w string, ts float64, status []map[string]any) {
		objects.Put(sub.HeartbeatKey(w), p.Heartbeat{Worker: w, Ts: ts, Status: status}.ToBytes())
	}
	beat("w-1", 1000, []map[string]any{{"id": "7", "phase": "running", "live_url": "rtsp://srv-a:8554/7"}})
	beat("w-2", 900, []map[string]any{{"id": "8", "phase": "held"}}) // 120 s ago: gone
	now := 1020.0

	if got := p.Heartbeats(objects, "vms/"); len(got) != 2 {
		t.Fatal("the read model shows both, however old:", got)
	}
	hs := p.Holders(objects, "vms/", now, 45)
	if len(hs) != 1 {
		t.Fatal(hs)
	}
	if _, ok := hs["w-1"]; !ok {
		t.Fatal(hs)
	}

	h, ok := p.HolderOf(objects, "vms/", "7", now, p.HolderQuery{})
	if !ok || h.Worker != "w-1" || p.Str(h.Status["live_url"]) != "rtsp://srv-a:8554/7" {
		t.Fatal(h, ok)
	}
	if _, ok := p.HolderOf(objects, "vms/", "8", now, p.HolderQuery{}); ok {
		t.Fatal("a silent holder must not be an answer")
	}
	// phase narrows it: a recorder subscribes only to a fan-out that is running
	if _, ok := p.HolderOf(objects, "vms/", "7", now, p.HolderQuery{Phase: "held"}); ok {
		t.Fatal("phase must narrow")
	}
	if _, ok := p.HolderOf(objects, "vms/", "7", now, p.HolderQuery{Phase: "running"}); !ok {
		t.Fatal("phase must match")
	}
	// field narrows it: no door published, no answer — rather than a broken URL
	if _, ok := p.HolderOf(objects, "vms/", "7", now, p.HolderQuery{Field: "playback_url"}); ok {
		t.Fatal("a missing field must narrow")
	}
}

func TestATornLastLineCostsTheLineAndNotTheBucket(t *testing.T) {
	// Append writes and flushes without fsync, so a crash can leave the last line
	// half-written. Losing ten minutes of observations because one record was
	// damaged is the wrong trade — the same shape as it is for footage: the open
	// thing, not the day. Skipping silently would be the wrong trade too, so the
	// skips are counted.
	path := filepath.Join(t.TempDir(), "20250101T000000Z.events.jsonl")
	os.WriteFile(path, []byte(`{"t":1,"kind":"started"}`+"\n"+`{"t":2,"kind":"position"}`+"\n"+`{"t":3,"ki`), 0o644)
	before := p.TornLines()
	got := p.ReadBucket(path)
	if len(got) != 2 || got[0].Kind() != "started" || got[1].Kind() != "position" {
		t.Fatal(got)
	}
	if p.TornLines() != before+1 {
		t.Fatal("a torn line must be counted:", before, p.TornLines())
	}
	if p.ReadBucket(filepath.Join(t.TempDir(), "nope.jsonl")) != nil {
		t.Fatal("a missing bucket is empty, not an error")
	}
}

// -- the watermark and the schema: what an upgrade needs of the platform ---------------------------

func TestTheSchemaIsRaisedAfterTheUpgradeAndNeverDuringIt(t *testing.T) {
	// A rolling upgrade means old and new processes read the same rows for a while. Adding a field is
	// free; changing what one MEANS is a new schema number — and the direction of every check here is what
	// keeps the upgrade rolling. A NEWER process against an older store is fine: it understands the old
	// layout, and that is the whole of an upgrade. An OLDER process against a newer store refuses to start.
	box := testbox.NewBox()
	ctl := p.NewController(p.Subsystem{Name: "vms"}, box.Vars, box.Objects, box.Wall.Now)
	if p.SchemaVersion(box.Vars) != p.Schema { // absent: a fresh install is whatever this build is
		t.Fatal(p.SchemaVersion(box.Vars))
	}
	_, idx, _ := box.Vars.Get(p.SchemaKey)
	box.Vars.Put(p.SchemaKey, p.Items{"version": strconv.Itoa(p.Schema + 1)}, idx) // somebody upgraded the store
	if err := p.CheckSchema(box.Vars); err == nil || !strings.Contains(err.Error(), "understands") {
		t.Fatal(err)
	}
	func() { // a build older than the store does not run at all
		defer func() {
			if recover() == nil {
				t.Error("an old build started against a newer store")
			}
		}()
		p.NewController(p.Subsystem{Name: "vms"}, box.Vars, box.Objects, box.Wall.Now)
	}()
	_, idx, _ = box.Vars.Get(p.SchemaKey)
	box.Vars.Put(p.SchemaKey, p.Items{"version": strconv.Itoa(p.Schema)}, idx)

	// raising is refused while anything live understands less: you cannot raise the store out from under a
	// machine you forgot to upgrade
	box.Objects.Put("vms/w-2/heartbeat", p.Heartbeat{Worker: "w-2", Ts: box.Wall.Now(),
		Extra: map[string]any{"server": "srv-2", "schema": p.Schema, "build": "old"}}.ToBytes())
	err := ctl.SetSchema(p.Schema + 1)
	if err == nil || !strings.Contains(err.Error(), "still running") || !strings.Contains(err.Error(), "vms/w-2") {
		t.Fatal(err)
	}
	box.Wall.Advance(60) // w-2 is gone; w-1 is new and says so
	box.Objects.Put("vms/w-1/heartbeat", p.Heartbeat{Worker: "w-1", Ts: box.Wall.Now(),
		Extra: map[string]any{"server": "srv-1", "schema": p.Schema + 1}}.ToBytes())
	if err := ctl.SetSchema(p.Schema + 1); err != nil {
		t.Fatal(err)
	}
	if err := ctl.SetSchema(p.Schema); err == nil || !strings.Contains(err.Error(), "does not go back") {
		t.Fatal(err)
	}
}
