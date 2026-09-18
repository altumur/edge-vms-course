package vms_test

// Lesson 21 — the disk fills. Retention by days is a PROMISE to the operator; this file is about what
// happens when the promise cannot be kept, which is deliberately not the same thing.
//
// Three steps, in this order, and the order is the design: give up what is not ours, cut above the floor,
// say the shortfall out loud. Step 1 is why there is no evacuation button, no schedule and no "recovery
// mode" — footage a neighbour wrote here while our server was down is playable, indexed and in nobody's
// way, right up until the disk it sits on needs the room, and then the server that needs it acts.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/w2cplatform"
)

// layDown: a segment on disk and its line in the manifest — the two halves the archive keeps, written the
// way Promote writes them, so nothing below is measuring a fixture.
func layDown(t *testing.T, root, unit string, epoch int, start float64, size int) vms.Segment {
	t.Helper()
	pth := vms.SegmentPath(root, unit, epoch, time.Unix(int64(start), 0).UTC())
	if err := os.MkdirAll(filepath.Dir(pth), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(pth, make([]byte, size), 0o644); err != nil {
		t.Fatal(err)
	}
	rel, _ := filepath.Rel(root, pth)
	seg := vms.Segment{Unit: unit, Epoch: epoch, Start: start, End: start + 600,
		Path: filepath.ToSlash(rel), Bytes: int64(size), Source: "live"}
	if err := vms.NewManifest(root, unit).Append(seg); err != nil {
		t.Fatal(err)
	}
	return seg
}

// spaceBox: one archive, one unit, `days` deep in `n` segments, and the watermark switched on.
func spaceBox(t *testing.T, days float64, size, n int) (*testbox.Box, *vms.ArchiveResource) {
	t.Helper()
	box := testbox.NewBox()
	if _, err := box.Vars.Put(p.SpaceKey, p.Items{"enabled": "true", "high": "0.85", "low": "0.75", "min_days": "3"}, p.Absent); err != nil {
		t.Fatal(err)
	}
	arch := vms.NewArchiveResource(box.Spool, box.Archive, 600, box.Wall.Now)
	now := box.Wall.Now()
	for i := 0; i < n; i++ {
		layDown(t, box.Archive, "1", 1, now-(days-float64(i)*days/float64(n))*86400, size)
	}
	return box, arch
}

func TestTheWatermarkIsAFloorAndAShortfallNotAQuietCut(t *testing.T) {
	// Freeing stops at the floor, and what could not be freed is a NUMBER in the report — not a quiet cut
	// into yesterday that nobody asked for and nobody is told about. An operator can act on a shortfall;
	// there is nothing to do about footage that has already gone.
	box, arch := spaceBox(t, 10, 50_000, 10)
	policy := &vms.ArchivePolicy{Res: arch, Vars: box.Vars} // no peers on this box: nowhere to evacuate to
	now := box.Wall.Now()
	if d := math.Round(vms.DepthDays(arch, "1", now)); d != 10 {
		t.Fatal(d)
	}

	rep := policy.Free(150_000, now, 3) // three segments' worth
	if rep["freed"] != int64(150_000) || rep["cut"] != 3 || rep["shortfall"] != nil {
		t.Fatal(rep)
	}
	if d := math.Round(vms.DepthDays(arch, "1", now)); d != 7 {
		t.Fatal(d)
	}
	if left := vms.NewManifest(box.Archive, "1").Read(); len(left) != 7 {
		t.Fatal(len(left))
	}

	rep = policy.Free(10_000_000, now, 3) // more than there is above the floor
	short, _ := rep["shortfall"].(int64)
	if short <= 0 || rep["freed"].(int64) >= 10_000_000 {
		t.Fatal(rep)
	}
	left := vms.NewManifest(box.Archive, "1").Read()
	depth := vms.DepthDays(arch, "1", now)
	if len(left) == 0 || depth < 3 || depth > 4 { // AT the floor — not emptied, and not taken below it
		t.Fatal(len(left), depth)
	}
}

func TestAResourceOverTheMarkSaysSoAndOneUnderItDoesNothing(t *testing.T) {
	// Two marks and the gap between them. One mark alone gives a saw: a file freed, a file written, for
	// ever. Freeing goes down to LOW, and the gap is chosen in hours of ingest rather than in percent.
	box, arch := spaceBox(t, 10, 50_000, 10)
	res := p.NewResource(box.Archive, "srv-1", "http://srv-1", box.Vars, box.Objects, 600, box.Wall.Now, nil)
	res.SpaceProbe = func(string) (int64, int64) { return 1_000_000, 500_000 }
	res.Register("rec", &vms.ArchivePolicy{Res: arch, Vars: box.Vars})

	rep := res.Relieve()
	if rep["space"] != "ok" || rep["full"] != 0.5 {
		t.Fatal(rep)
	}
	hb, err := res.Heartbeat() // what a peer reads BEFORE sending anything here
	if err != nil || hb.Space.Free != 500_000 {
		t.Fatal(hb.Space, err)
	}

	res.SpaceProbe = func(string) (int64, int64) { return 1_000_000, 100_000 } // 90 % full
	rep = res.Relieve()
	if rep["space"] != "over" || rep["need"] != int64(150_000) { // down to the LOW mark, not to the high one
		t.Fatal(rep)
	}
	if rep["freed"] != int64(150_000) || rep["short"] != int64(0) || rep["rec.cut"] != 3 {
		t.Fatal(rep)
	}
}

func TestBackfillStopsWhileTheDiskIsOverTheMark(t *testing.T) {
	// Otherwise the two chase each other for ever: the resource frees space, the recorder fetches more of
	// the same hours back. KeepDays closes that trap in time; this closes it in space, and not even an
	// operator's `force` opens it — a range fetched now is a range the resource is about to delete.
	box := testbox.NewBox()
	ctl := vms.NewVmsController(box.Vars, box.Objects, 0, box.Wall.Now)
	w := holder(t, box, func(k string) vms.Device {
		return vms.NewFakeDevice(k, []string{"1"}, map[string]vms.Coverage{"1": {From: 0, To: 1_000_000, Fragments: 5}})
	}, nil)
	mustCreate(t, ctl, map[string]any{"name": "front", "source": card})
	ctl.EnsurePlaced(nil)
	w.ReconcileOnce()
	w.HeartbeatOnce()

	recCtl := p.NewSpecController(vms.RecSpec, box.Vars, box.Objects, 0, box.Wall.Now, "")
	if _, err := recCtl.Create(map[string]any{"cam": "1"}); err != nil {
		t.Fatal(err)
	}
	arch := vms.NewArchiveResource(box.Spool, box.Archive, 600, box.Wall.Now)
	r, err := vms.NewRecWorker("r-1", box.Vars, box.Objects, vms.NewFakeActuator(), arch,
		vms.VmsWorkerOptions{Server: "srv-1", Env: vms.Env{}, WorkerOptions: p.WorkerOptions{Clock: box.Clock.Now, Wall: box.Wall.Now}})
	if err != nil {
		t.Fatal(err)
	}
	r.KeepDays, r.Settle = 1, 1000
	r.HeartbeatOnce()
	recCtl.EnsurePlaced(nil)
	r.ReconcileOnce()

	const now = 1_000_000.0
	if _, err := box.Vars.Put(p.SpaceKey, p.Items{"enabled": "true", "high": "0.85", "low": "0.75"}, p.Absent); err != nil {
		t.Fatal(err)
	}
	r.SpaceProbe = func(string) (int64, int64) { return 1_000_000, 100_000 } // 90 % full
	if !r.UnderPressure() {
		t.Fatal("over the mark and the recorder does not know it")
	}
	if done := r.Backfill(1, now, true); len(done) != 0 { // force does not open it either
		t.Fatal(done)
	}

	r.SpaceProbe = func(string) (int64, int64) { return 1_000_000, 500_000 } // room again…
	if r.UnderPressure() {
		t.Fatal("under the mark and still refusing")
	}
	done := r.Backfill(1, now, true) // …and the same call fetches
	if len(done) == 0 || done[0].Segments == 0 {
		t.Fatal(done)
	}
}

// -- evacuation: why there is no button -------------------------------------------------------------

// dirPeers is a SegmentPeer over the servers' OWN routes: no sockets, the same handlers the console and
// the player go through. A fake that answered by itself would be testing the fake.
type dirPeers struct {
	arch map[string]*vms.ArchiveResource
	mute bool // "takes the bytes, says 204, then goes quiet when asked what it has"
}

func (d *dirPeers) route(url string) p.Extra {
	a, ok := d.arch[url]
	if !ok {
		return nil
	}
	return vms.ResourceRoutesWith(a, nil, "")
}

func (d *dirPeers) PutSegment(url, rel string, data []byte, line string) error {
	h := d.route(url)
	if h == nil {
		return fmt.Errorf("no such server: %s", url)
	}
	req := httptest.NewRequest("PUT", "/segment/"+rel, bytes.NewReader(data))
	req.Header.Set("X-Segment", line)
	w := httptest.NewRecorder()
	if !h(w, req) {
		return fmt.Errorf("PUT %s: no route", rel)
	}
	if w.Code != 200 && w.Code != 201 && w.Code != 204 {
		return fmt.Errorf("PUT %s: %d", rel, w.Code)
	}
	return nil
}

func (d *dirPeers) Manifest(url, unit string) ([]byte, error) {
	if d.mute {
		return nil, fmt.Errorf("%s stopped answering", url)
	}
	h := d.route(url)
	if h == nil {
		return nil, fmt.Errorf("no such server: %s", url)
	}
	w := httptest.NewRecorder()
	if !h(w, httptest.NewRequest("GET", "/manifest/"+unit, nil)) {
		return nil, fmt.Errorf("GET manifest %s: no route", unit)
	}
	if w.Code != 200 {
		return nil, fmt.Errorf("GET manifest %s: %d", unit, w.Code)
	}
	return io.ReadAll(w.Result().Body)
}

var _ vms.SegmentPeer = (*dirPeers)(nil)

type pair struct {
	box   *testbox.Box
	arch  map[string]*vms.ArchiveResource
	res   map[string]*p.Resource
	peers *dirPeers
}

// twoServers: srv-a and srv-b, each with an archive and a resource, sharing one store.
func twoServers(t *testing.T) *pair {
	t.Helper()
	box := testbox.NewBox()
	if _, err := box.Vars.Put(p.SpaceKey, p.Items{"enabled": "true", "high": "0.85", "low": "0.75", "min_days": "3"}, p.Absent); err != nil {
		t.Fatal(err)
	}
	c := &pair{box: box, arch: map[string]*vms.ArchiveResource{}, res: map[string]*p.Resource{},
		peers: &dirPeers{arch: map[string]*vms.ArchiveResource{}}}
	for _, s := range []string{"srv-a", "srv-b"} {
		root := filepath.Join(box.Root, "archive-"+s)
		a := vms.NewArchiveResource(filepath.Join(box.Root, "spool-"+s), root, 600, box.Wall.Now)
		c.arch[s], c.peers.arch["http://"+s] = a, a
		r := p.NewResource(root, s, "http://"+s, box.Vars, box.Objects, 600, box.Wall.Now, nil)
		r.Register("rec", &vms.ArchivePolicy{Res: a, Vars: box.Vars, Objects: box.Objects, Peers: c.peers, Server: s})
		c.res[s] = r
	}
	return c
}

// free: that server's resource heartbeat, with a disk of our choosing — a test cannot fill one.
func (c *pair) free(t *testing.T, server string, free int64) {
	t.Helper()
	c.res[server].SpaceProbe = func(string) (int64, int64) { return 1_000_000, free }
	if _, err := c.res[server].Heartbeat(); err != nil {
		t.Fatal(err)
	}
}

// writer: a recorder on `server` saying in its heartbeat that it holds `unit` — the only fact Foreign
// needs, and the same one the console reads to find a camera's holder.
func (c *pair) writer(t *testing.T, server, unit string) {
	t.Helper()
	raw, _ := json.Marshal(map[string]any{"worker": "r-" + server, "ts": c.box.Wall.Now(),
		"status": []any{map[string]any{"id": unit, "phase": "running"}}, "server": server})
	if err := c.box.Objects.Put("rec/heartbeats/r-"+server, raw); err != nil {
		t.Fatal(err)
	}
}

func TestAServerThatNeedsRoomSendsBackWhatItWroteForANeighbour(t *testing.T) {
	// The whole point of having no evacuation button. srv-b wrote camera 1 while srv-a was away; here the
	// recording is already back on srv-a. Nothing happens while srv-b has room — the console merges
	// timelines across resources, so the footage is neither lost nor in the way. It becomes work only when
	// srv-b's own disk goes over the high mark, and then srv-b, the server that needs the space, acts.
	c := twoServers(t)
	now := c.box.Wall.Now()
	for i := 0; i < 4; i++ { // what srv-b wrote for srv-a: four segments of 50 kB, under srv-b's epoch
		layDown(t, c.arch["srv-b"].Root, "1", 5, now-4000+float64(i)*600, 50_000)
	}
	first := vms.NewManifest(c.arch["srv-b"].Root, "1").Read()[0].Path
	c.writer(t, "srv-a", "1") // the recorder writing camera 1 is on srv-a now

	if away := vms.Foreign(c.arch["srv-b"], c.box.Objects, "srv-b", now, 45); len(away) != 1 || away["1"] != "srv-a" {
		t.Fatal(away)
	}
	if away := vms.Foreign(c.arch["srv-b"], c.box.Objects, "srv-a", now, 45); len(away) != 0 {
		t.Fatal("from srv-a's side it is nobody's business:", away) // the same tree, the other server's question
	}

	c.free(t, "srv-a", 500_000)
	c.free(t, "srv-b", 500_000) // both have room: nothing to do, and that is the answer
	if rep := c.res["srv-b"].Relieve(); rep["space"] != "ok" {
		t.Fatal(rep)
	}
	if n := len(vms.NewManifest(c.arch["srv-b"].Root, "1").Read()); n != 4 {
		t.Fatal(n)
	}
	if _, err := os.Stat(filepath.Join(c.arch["srv-a"].Root, "rec", "1")); err == nil {
		t.Fatal("srv-a was handed footage nobody was short of room for")
	}

	c.res["srv-b"].SpaceProbe = func(string) (int64, int64) { return 1_000_000, 100_000 } // 90 % full
	rep := c.res["srv-b"].Relieve()
	if rep["space"] != "over" || rep["need"] != int64(150_000) {
		t.Fatal(rep)
	}
	if rep["rec.evacuated"] != 3 || rep["freed"] != int64(150_000) || rep["short"] != int64(0) {
		t.Fatal(rep)
	}
	if n := len(vms.NewManifest(c.arch["srv-b"].Root, "1").Read()); n != 1 { // gone from here: file AND line
		t.Fatal(n)
	}
	if _, err := os.Stat(filepath.Join(c.arch["srv-b"].Root, first)); err == nil {
		t.Fatal("the line went and the file stayed")
	}
	there := vms.NewManifest(c.arch["srv-a"].Root, "1").Read()
	if len(there) != 3 {
		t.Fatal(there)
	}
	for _, s := range there {
		if s.Epoch != 5 { // it arrived under the epoch srv-b wrote it: the epoch says who WROTE it, never where it lies
			t.Fatal(s)
		}
		if _, err := os.Stat(filepath.Join(c.arch["srv-a"].Root, s.Path)); err != nil {
			t.Fatal(err)
		}
	}
}

func TestADestinationWithNoRoomIsNotWhereTheProblemGoes(t *testing.T) {
	// Two tight servers must not trade gigabytes. The destination's free space comes from its own
	// heartbeat and is read BEFORE anything is sent; a destination that cannot take the batch is skipped,
	// and the floor answers instead — which here means a shortfall, because an hour of footage is under
	// the three-day floor and the honest answer is a number rather than a quiet cut.
	c := twoServers(t)
	now := c.box.Wall.Now()
	for i := 0; i < 4; i++ {
		layDown(t, c.arch["srv-b"].Root, "1", 5, now-4000+float64(i)*600, 50_000)
	}
	c.writer(t, "srv-a", "1")
	c.free(t, "srv-a", 1_000) // srv-a is tight too
	c.free(t, "srv-b", 500_000)
	c.res["srv-b"].SpaceProbe = func(string) (int64, int64) { return 1_000_000, 100_000 }

	rep := c.res["srv-b"].Relieve()
	skipped, _ := rep["rec.skipped"].(map[string]string)
	if len(skipped) != 1 || skipped["1"] != "srv-a has no room" || rep["rec.evacuated"] != 0 {
		t.Fatal(rep)
	}
	if n := len(vms.NewManifest(c.arch["srv-b"].Root, "1").Read()); n != 4 { // nothing sent, nothing deleted
		t.Fatal(n)
	}
	if rep["rec.cut"] != 0 || rep["rec.shortfall"] != int64(150_000) || rep["short"] != int64(150_000) {
		t.Fatal(rep)
	}
}

func TestNothingIsDeletedOnA204Alone(t *testing.T) {
	// The deletion follows an OBSERVED fact, not an answer. If the destination cannot be asked what it
	// holds, the segments stay here — a copy that may not have arrived is a copy we still have. The next
	// pass asks again, and the cost of being wrong the other way is footage that exists nowhere.
	c := twoServers(t)
	now := c.box.Wall.Now()
	for i := 0; i < 4; i++ {
		layDown(t, c.arch["srv-b"].Root, "1", 5, now-4000+float64(i)*600, 50_000)
	}
	c.writer(t, "srv-a", "1")
	c.free(t, "srv-a", 500_000)
	c.free(t, "srv-b", 500_000)
	c.peers.mute = true // takes the bytes, says 204, and then goes quiet when asked what it has
	c.res["srv-b"].SpaceProbe = func(string) (int64, int64) { return 1_000_000, 100_000 }

	rep := c.res["srv-b"].Relieve()
	if rep["rec.evacuated"] != 0 || rep["freed"] != int64(0) {
		t.Fatal(rep)
	}
	segs := vms.NewManifest(c.arch["srv-b"].Root, "1").Read()
	if len(segs) != 4 { // still here, file and line
		t.Fatal(len(segs))
	}
	for _, s := range segs {
		if _, err := os.Stat(filepath.Join(c.arch["srv-b"].Root, s.Path)); err != nil {
			t.Fatal(err)
		}
	}
}

func TestOverTheFloorTheDeepestUnitGivesUpItsOldest(t *testing.T) {
	// Nothing foreign, nothing to evacuate: the cut. Not the oldest segments on the disk — that empties
	// the camera with the longest retention, which is the one that was paid for. The choice is made again
	// after every deletion, so the loss spreads instead of falling on one camera.
	box := testbox.NewBox()
	arch := vms.NewArchiveResource(box.Spool, box.Archive, 600, box.Wall.Now)
	now := box.Wall.Now()
	for i := 0; i < 10; i++ { // camera 1: ten days deep
		layDown(t, box.Archive, "1", 1, now-float64(10-i)*86400, 50_000)
	}
	for i := 0; i < 4; i++ { // camera 2: four
		layDown(t, box.Archive, "2", 1, now-float64(4-i)*86400, 50_000)
	}

	freed, removed := vms.Cut(arch, 150_000, now, 3)
	if freed != 150_000 || removed != 3 {
		t.Fatal(freed, removed)
	}
	if n1, n2 := len(vms.NewManifest(box.Archive, "1").Read()), len(vms.NewManifest(box.Archive, "2").Read()); n1 != 7 || n2 != 4 {
		t.Fatal(n1, n2) // the deepest gave up its oldest, three times; the shallow one was not touched
	}
	if d := math.Round(vms.DepthDays(arch, "1", now)); d != 7 {
		t.Fatal(d)
	}

	freed, _ = vms.Cut(arch, 10_000_000, now, 3) // more than there is above the floor
	if freed >= 10_000_000 {
		t.Fatal(freed)
	}
	if d1, d2 := vms.DepthDays(arch, "1", now), vms.DepthDays(arch, "2", now); d1 > 4 || d2 > 4 {
		t.Fatal(d1, d2) // both at the floor, neither below it
	}
}

func TestADrainingDestinationIsNotHandedGigabytesFirst(t *testing.T) {
	// The two halves of an upgrade meet here. srv-a is about to stop, and the operator has said so; the
	// last thing it needs is a neighbour pushing an archive onto its disks minutes before the power goes.
	// The evacuation reads the same row every subsystem reads and skips that destination — one fact, one
	// row, and no new word in either mechanism.
	c := twoServers(t)
	now := c.box.Wall.Now()
	for i := 0; i < 4; i++ {
		layDown(t, c.arch["srv-b"].Root, "1", 5, now-4000+float64(i)*600, 50_000)
	}
	c.writer(t, "srv-a", "1")
	c.free(t, "srv-a", 500_000) // room enough, and it would be evacuated to but for the drain
	c.free(t, "srv-b", 500_000)
	c.res["srv-b"].SpaceProbe = func(string) (int64, int64) { return 1_000_000, 100_000 }
	con := p.NewSpecController(vms.RecSpec, c.box.Vars.AsWriter("console", vms.RecSpec.ACLConsole()...),
		c.box.Objects, 0, c.box.Wall.Now, "")
	if err := con.Drain("srv-a"); err != nil {
		t.Fatal(err)
	}

	rep := c.res["srv-b"].Relieve()
	skipped, _ := rep["rec.skipped"].(map[string]string)
	if len(skipped) != 1 || skipped["1"] != "srv-a draining" || rep["rec.evacuated"] != 0 {
		t.Fatal(rep)
	}
	if n := len(vms.NewManifest(c.arch["srv-b"].Root, "1").Read()); n != 4 { // nothing sent, nothing deleted
		t.Fatal(n)
	}

	if err := con.Undrain(); err != nil { // and once it is back, the same pass moves it
		t.Fatal(err)
	}
	if rep = c.res["srv-b"].Relieve(); rep["rec.evacuated"] != 3 || rep["short"] != int64(0) {
		t.Fatal(rep)
	}
}
