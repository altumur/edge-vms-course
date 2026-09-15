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
//	RECORDER_NAME / NOMAD_ALLOC_INDEX  -> the slot to claim: r-<index>
//	NOMAD_NODE_NAME (or the hostname)  -> `server`: whose archive it writes into, and whose shared memory it may read
//	SPOOL, ARCHIVE                     -> the archive resource's two roots on this server
//	CAPACITY                           -> recordings this server's disks and NIC can take — its own number

import (
	"log"
	"sort"
	"strconv"
	"strings"

	p "vmsserver/psimplatform"
)

var REC = p.Subsystem{Name: "rec"}

// RecWorker: VmsWorker over rec/recordings/*, its pipelines fed by the
// worker's tee, its segments promoted into this server's archive.
type RecWorker struct {
	*VmsWorker
	Archive      *ArchiveResource
	GraceSeconds float64
	Promoted     int
	Waiting      map[int]bool
	Sources      map[int]string // what each running pipeline subscribed to
}

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
	r := &RecWorker{VmsWorker: w, Archive: archive, GraceSeconds: 30, Waiting: map[int]bool{}, Sources: map[int]string{}}
	w.Enrich, w.StatusExtra, w.BeforePass, w.AfterPump, w.StatusFix = r.enrich, r.statusExtra, func() { r.Resubscribe() }, func() { r.PromoteClosed() }, r.statusFix
	for _, pth := range archive.ClosedInSpool(r.GraceSeconds, w.Wall()) { // what the last instance closed but did not promote
		archive.Promote(pth, 0)
		r.Promoted++
	}
	return r, nil
}

// Source: (server, source) of the worker holding the camera, from its heartbeat; "" if nobody does. The
// source is the worker's shared-memory branch when that worker is on THIS server, its RTSP fan-out otherwise.
func (r *RecWorker) Source(cam int) (server, source string) {
	hbs := p.Heartbeats(r.Objects, "vms/")
	names := []string{}
	for w := range hbs {
		names = append(names, w)
	}
	sort.Strings(names)
	for _, w := range names {
		for _, st := range hbs[w].Status {
			if p.Str(st["id"]) == strconv.Itoa(cam) && p.Str(st["phase"]) == "running" && p.Str(st["live_url"]) != "" {
				server = hbs[w].ExtraString("server", "?")
				if server == r.Server && p.Str(st["live_shm"]) != "" {
					return server, p.Str(st["live_shm"])
				}
				return server, p.Str(st["live_url"])
			}
		}
	}
	return "", ""
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
	server, src := r.Source(cam.ID)
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
	_, src := r.Source(cam.ID)
	out := map[string]any{"cam": strconv.Itoa(cam.ID), "source": nil, "via": nil}
	if src != "" {
		out["source"], out["via"] = src, via(src)
	}
	if _, running := r.Reconciler.Actual[cam.ID]; r.Waiting[cam.ID] && !running {
		out["why"] = "camera held by nobody"
	}
	return out
}

func (r *RecWorker) statusFix(st []map[string]any) {
	for _, s := range st {
		id, _ := s["id"].(int)
		if s["phase"] != "running" && r.Waiting[id] && s["enabled"] == true {
			s["phase"] = "waiting"
		}
	}
}

// Resubscribe: the camera's worker moved — the source is another server's fan-out now, or, if it moved
// HERE, the shared-memory branch. The pipeline reading the old source is stopped and counted lost, so the
// reconciler starts it again on the new one, under a new rec epoch (a start is a new writer).
func (r *RecWorker) Resubscribe() []int {
	moved := []int{}
	ids := []int{}
	for cid := range r.Reconciler.Actual {
		ids = append(ids, cid)
	}
	sort.Ints(ids)
	for _, cid := range ids {
		_, src := r.Source(cid)
		if src != "" && r.Sources[cid] != "" && r.Sources[cid] != src {
			r.Act.Actuate("stop", Camera{ID: cid})
			r.Reconciler.Lost(cid, r.Now())
			delete(r.Sources, cid)
			moved = append(moved, cid)
			log.Printf("%s: camera %d is held elsewhere now (%s): re-subscribing", r.Name, cid, src)
		}
	}
	return moved
}

func (r *RecWorker) PromoteClosed() int {
	n := 0
	for _, pth := range r.Archive.ClosedInSpool(r.GraceSeconds, r.Wall()) {
		r.Archive.Promote(pth, 0)
		n++
	}
	r.Promoted += n
	return n
}

func (r *RecWorker) MetricsText() string {
	return "# TYPE rec_recordings_running gauge\nrec_recordings_running " + strconv.Itoa(len(r.Reconciler.Actual)) + "\n" +
		"# TYPE rec_segments_promoted counter\nrec_segments_promoted " + strconv.Itoa(r.Promoted) + "\n"
}
