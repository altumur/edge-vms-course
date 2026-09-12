package cluster

// The recorder under a scheduler: М9's AppHost plus what М11 adds.
//
//	Prologue      identity → migrate → rehydrate → epoch by CAS → lease
//	publish       every second: publish on change, with a floor           (Lesson 3)
//	lease         renew by reading my epoch; fence myself if it moved      (Lesson 4)
//	heartbeat     a timestamp in an OBJECT, for node_failover_seconds      (Lesson 4)
//	reindex       files back into rows: the fenced instance's footage      (Lesson 4)
//
// What М9 does — reconcile, report — is the same loop (recorder/reconciler,
// imported) driven by goroutines and tickers instead of asyncio tasks.
// Only the actuator's gate and the report grow: a fenced instance starts
// nothing, and `replicated` joins the conditions.

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"sort"
	"strconv"
	"sync"
	"time"

	"recorder/reconciler"
)

type desired struct {
	mu   sync.Mutex
	rows []CameraRow
	byID map[int64]CameraRow
}

func (d *desired) set(rows []CameraRow) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.rows = rows
	d.byID = make(map[int64]CameraRow, len(rows))
	for _, r := range rows {
		d.byID[r.ID] = r
	}
}

func (d *desired) Desired() []reconciler.Camera {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]reconciler.Camera, 0, len(d.rows))
	for _, r := range d.rows {
		out = append(out, reconciler.Camera{ID: r.ID, Enabled: r.Enabled, Revision: r.Revision})
	}
	return out
}

func (d *desired) Rows() []CameraRow {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]CameraRow(nil), d.rows...)
}

type ClusterAppHost struct {
	Settings Settings
	Store    Store
	Vars     Variables
	Objects  ObjectStore
	Identity Identity
	Actuator Actuator
	Clock    Clock          // monotonic
	Wall     func() float64 // unix seconds

	Desired          *desired
	Reconciler       *reconciler.Reconciler
	Publisher        *Publisher
	Directory        *Directory
	Lease            *Lease
	Restore          *RestoreResult
	Failover         map[string]float64 // last, worst — node_failover_seconds
	RecordingAllowed bool
	ReplicatedNow    bool
	Passes           int

	mu           sync.Mutex // guards the loop-shared fields above during Run
	startedAt    float64
	resumeFromTS float64 // > 0 while a restore waits for its first start
	wake         chan struct{}
}

func NewClusterAppHost(settings Settings, store Store, v Variables, objects ObjectStore, id Identity, act Actuator,
	clock Clock, wall func() float64) *ClusterAppHost {
	if clock == nil {
		clock = Monotonic()
	}
	if wall == nil {
		wall = func() float64 { return float64(time.Now().UnixNano()) / 1e9 }
	}
	h := &ClusterAppHost{Settings: settings, Store: store, Vars: v, Objects: objects, Identity: id, Actuator: act,
		Clock: clock, Wall: wall, Desired: &desired{}, Failover: map[string]float64{}, RecordingAllowed: true,
		wake: make(chan struct{}, 1)}
	h.Reconciler = reconciler.New(h.Desired, h.actuate)
	h.Reconciler.MaxBackoff, h.Reconciler.StallFailures = settings.MaxBackoff, settings.StallFailures
	h.Publisher = NewPublisher(id.recorder, store, v, objects, settings.PublishFloor, clock)
	h.Directory = NewDirectory(v, 5, clock)
	h.startedAt = clock()
	act.SetSettings(settings)
	return h
}

// -- prologue: the six steps ------------------------------------------------

func (h *ClusterAppHost) Prologue() (RestoreResult, error) {
	node := h.Identity.recorder
	r, err := Rehydrate(h.Identity, h.Store, h.Objects) // steps 2–4
	if err != nil {
		return r, err
	}
	h.Restore = &r
	h.Publisher.PublishedRev = h.Identity.ConfigRevision
	hb := h.readHeartbeat()
	epoch, idx, err := NextEpoch(h.Vars, node) // step 5
	if err != nil {
		return r, err
	}
	h.Settings.Epoch = epoch           // step 6: the epoch in the path
	h.Actuator.SetSettings(h.Settings) // in every new segment path
	h.Lease = NewLease(h.Vars, node, epoch, h.Settings.LeaseTTL, h.Settings.LeaseMargin, h.Clock)
	if fo, _, err := h.Vars.Get("nodes/" + node + "/failover"); err == nil {
		for k, v := range fo {
			h.Failover[k], _ = strconv.ParseFloat(v, 64)
		}
	}
	if r.State == "restored" && hb != nil && hb.TS > 0 {
		h.resumeFromTS = hb.TS // the old instance's last sign of life
	}
	log.Printf("%s: %s (rev %d, %d cameras); epoch %d (ModifyIndex %d)", node, r.State, r.Revision, r.Cameras, epoch, idx)
	return r, nil
}

// -- gates ----------------------------------------------------------------------

func (h *ClusterAppHost) actuate(verb string, cam reconciler.Camera) bool {
	if verb == "start" || verb == "restart" {
		if !h.RecordingAllowed { // storage unavailable is a REASON, not a phase
			return false
		}
		if h.Lease != nil && !h.Lease.MayWrite() { // fenced, or the lease ran out: start nothing
			return false
		}
	}
	h.Desired.mu.Lock()
	row := h.Desired.byID[cam.ID]
	h.Desired.mu.Unlock()
	row.ID = cam.ID
	return h.Actuator.Actuate(verb, row)
}

func (h *ClusterAppHost) now() float64 { return h.Clock() - h.startedAt }

func (h *ClusterAppHost) ReconcileOnce() ([]reconciler.Action, error) {
	rows, err := h.Store.FetchDesired()
	if err != nil {
		return nil, err
	}
	h.Desired.set(rows)
	h.mu.Lock()
	defer h.mu.Unlock()
	actions := h.Reconciler.Reconcile(h.now())
	h.Passes++
	started := false
	for _, a := range actions {
		log.Printf("reconcile: %s camera %d", a.Verb, a.ID)
		started = started || a.Verb == "start"
	}
	if h.resumeFromTS > 0 && started {
		secs := h.Wall() - h.resumeFromTS
		if secs < 0 {
			secs = 0
		}
		worst := h.Failover["worst"]
		if secs > worst {
			worst = secs
		}
		h.Failover = map[string]float64{"last": secs, "worst": worst}
		h.resumeFromTS = 0
		path := "nodes/" + h.Identity.recorder + "/failover"
		if _, idx, err := h.Vars.Get(path); err == nil {
			_, err = h.Vars.Put(path, Items{"last": fmtF(secs), "worst": fmtF(worst)}, idx)
			if err != nil {
				log.Printf("could not record failover time: %v", err)
			}
		}
		log.Printf("%s: recording resumed %.1fs after the old instance's last heartbeat (worst %.1fs)", h.Identity.recorder, secs, worst)
	}
	return actions, nil
}

func fmtF(f float64) string { return strconv.FormatFloat(f, 'f', 1, 64) }

// phases: camera id -> (observed_revision, phase). Phase is a POSITION.
func (h *ClusterAppHost) phases() []StatusRow {
	actual, failures := h.Reconciler.Actual(), h.Reconciler.Failures()
	var out []StatusRow
	for _, cam := range h.Desired.Rows() {
		if !cam.Enabled {
			out = append(out, StatusRow{cam.ID, cam.Revision, "pending"})
			continue
		}
		have, running := actual[cam.ID]
		phase := "pending"
		switch {
		case running:
			phase = "running"
		case failures[cam.ID].N > 0:
			phase = "failed"
		}
		out = append(out, StatusRow{cam.ID, have, phase})
	}
	return out
}

func (h *ClusterAppHost) ReportOnce() error {
	h.mu.Lock()
	defer h.mu.Unlock()
	if err := h.Store.Report(h.phases()); err != nil {
		return err
	}
	rev, err := h.Store.ConfigRevision()
	if err != nil {
		return err
	}
	ok, reason := h.Publisher.Replicated(rev)
	h.ReplicatedNow = ok
	failures := h.Reconciler.Failures()
	for _, cam := range h.Desired.Rows() {
		f, failing := failures[cam.ID]
		fr := ""
		if failing {
			fr = fmt.Sprintf("failing, retry in %.0fs", f.Delay)
		}
		sr := ""
		if !h.RecordingAllowed {
			sr = "disk full; policy=stop_recording"
		}
		for _, c := range []struct {
			name   string
			ok     bool
			reason string
		}{{"camera_reachable", !failing, fr}, {"storage_available", h.RecordingAllowed, sr},
			{"licensed", true, ""}, {"replicated", ok, reason}} {
			if err := h.Store.SetCondition(cam.ID, c.name, c.ok, c.reason); err != nil {
				return err
			}
		}
	}
	return nil
}

// Fence stops everything: this instance's epoch is stale.
func (h *ClusterAppHost) Fence(why string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if !h.RecordingAllowed {
		return
	}
	log.Printf("%s: FENCED (%s). Stopping every pipeline; this instance's epoch %d is stale.", h.Identity.recorder, why, h.Settings.Epoch)
	h.RecordingAllowed = false
	h.Actuator.StopAll()
	h.Reconciler.Clear()
	h.Wake()
}

func (h *ClusterAppHost) Wake() {
	select {
	case h.wake <- struct{}{}:
	default:
	}
}

// -- the heartbeat is an object ------------------------------------------------
//
// Small and FREQUENT and never queried — the three-stores rule (Lesson 2)
// says that is not raft. A thousand recorders writing a raft entry every ten
// seconds is a hundred commits a second replicated to every server; a
// thousand tiny objects a second is nothing to an object store.

type heartbeat struct {
	TS       float64        `json:"ts"`
	Epoch    int64          `json:"epoch"`
	Revision int64          `json:"revision,omitempty"`
	Server   string         `json:"server,omitempty"`
	Cameras  []cameraStatus `json:"cameras,omitempty"`
}

// cameraStatus is the recorder's own /status row, carried in the heartbeat so
// the cluster console builds the camera list without calling any recorder
// (М12, "The camera list"). Server lets a dead server read as one cause.
type cameraStatus struct {
	ID               int64  `json:"id"`
	Name             string `json:"name"`
	Site             string `json:"site"`
	Enabled          bool   `json:"enabled"`
	Phase            string `json:"phase"`
	Revision         int64  `json:"revision"`
	ObservedRevision int64  `json:"observed_revision"`
}

// HeartbeatPayload is the object the heartbeat task writes.
func (h *ClusterAppHost) HeartbeatPayload() heartbeat {
	rows := h.Desired.Rows()
	phases := map[int64]StatusRow{}
	for _, p := range h.phases() {
		phases[p.Camera] = p
	}
	cams := make([]cameraStatus, 0, len(rows))
	for _, r := range rows {
		p := phases[r.ID]
		if p.Phase == "" {
			p.Phase = "pending"
		}
		cams = append(cams, cameraStatus{r.ID, r.Name, r.SiteID, r.Enabled, p.Phase, r.Revision, p.ObservedRevision})
	}
	server := os.Getenv("NOMAD_NODE_ID")
	if server == "" {
		server, _ = os.Hostname()
	}
	return heartbeat{h.Wall(), h.Settings.Epoch, h.Identity.ConfigRevision, server, cams}
}

func (h *ClusterAppHost) readHeartbeat() *heartbeat {
	raw, err := h.Objects.Get(h.Identity.recorder + "/heartbeat")
	if err != nil || len(raw) == 0 {
		return nil
	}
	var hb heartbeat
	if json.Unmarshal(raw, &hb) != nil {
		return nil
	}
	return &hb
}

func (h *ClusterAppHost) heartbeatOnce() {
	if h.Lease == nil || !h.Lease.MayWrite() {
		return
	}
	b, _ := json.Marshal(h.HeartbeatPayload())
	if err := h.Objects.Put(h.Identity.recorder+"/heartbeat", b); err != nil {
		log.Printf("heartbeat write failed (object store unreachable?): %v", err)
	}
}

func (h *ClusterAppHost) leaseOnce() {
	if h.Lease == nil {
		return
	}
	if !h.Lease.Renew() {
		why := "lease expired without renewal"
		if h.Lease.Fenced {
			why = "a newer epoch was issued"
		}
		h.Fence(why)
	}
}

func (h *ClusterAppHost) reindexOnce() {
	rep, err := Sweep(h.Store, RealFs{}, h.Settings.ArchiveDir, h.Settings.Epoch, h.Settings.SegmentSeconds, h.Wall())
	if err != nil {
		log.Printf("reindex sweep failed: %v", err)
	} else if rep.Reindexed > 0 {
		log.Printf("reindex: %d segments, %d from fenced epochs", rep.Reindexed, rep.Fenced)
	}
}

// -- lifecycle ---------------------------------------------------------------

func every(ctx context.Context, wg *sync.WaitGroup, interval time.Duration, wake <-chan struct{}, f func()) {
	wg.Add(1)
	go func() {
		defer wg.Done()
		t := time.NewTicker(interval)
		defer t.Stop()
		for {
			f()
			select {
			case <-ctx.Done():
				return
			case <-t.C:
			case <-wake:
			}
		}
	}()
}

// Run is the process: migrate, the prologue, then one goroutine per
// concern until ctx is cancelled. The timer is correctness; the wake is
// latency.
func (h *ClusterAppHost) Run(ctx context.Context, serveConsole bool) error {
	if ok, err := h.Store.Migrate(""); err != nil {
		return err
	} else if !ok {
		log.Printf("running on the previous schema; the box keeps recording")
	}
	if _, err := h.Prologue(); err != nil {
		return err
	}
	sec := func(f float64) time.Duration { return time.Duration(f * float64(time.Second)) }
	var wg sync.WaitGroup
	never := make(chan struct{})
	every(ctx, &wg, sec(h.Settings.PollInterval), h.wake, func() {
		if _, err := h.ReconcileOnce(); err != nil {
			log.Printf("reconcile pass failed; will retry: %v", err)
		}
	})
	every(ctx, &wg, sec(h.Settings.ReportInterval), never, func() {
		if err := h.ReportOnce(); err != nil {
			log.Printf("report failed; convergence continues regardless: %v", err)
		}
	})
	every(ctx, &wg, time.Second, never, func() { h.Publisher.PublishOnce() })
	interval := (h.Settings.LeaseTTL - h.Settings.LeaseMargin) / 3
	if interval < 1 {
		interval = 1
	}
	every(ctx, &wg, sec(interval), never, h.leaseOnce)
	every(ctx, &wg, sec(h.Settings.HeartbeatInterval), never, h.heartbeatOnce)
	every(ctx, &wg, sec(h.Settings.ReindexInterval), never, h.reindexOnce)
	if serveConsole {
		srv := ServeConsole(h, h.Settings.ConsoleAddr)
		go func() { <-ctx.Done(); srv.Close() }()
	}
	<-ctx.Done()
	log.Printf("stopping: closing pipelines (each finalizes its open segment)")
	h.Actuator.StopAll()
	wg.Wait()
	h.Publisher.PublishOnce() // a last publish, best effort
	return h.ReportOnce()
}

func sortInt64s(a []int64) { sort.Slice(a, func(i, j int) bool { return a[i] < a[j] }) }
