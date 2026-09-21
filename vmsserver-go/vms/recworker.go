package vms

// vmsrecorder — the fourth subsystem's worker: the only one placed on top of
// the archive. A recorder is a worker in the platform's sense (a slot claimed
// by CAS — r-1 — an assignment read from the store, an epoch per unit, a
// heartbeat with capacity and headroom) whose unit is one camera's RECORDING,
// rec/recordings/<cam>. It does not hold the camera: it subscribes to the tee
// of whichever VMS worker does — found in the VMS heartbeat, never by calling
// a worker, never a second connection to the camera — and writes footage into
// rec/<cam>/e<epoch>/ on ITS server's archive, with the manifest beside it.
//
// Two branches for two distances: on the worker's own server the source is
// its shared-memory branch (`live_shm`, shm://…) — the same bytes, no RTSP
// hop; from another server its RTSP fan-out (`live_url`). The rec controller
// prefers the two together (`near: vms`, an affinity); the recorder picks by
// where the worker is. The worker may be anywhere (`servers: shared`); the
// recorder must be where the disks are — `requires: resource`, `servers:
// distinct` by default — and when its server dies the controller moves its
// recordings to a server whose resource answers.
//
//	RECORDER_NAME / SLOT_INDEX         -> the slot to claim: r-<index>
//	SERVER_NAME (or the hostname)      -> `server`: whose archive it writes into, and whose shared memory it may read
//	SPOOL, ARCHIVE                     -> the archive resource's two roots on this server
//	CAPACITY                           -> recordings this server's disks and NIC can take — its own number

import (
	"log"
	"sort"
	"strconv"
	"strings"

	p "vmsserver/w2cplatform"
)

var REC = p.Subsystem{Name: "rec"}

// RecWorker: VmsWorker over rec/recordings/*, its pipelines fed by the
// worker's tee, its segments promoted into this server's archive.
type RecWorker struct {
	*VmsWorker
	Archive      *ArchiveResource
	GraceSeconds float64
	Promoted     int
	Waiting      map[string]bool   // units with nobody holding their camera
	Sources      map[string]string // what each running pipeline subscribed to
	// Backfill (Lesson 16): the hours in LOCAL time it may run in, how far back
	// it may reach, how fresh it must NOT touch, and the seam tolerance that
	// stops 144 seams a day from looking like 144 gaps.
	Window         [2]int // {0,0}: any hour
	KeepDays       float64
	Settle         float64
	SpaceProbe     func(root string) (int64, int64) // the disk under the archive; a test cannot fill one
	Stitch         float64
	BackfillBudget int // ranges per pass; 0 = only what an operator asks for
	Backfilled     int
	// Fetched: request ids this recorder has fetched — the heartbeat carries them, the console removes the
	// rows. Closed: ranges promoted from a device, `<unit>|<from>|<to>`, for the console to scan.
	Fetched []string
	Closed  []string
}

// ClosedReported: how many closed ranges the heartbeat carries. A window and not a queue: the console acts on
// what it sees, and a range that scrolled out was either acted on or is gone — which is why the console's
// decision has to be idempotent on its own (it is: the job's id is the range).
const ClosedReported = 32

func NewRecWorker(name string, vars p.Variables, objects p.ObjectStore, act Actuator, archive *ArchiveResource, o VmsWorkerOptions) (*RecWorker, error) {
	env := o.Env
	if archive == nil {
		spool, root := env.Get("SPOOL"), env.Get("ARCHIVE")
		if spool == "" {
			spool = "/data/spool"
		}
		if root == "" {
			root = "/data/archive"
		}
		archive = NewArchiveResource(spool, root, 600, nil)
	}
	sub := REC
	o.Sub, o.RowsName, o.ParseRow, o.NameEnv, o.Prefix = &sub, "recordings", RecRow, "RECORDER_NAME", "r"
	o.ArchiveRoot = archive.Root
	w, err := NewVmsWorker(name, vars, objects, act, o)
	if err != nil {
		return nil, err
	}
	r := &RecWorker{VmsWorker: w, Archive: archive, GraceSeconds: 30, Waiting: map[string]bool{}, Sources: map[string]string{},
		KeepDays: 30, Settle: 900, Stitch: 2, SpaceProbe: p.DiskSpace}
	w.Enrich, w.StatusExtra, w.BeforePass, w.AfterPump, w.StatusFix = r.enrich, r.statusExtra, func() { r.Resubscribe() }, r.afterPump, r.statusFix
	w.HeartbeatFix = r.heartbeatFix
	for _, pth := range archive.ClosedInSpool(r.GraceSeconds, w.Wall()) { // what the last instance closed but did not promote
		archive.Promote(pth, 0, "live")
		r.Promoted++
	}
	return r, nil
}

// Source: (server, source) of the worker holding the camera, from its heartbeat; "" if nobody does. The
// source is the worker's shared-memory branch when that worker is on THIS server, its RTSP fan-out otherwise.
// Source: (server, source) of the worker holding the camera, from its
// HEARTBEAT — never a call to the worker. The source is that worker's
// shared-memory branch (live_shm) when it is on THIS server — the same bytes
// with no RTSP hop, no fan-out process on the recording path — and its RTSP
// fan-out (live_url) otherwise. A holder that has gone silent is no answer:
// that is what the catalogue's freshness filter is for.
func (r *RecWorker) Source(cam string) (server, source string) {
	h, ok := p.HolderOf(r.Objects, "vms/", cam, r.Wall(), p.HolderQuery{Phase: "running", Field: "live_url"})
	if !ok {
		return "", ""
	}
	server = h.HB.ExtraString("server", "?")
	if server == r.Server && p.Str(h.Status["live_shm"]) != "" {
		return server, p.Str(h.Status["live_shm"])
	}
	return server, p.Str(h.Status["live_url"])
}

func via(source string) string {
	if strings.HasPrefix(source, "shm://") {
		return "shm"
	}
	return "rtsp"
}

// A recording's pipeline needs a source. No source (the camera is held by nobody yet) means "cannot start
// now": the reconciler backs off and retries, and the status says `waiting`.
func (r *RecWorker) enrich(cam Camera) (Camera, bool) {
	// Two identities in three lines: cam.Cam is WHOSE fan-out to subscribe to, cam.ID
	// is WHICH recording is subscribing. `id: cam` makes them equal today; nothing
	// here would change if it stopped.
	server, src := r.Source(cam.Cam)
	if src == "" {
		r.Waiting[cam.ID] = true
		return cam, false
	}
	delete(r.Waiting, cam.ID)
	r.Sources[cam.ID] = src
	cam.Source, cam.SourceServer, cam.Via, cam.Spool, cam.Archive = src, server, via(src), r.Archive.Spool, r.Archive.Root
	return cam, true
}

func (r *RecWorker) statusExtra(cam Camera) map[string]any {
	_, src := r.Source(cam.Cam)
	out := map[string]any{"cam": cam.Cam, "source": nil, "via": nil}
	if src != "" {
		out["source"], out["via"] = src, via(src)
	}
	if _, running := r.Reconciler.Actual[cam.ID]; r.Waiting[cam.ID] && !running {
		out["why"] = "camera held by nobody"
	}
	return out
}

// heartbeatFix: how deep the spool is — closed segments this recorder has not promoted yet. No grace: the
// question the drain route asks is "is anything unwritten", and a segment closed a second ago counts.
//
// `fetched`: the requests this recorder has closed. It cannot delete the rows — a worker writes no
// configuration — so it says which ones are done and the console removes them.
func (r *RecWorker) heartbeatFix(extra map[string]any) {
	extra["spool"] = len(r.Archive.ClosedInSpool(0, r.Wall()))
	extra["fetched"] = strings.Join(tail(r.Fetched, 32), ",")
	extra["closed"] = strings.Join(r.Closed, ",")
}

func tail(xs []string, n int) []string {
	if len(xs) > n {
		return xs[len(xs)-n:]
	}
	return xs
}

func (r *RecWorker) statusFix(st []map[string]any) {
	for _, s := range st {
		id := p.Str(s["id"])
		if s["phase"] != "running" && r.Waiting[id] && s["enabled"] == true {
			s["phase"] = "waiting"
		}
	}
}

// Resubscribe: the camera's worker moved — the source is another server's fan-out now, or, if it moved
// HERE, the shared-memory branch. The pipeline reading the old source is stopped and counted lost, so the
// reconciler starts it again on the new one, under a new rec epoch (a start is a new writer).
func (r *RecWorker) Resubscribe() []string {
	// Walked over the ROWS, not over Actual, because the move is about a camera and
	// the row is the only thing that knows which camera a recording records.
	rows := map[string]Camera{}
	ids := []string{}
	for _, row := range r.Rows {
		if _, running := r.Reconciler.Actual[row.ID]; running {
			rows[row.ID] = row
			ids = append(ids, row.ID)
		}
	}
	sort.Strings(ids)
	moved := []string{}
	for _, cid := range ids {
		_, src := r.Source(rows[cid].Cam)
		if src != "" && r.Sources[cid] != "" && r.Sources[cid] != src {
			r.Act.Actuate("stop", Camera{ID: cid})
			r.Reconciler.Lost(cid, r.Now())
			delete(r.Sources, cid)
			moved = append(moved, cid)
			log.Printf("%s: camera %s is held elsewhere now (%s): re-subscribing", r.Name, rows[cid].Cam, src)
		}
	}
	return moved
}

// afterPump: promote what the pipelines closed, and — only if an operator set a
// budget — spend it on the gaps. Bounded, and inside the window: it shares the
// device's uplink with live.
func (r *RecWorker) afterPump() {
	r.PromoteClosed()
	r.Requests(2) // what a person asked for: outside the budget and the hour
	if r.BackfillBudget > 0 {
		r.Backfill(r.BackfillBudget, 0, false)
	}
}

func (r *RecWorker) PromoteClosed() int {
	n := 0
	for _, pth := range r.Archive.ClosedInSpool(r.GraceSeconds, r.Wall()) {
		r.Archive.Promote(pth, 0, "live")
		n++
	}
	r.Promoted += n
	return n
}

func (r *RecWorker) MetricsText() string {
	return "# TYPE rec_recordings_running gauge\nrec_recordings_running " + strconv.Itoa(len(r.Reconciler.Actual)) + "\n" +
		"# TYPE rec_segments_promoted counter\nrec_segments_promoted " + strconv.Itoa(r.Promoted) + "\n" +
		"# TYPE rec_segments_backfilled counter\nrec_segments_backfilled " + strconv.Itoa(r.Backfilled) + "\n"
}
