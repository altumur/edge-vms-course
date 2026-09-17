package w2cplatform_test

// Lesson 1 — the subsystem contract, and the platform that knows nothing.

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

	"vmsserver/testbox"
	p "vmsserver/w2cplatform"
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

// Local is PeerClient's three calls, against directories.
type Local struct{ roots map[string]string }

func (l Local) Mirrored(url, server string) ([]p.Bucket, error) {
	return p.MirroredBuckets(l.roots[url], server, 600), nil
}
func (l Local) Put(url, server, path string, data []byte) error {
	pth := filepath.Join(l.roots[url], p.MirrorDir, server, path)
	os.MkdirAll(filepath.Dir(pth), 0o755)
	return os.WriteFile(pth, data, 0o644)
}
func (l Local) Get(url, server, path string) ([]byte, error) {
	return os.ReadFile(filepath.Join(l.roots[url], p.MirrorDir, server, path))
}

func TestTheResourceIsAPlatformJobThatMirrorsAnySubsystemsBuckets(t *testing.T) {
	// Two resources on one box (two roots), one raft. The knob is one Variable;
	// each resource copies its CLOSED buckets — whatever subsystem wrote them —
	// to the next live resource after it; a resource back with an empty disk
	// pulls its own buckets home. Nothing here knows what a bucket is about.
	box := testbox.NewBox()
	tt := box.Wall.Now() - 7200
	roots := map[string]string{}
	for _, s := range []string{"srv-a", "srv-b", "srv-c"} {
		roots[s], _ = os.MkdirTemp("", "res-"+s+"-")
	}
	res := map[string]*p.Resource{}
	for s, r := range roots {
		res[s] = p.NewResource(r, s, s, box.Vars, box.Objects, 600, box.Wall.Now, Local{roots})
	}
	p.NewEventLog(roots["srv-a"], "thing", "x", 1, 600).Append(tt+5, "tick", map[string]any{"n": 1})    // some subsystem's bucket, closed
	p.NewEventLog(roots["srv-a"], "other", "y", 2, 600).Append(tt+9, "seen", nil)                       // another's
	p.NewEventLog(roots["srv-a"], "thing", "x", 1, 600).Append(tt+7000, "tick", map[string]any{"n": 2}) // the open one
	for _, r := range res {
		r.Heartbeat()
	}
	if u := p.ResourcesSeen(box.Objects)["srv-a"].Units; !reflect.DeepEqual(u, map[string][]string{"other": {"y"}, "thing": {"x"}}) {
		t.Fatal(u)
	}
	all := []string{"srv-a", "srv-b", "srv-c"}
	if !reflect.DeepEqual(p.PeersOf("srv-a", all, 1), []string{"srv-b"}) || !reflect.DeepEqual(p.PeersOf("srv-c", all, 1), []string{"srv-a"}) {
		t.Fatal("peers")
	}
	if res["srv-a"].Pass()["enabled"] != false { // knob off: nothing leaves
		t.Fatal("knob")
	}
	box.Vars.Put(p.MirrorKey, p.Items{"enabled": "true", "copies": "1"}, p.NoCAS)
	r := res["srv-a"].Pass()
	if r["mirrored"] != 2 || !reflect.DeepEqual(r["peers"], []string{"srv-b"}) {
		t.Fatal(r)
	}
	if res["srv-a"].Pass()["mirrored"] != 0 { // once
		t.Fatal("twice")
	}
	res["srv-b"].Heartbeat()
	if m := p.ResourcesSeen(box.Objects)["srv-b"].Mirrors; !reflect.DeepEqual(m, map[string]int{"srv-a": 2}) {
		t.Fatal(m)
	}
	if _, ok := res["srv-b"].Units()[".mirror"]; ok { // a copy is not srv-b's data
		t.Fatal(".mirror listed")
	}
	os.RemoveAll(roots["srv-a"]) // srv-a back with a replaced disk
	os.MkdirAll(roots["srv-a"], 0o755)
	if res["srv-a"].Restore()["pulled"] != 2 {
		t.Fatal("restore")
	}
	bs := p.BucketsUnder(roots["srv-a"], "thing", "x", 600)
	if len(bs) != 1 || bs[0].Events != 1 { // the closed one is home; the open one was the RPO
		t.Fatal(bs)
	}
	box.Vars.Put("other/retention", p.Items{"days": "1"}, p.NoCAS)
	box.Wall.Advance(3 * 86400)
	if res["srv-a"].Retain() != 1 || len(p.BucketsUnder(roots["srv-a"], "other", "y", 600)) != 0 { // each subsystem's days, from its own row
		t.Fatal("retain")
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

// counterHook is a subsystem that keeps no video and no buckets — it counts. Nothing in these tests knows
// what a camera is, which is the point of the door being a method name.
type counterHook struct {
	asked []int64
	ticks []int64
}

func (c *counterHook) Pass(now float64) map[string]any { return map[string]any{"ticks": len(c.ticks)} }

func (c *counterHook) Free(need int64, now, minDays float64) map[string]any {
	c.asked = append(c.asked, need)
	var freed int64
	for len(c.ticks) > 0 && freed < need { // one tick at a time, like a segment
		freed += c.ticks[0]
		c.ticks = c.ticks[1:]
	}
	return map[string]any{"freed": freed, "dropped": 10 - len(c.ticks)}
}

type bucketsOnly struct{}

func (bucketsOnly) Pass(now float64) map[string]any { return map[string]any{} }

func TestTheWatermarkAsksAndNeverDeletes(t *testing.T) {
	// The resource measures the DISK (a test cannot fill one, so the probe is a seam), and over the high
	// mark it says how many bytes to free — down to the LOW mark, or the next write puts it straight back
	// over. What to give up is the subsystem's to decide: the platform calls Free and touches nothing.
	box := testbox.NewBox()
	dir, _ := os.MkdirTemp("", "space-")
	hook := &counterHook{ticks: make([]int64, 10)}
	for i := range hook.ticks {
		hook.ticks[i] = 50_000
	}
	res := p.NewResource(dir, "srv-1", "http://srv-1", box.Vars, box.Objects, 600, box.Wall.Now, nil)
	res.SpaceProbe = func(string) (int64, int64) { return 1_000_000, 500_000 }
	res.Register("counter", hook)

	if !reflect.DeepEqual(res.Relieve(), map[string]any{"space": "off"}) { // a knob, off until an operator says so
		t.Fatal(res.Relieve())
	}
	box.Vars.Put(p.SpaceKey, p.Items{"enabled": "true", "high": "0.85", "low": "0.75"}, p.NoCAS)
	if !reflect.DeepEqual(res.Relieve(), map[string]any{"space": "ok", "full": 0.5}) {
		t.Fatal(res.Relieve())
	}
	if len(hook.asked) != 0 {
		t.Fatal(hook.asked)
	}
	hb, _ := res.Heartbeat()
	if hb.Space.Free != 500_000 || hb.Schema != p.Schema || hb.Build != p.Build {
		t.Fatal(hb.Space, hb.Schema, hb.Build)
	}

	res.SpaceProbe = func(string) (int64, int64) { return 1_000_000, 100_000 } // 90 % full
	rep := res.Relieve()
	if len(hook.asked) != 1 || hook.asked[0] != 150_000 { // to the LOW mark, not to the high one
		t.Fatal(hook.asked)
	}
	if rep["space"] != "over" || rep["need"] != int64(150_000) || rep["freed"] != int64(150_000) || rep["short"] != int64(0) {
		t.Fatal(rep)
	}
	if rep["counter.dropped"] != 3 || len(hook.ticks) != 7 { // three ticks of fifty kB, and not one more
		t.Fatal(rep, len(hook.ticks))
	}

	// a subsystem with no Free is simply not asked: retention by days is its whole policy
	res2 := p.NewResource(dir, "srv-1", "http://srv-1", box.Vars, box.Objects, 600, box.Wall.Now, nil)
	res2.SpaceProbe = func(string) (int64, int64) { return 1_000_000, 100_000 }
	res2.Register("other", bucketsOnly{})
	rep = res2.Relieve()
	if rep["short"] != rep["need"] || rep["freed"] != int64(0) { // nobody could give anything: said, not hidden
		t.Fatal(rep)
	}
}

func TestTheTreeIsWalkedOnceAPassAndNeverOnAHeartbeat(t *testing.T) {
	// `usage` answers "how much do we hold" and can only be answered by walking; `space` answers "how much
	// is left" and is one syscall. The first is measured with the policy pass and published from the cache
	// with the time it was taken; the second is live in every heartbeat.
	//
	// At fifty cameras and ten-minute segments a month of archive is a quarter of a million files. Walking
	// them every ten seconds does not merely cost a second — it touches every inode in the tree, so the
	// cache holds the archive's metadata instead of the video the machine exists to serve.
	box := testbox.NewBox()
	dir, _ := os.MkdirTemp("", "usage-")
	os.WriteFile(filepath.Join(dir, "a"), make([]byte, 4096), 0o644)
	res := p.NewResource(dir, "srv-1", "http://srv-1", box.Vars, box.Objects, 600, box.Wall.Now, nil)
	res.SpaceProbe = func(string) (int64, int64) { return 1_000_000, 400_000 }

	hb, _ := res.Heartbeat() // the first one of a process pays for it once
	if hb.Usage != 4096 || hb.UsageAt != box.Wall.Now() {
		t.Fatal(hb.Usage, hb.UsageAt)
	}
	os.WriteFile(filepath.Join(dir, "b"), make([]byte, 4096), 0o644) // the tree grows…
	box.Wall.Advance(10)
	for i := 0; i < 59; i++ { // …ten minutes of heartbeats, one per ten seconds
		hb, _ = res.Heartbeat()
	}
	if hb.Usage != 4096 || hb.UsageAt >= hb.Ts { // not one more walk, and the number says how old it is
		t.Fatal(hb.Usage, hb.UsageAt, hb.Ts)
	}
	if hb.Space.Free != 400_000 { // while `space` is measured every time
		t.Fatal(hb.Space)
	}
	res.Pass() // the pass is where the walk belongs
	hb, _ = res.Heartbeat()
	if hb.Usage != 8192 || hb.UsageAt != box.Wall.Now() {
		t.Fatal(hb.Usage, hb.UsageAt)
	}
}

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
