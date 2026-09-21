package vms

// vmsworker — DriverPack as the worker. One process, N pipelines, its own
// loop. It reads its assignment (vms/workers/<me>) and the camera rows it
// names, runs М9's reconcile loop over them, takes an epoch per camera by
// CAS when it starts one, holds a lease per camera, and publishes a
// heartbeat carrying its status. It never writes configuration.
//
// It HOLDS a camera: one connection, one epoch, one fan-out — the tee's
// RTP branch re-served as rtsp://<server>:8554/<cam> (`live_url`) for
// subscribers on any server, and its shmsink branch <ShmDir>/<cam>.shm
// (`live_shm`) for subscribers on this one — and writes the camera's
// events into vms/<cam>/e<epoch>/ on its server's resource. It records
// nothing: footage is the recorder's (recorder.go), a subscriber like the
// gateway and the detectors. The same class runs the recorder over the
// rec rows: Sub, RowsName, ParseRow and the hooks are what differ.
//
// What the environment hands a process, on a box or in an allocation:
//
//	WORKER_NAME / SLOT_INDEX          -> the slot to claim: w-<index>. The index is the preference;
//	                                    the claim (CAS on vms/slots/w-N) is the proof
//	SERVER_NAME (or the hostname)     -> `server` in the heartbeat: which resource it records into
//	LABELS                            -> `labels` in the heartbeat: what this server can reach
//	INSTANCE_ID                       -> the instance; CAPACITY -> the worker's own number.
//	None of these names an orchestrator: see `w2cplatform/runtime.go`.
//
// A worker on a cluster is a worker on a box whose stores happen to be raft.

import (
	"log"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	p "vmsserver/w2cplatform"
)

// Env is a process's environment; nil means the real one. It is the platform's
// type, so the neutral names in `w2cplatform/runtime.go` are the only names
// this package ever reads from an environment.
type Env = p.Env

// SlotFromEnvironment: WORKER_NAME, else `w-<SLOT_INDEX>`, else "" — claim
// whatever is free, a lapsed slot first. Which runtime filled SLOT_INDEX in is
// not this file's business.
func SlotFromEnvironment(env Env) string { return p.SlotName(env, "WORKER_NAME", "w") }

func LabelsFromEnvironment(env Env) []string { return p.LabelsOf(env, "") }

// Posted is what an element posted on the bus about a camera.
type Posted struct {
	Cam    string
	Kind   string
	Fields map[string]any
}

// Actuator builds and tears down pipelines and drains their bus.
type Actuator interface {
	Actuate(verb string, cam Camera) bool
	Pump() (dead []string, posted []Posted)
	StopAll()
}

// FakeActuator is М9 Lesson 6's print(), with a memory. Failing says whose
// start fails. Tests push into Dead and Posted directly.
type FakeActuator struct {
	mu      sync.Mutex
	Failing func(cid string) bool
	Calls   []Action
	Running map[string]bool
	Epochs  map[string]int
	Started map[string]Camera // what each start was given: the enriched row (the fan-out, the recorder's source)
	Dead    []string
	Posted  []Posted
	Fetched []Fetched // what RecordRange was asked for
}

// Fetched is one range the fake was asked to fetch.
type Fetched struct {
	Unit     string
	From, To float64
	URL      string
}

func NewFakeActuator() *FakeActuator {
	return &FakeActuator{Running: map[string]bool{}, Epochs: map[string]int{}, Started: map[string]Camera{}}
}

func FailingSet(ids ...string) func(string) bool {
	set := map[string]bool{}
	for _, i := range ids {
		set[i] = true
	}
	return func(cid string) bool { return set[cid] }
}

func (f *FakeActuator) Actuate(verb string, cam Camera) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.Calls = append(f.Calls, Action{verb, cam.ID})
	if verb == "stop" {
		delete(f.Running, cam.ID)
		return true
	}
	if f.Failing != nil && f.Failing(cam.ID) {
		delete(f.Running, cam.ID)
		return false
	}
	f.Running[cam.ID] = true
	f.Epochs[cam.ID] = cam.Epoch
	f.Started[cam.ID] = cam
	return true
}

func (f *FakeActuator) Pump() ([]string, []Posted) {
	f.mu.Lock()
	defer f.mu.Unlock()
	dead, posted := f.Dead, f.Posted
	f.Dead, f.Posted = nil, nil
	for _, cid := range dead {
		delete(f.Running, cid)
	}
	return dead, posted
}

// Post is what an element would post on the bus.
func (f *FakeActuator) Post(cid string, kind string, fields map[string]any) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.Posted = append(f.Posted, Posted{cid, kind, fields})
}

// RecordRange is the fake's second verb (Lesson 16): fetch a range through a
// playback door and write it into the spool as ordinary segments under our
// epoch — the same shape live recording writes, because it IS the same
// archive. Fetched records what it was asked for, so a test can see the range
// and not just the file.
func (f *FakeActuator) RecordRange(unit, url string, epoch int, t0, t1 float64, spool string) []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	cam, _ := strconv.Atoi(unit)
	out := []string{}
	const seg = 600.0
	for t := t0; t < t1; {
		end := t + seg
		if end > t1 {
			end = t1
		}
		pth := SegmentPath(spool, strconv.Itoa(cam), epoch, time.Unix(int64(t), 0).UTC())
		os.MkdirAll(filepath.Dir(pth), 0o755)
		os.WriteFile(pth, make([]byte, 16), 0o644)
		// the segment ends where it ends: Promote reads mtime
		os.Chtimes(pth, time.Unix(int64(end), 0), time.Unix(int64(end), 0))
		out = append(out, pth)
		f.Fetched = append(f.Fetched, Fetched{unit, t, end, url})
		t = end
	}
	return out
}

func (f *FakeActuator) StopAll() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.Running = map[string]bool{}
}

// RunningIDs is the sorted set of running cameras — the test's view.
func (f *FakeActuator) RunningIDs() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := []string{}
	for cid := range f.Running {
		out = append(out, cid)
	}
	sort.Strings(out)
	return out
}

type Observed struct {
	Cam  string
	T    float64
	Kind string
}

// VmsWorkerOptions: zero values are the defaults.
type VmsWorkerOptions struct {
	p.WorkerOptions
	Server        string
	Capacity      int
	ArchiveRoot   string
	BucketSeconds int
	Env           Env // nil: the process's own
	// the subsystem this worker is a worker OF: the VMS by default; the recorder sets rec/recordings
	Sub      *p.Subsystem
	RowsName string
	ParseRow func(p.Items) Camera
	NameEnv  string // WORKER_NAME (w-<i>) or RECORDER_NAME (r-<i>)
	Prefix   string
	// One session per DEVICE, not per channel: DeviceFactory(key) opens it — a
	// DriverPack session on a box, a FakeDevice in the tests, nil for a source
	// with no archive of its own (a file).
	DeviceFactory func(key string) Device
}

// VmsWorker: name is a slot. Given (systemd's %i, Nomad's alloc index) it
// is claimed by that name; "" means whichever slot is free — a lapsed one
// first, so a replacement inherits its assignment.
type VmsWorker struct {
	*p.Worker
	ArchiveRoot      string
	BucketSeconds    int
	ObservedEvents   []Observed
	Capacity         int // cameras this process can carry: М9 Lesson 7's B + n·I, measured on its server
	Act              Actuator
	Rows             []Camera
	AssignmentRev    int
	Reconciler       *Reconciler
	RecordingAllowed bool
	FencedReason     string
	Server           string
	Labels           []string
	Alloc            string
	StartedAt        float64
	StartedWall      float64
	PreviousHb       float64 // the previous instance of this slot, if it left a heartbeat: what failover is measured from
	PreviousInstance string
	Passes           int
	ShmDir           string
	RowsName         string
	ParseRow         func(p.Items) Camera
	DeviceFactory    func(key string) Device // nil: nothing is held
	Devices          map[string]Device       // key (DeviceOf(source)) -> the open session
	// hooks the recorder fills in: what a pipeline needs beyond the row (nil = cannot start now), what the
	// status says per unit, and what runs before each reconcile pass and after each pump
	Enrich      func(cam Camera) (Camera, bool)
	StatusExtra func(cam Camera) map[string]any
	BeforePass  func()
	AfterPump   func()
	StatusFix   func(st []map[string]any)
	// HeartbeatFix: what this subsystem's worker adds to its OWN heartbeat, beside what every worker says.
	// The recorder publishes how deep its spool is, and the drain route reads it — a machine whose units
	// have all left is still not safe to stop while something it recorded is unwritten.
	HeartbeatFix func(extra map[string]any)
}

func slotFromEnv(env Env, nameEnv, prefix string) string { return p.SlotName(env, nameEnv, prefix) }

// NewVmsWorker: name is a slot. Given (systemd's %i, Nomad's alloc index) it
// is claimed by that name; "" means the environment's, and failing that
// whichever slot is free — a lapsed one first, so a replacement inherits its assignment.
func NewVmsWorker(name string, vars p.Variables, objects p.ObjectStore, act Actuator, o VmsWorkerOptions) (*VmsWorker, error) {
	env := o.Env
	sub, rows, parse, nameEnv, prefix := VMS, "cameras", Row, "WORKER_NAME", "w"
	if o.Sub != nil {
		sub, rows, parse, nameEnv, prefix = *o.Sub, o.RowsName, o.ParseRow, o.NameEnv, o.Prefix
	}
	if name == "" {
		name = slotFromEnv(env, nameEnv, prefix)
	}
	if o.Instance == "" {
		o.Instance = p.InstanceOf(env)
	}
	base := p.NewWorker(sub, vars, objects, o.WorkerOptions)
	if _, err := base.ClaimSlot(name); err != nil {
		return nil, err
	}
	w := &VmsWorker{Worker: base, ArchiveRoot: o.ArchiveRoot, BucketSeconds: o.BucketSeconds, Capacity: o.Capacity,
		Act: act, RecordingAllowed: true, Server: o.Server, Labels: LabelsFromEnvironment(env), Alloc: p.InstanceOf(env),
		RowsName: rows, ParseRow: parse, ShmDir: env.Get("SHM_DIR")}
	if w.ShmDir == "" {
		w.ShmDir = ShmDir
	}
	w.DeviceFactory, w.Devices = o.DeviceFactory, map[string]Device{}
	if w.DeviceFactory == nil {
		w.DeviceFactory = func(string) Device { return nil }
	}
	w.Enrich = w.enrichWorker
	w.StatusExtra = w.statusExtraWorker
	if w.ArchiveRoot == "" {
		w.ArchiveRoot = env.Get("ARCHIVE")
		if w.ArchiveRoot == "" {
			w.ArchiveRoot = "/data/archive"
		}
	}
	if w.BucketSeconds == 0 {
		w.BucketSeconds = 600
	}
	if w.Capacity == 0 {
		w.Capacity, _ = strconv.Atoi(env.Get("CAPACITY"))
		if w.Capacity == 0 {
			w.Capacity = 50
		}
	}
	if w.Act == nil {
		w.Act = NewFakeActuator()
	}
	w.Server = p.ServerOfEnv(env, w.Server)
	w.Reconciler = NewReconciler(w, w.actuate)
	w.StartedAt = w.Clock()
	w.StartedWall = w.Wall()
	if raw, _ := objects.Get(w.Sub.HeartbeatKey(w.Name)); len(raw) > 0 {
		if old, err := p.HeartbeatFromBytes(raw); err == nil && old.ExtraString("instance", "") != w.Instance {
			w.PreviousHb, w.PreviousInstance = old.Ts, old.ExtraString("instance", "")
		}
	}
	return w, nil
}

// Desired is the store, as the reconciler sees it.
// Desired is what the reconciler runs. A row with `live: on-demand` is NOT
// here: its device is still held (see refreshDevices) and its archive still
// served, but no pipeline is built for it. Holding the device is what the row
// buys; the live fan-out is what `live` asks for.
func (w *VmsWorker) Desired() []Camera {
	out := []Camera{}
	for _, r := range w.Rows {
		if r.Live != "on-demand" {
			out = append(out, r)
		}
	}
	return out
}

// Refresh reads the assignment and the rows it names. A fresh worker knows
// nothing and reads everything.
func (w *VmsWorker) Refresh() {
	a := w.Assignment()
	w.AssignmentRev = a.Rev
	rows := []Camera{}
	for _, unit := range a.Units {
		items, _, _ := w.Vars.Get(w.Sub.Config(w.RowsName, unit))
		if items != nil && items["deleted"] != "true" {
			rows = append(rows, w.ParseRow(items))
		}
	}
	w.Rows = rows
	w.refreshDevices()
}

// One connection per device, however many of its channels are assigned: an NVR
// with thirty-two cameras is one session, not thirty-two. A device no row names
// any more is closed.
func (w *VmsWorker) refreshDevices() {
	want := map[string]bool{}
	for _, r := range w.Rows {
		if r.Source != "" { // a recorder's rows name no source
			want[DeviceOf(r.Source)] = true
		}
	}
	for key := range want {
		if _, held := w.Devices[key]; !held {
			if dev := w.DeviceFactory(key); dev != nil {
				w.Devices[key] = dev
			}
		}
	}
	for key, dev := range w.Devices {
		if !want[key] {
			delete(w.Devices, key)
			dev.Close()
		}
	}
}

// DeviceOfRow: the device a camera is a channel of, if it is held here.
func (w *VmsWorker) DeviceOfRow(cam Camera) Device {
	if cam.Source == "" {
		return nil
	}
	return w.Devices[DeviceOf(cam.Source)]
}

// DeviceStatus: what each held device is, and what it has that we have not
// imported. Discovery is an OBSERVATION and goes where observations go — this
// worker's token writes `vms/epoch/*` and `vms/slots/*`, never `vms/cameras/*`.
// The operator imports channels from the page, with their own token.
func (w *VmsWorker) DeviceStatus() []map[string]any {
	known := map[string]map[string]bool{}
	for _, r := range w.Rows {
		if r.Source == "" {
			continue
		}
		key := DeviceOf(r.Source)
		ch := ChannelOf(r.Source)
		if ch == "" {
			ch = r.ID
		}
		if known[key] == nil {
			known[key] = map[string]bool{}
		}
		known[key][ch] = true
	}
	out := []map[string]any{}
	for _, key := range sortedKeys(w.Devices) {
		dev := w.Devices[key]
		have := known[key]
		unimported := []string{}
		for _, c := range dev.Channels() {
			if !have[c] {
				unimported = append(unimported, c)
			}
		}
		out = append(out, map[string]any{"device": key, "channels": len(dev.Channels()),
			"known": sortedKeys(have), "unimported": unimported,
			"playbacks": dev.InUse(), "max_playbacks": dev.MaxPlaybacks()})
	}
	return out
}

// Playback: a range out of the DEVICE's own archive. The ceiling belongs to the
// hardware, not to this worker — capacity here is still counted in cameras, and
// an exhausted device is a 503. On a camera this competes with live for the one
// uplink; on an NVR it usually does not.
func (w *VmsWorker) Playback(cam string, t0, t1 float64) ([]byte, error) {
	var row Camera
	found := false
	for _, r := range w.Rows {
		if r.ID == cam {
			row, found = r, true
			break
		}
	}
	if !found {
		return nil, ErrNoDeviceArchive
	}
	dev := w.DeviceOfRow(row)
	if dev == nil {
		return nil, ErrNoDeviceArchive
	}
	if _, ok := dev.Coverage(cam); !ok {
		return nil, ErrNoDeviceArchive
	}
	sid, err := dev.OpenPlayback(cam, t0, t1) // ErrDeviceBusy when the device is full
	if err != nil {
		return nil, err
	}
	defer dev.ClosePlayback(sid)
	return dev.Read(sid)
}

// Recordings: where the device's footage is, span by span, clipped to [t0, t1). Costs no playback session:
// listing is not reading.
func (w *VmsWorker) Recordings(cam string, t0, t1 float64) ([][2]float64, error) {
	for _, r := range w.Rows {
		if r.ID != cam {
			continue
		}
		dev := w.DeviceOfRow(r)
		if dev == nil {
			return nil, ErrNoDeviceArchive
		}
		if _, ok := dev.Coverage(cam); !ok {
			return nil, ErrNoDeviceArchive
		}
		l, can := dev.(Lister)
		if !can {
			return nil, ErrNoIndex
		}
		spans, ok := l.Recordings(cam, t0, t1)
		if !ok {
			return nil, ErrNoIndex
		}
		return spans, nil
	}
	return nil, ErrNoDeviceArchive
}

// What the pipeline needs beyond the row. The worker's tee: its RTSP fan-out (`live_url`), the loopback
// port the fan-out serves from, and the shared-memory branch (`live_shm`) for subscribers on this server.
func (w *VmsWorker) enrichWorker(cam Camera) (Camera, bool) {
	cam.LiveURL, cam.LivePort, cam.LiveShm = LiveURL(w.Server, cam.ID), LivePort(cam.ID), LiveShm(cam.ID, w.ShmDir)
	return cam, true
}

// What the heartbeat says per unit beyond the platform's fields: the worker publishes where the camera's
// stream is — the fan-out, and the same-server fast path — so a recorder, a gateway or a detector finds it
// by reading, never by calling.
// Two kinds of output: live_url/live_shm — the stream now; playback_url +
// coverage — the archive the DEVICE wrote, which we did not. A subscriber needs
// nothing but this object, for either.
func (w *VmsWorker) statusExtraWorker(cam Camera) map[string]any {
	out := map[string]any{"live_url": LiveURL(w.Server, cam.ID), "live_shm": LiveShm(cam.ID, w.ShmDir)}
	if dev := w.DeviceOfRow(cam); dev != nil {
		if cov, ok := dev.Coverage(cam.ID); ok {
			out["playback_url"] = PlaybackURL(w.Server, cam.ID)
			out["coverage"] = cov.ToMap() // the SUMMARY: from, to, fragments — never the index
			// …and WHERE to ask for the index, which is not the same thing as carrying it. The heartbeat is
			// one object under a ceiling; thirty days of motion recording is thousands of spans. A door,
			// not a field (М10A Lesson 26 made the same choice for a mask).
			out["index_url"] = IndexURL(w.Server, cam.ID)
		}
	}
	return out
}

// the gate
func (w *VmsWorker) actuate(verb string, cam Camera) bool {
	unit := cam.ID
	if verb == "start" || verb == "restart" {
		if !w.RecordingAllowed {
			return false
		}
		if ep, held := w.Epochs[unit]; verb == "start" || !held {
			e, err := w.TakeEpoch(unit) // a new epoch for a new writer
			if err != nil {
				return false
			}
			cam.Epoch = e
		} else {
			cam.Epoch = ep
		}
		if !w.MayWrite(unit) {
			return false
		}
		full, ok := w.Enrich(cam)
		if !ok {
			return false // cannot start now (a recorder whose camera nobody holds): backoff, retry
		}
		return w.Act.Actuate(verb, full)
	}
	ok := w.Act.Actuate("stop", cam)
	w.Release(unit)
	return ok
}

func (w *VmsWorker) Now() float64 { return w.Clock() - w.StartedAt }

func (w *VmsWorker) ReconcileOnce() []Action {
	return w.ReconcileAt(w.Now())
}

func (w *VmsWorker) ReconcileAt(now float64) []Action {
	if w.BeforePass != nil {
		w.BeforePass()
	}
	w.Refresh()
	actions := w.Reconciler.Reconcile(now)
	w.Passes++
	return actions
}

// LeasePass renews every lease. A lost lease on a camera that is no longer
// assigned to me is a reassignment: let it go. A lost lease on a camera that
// IS still mine means another instance of ME took it: I am the zombie, and
// the whole instance fences.
func (w *VmsWorker) LeasePass() []string {
	if !w.RenewSlot() {
		w.Fence("slot " + w.Name + " is held by another instance now")
		var all []string
		for u := range w.Epochs {
			all = append(all, u)
		}
		sort.Strings(all)
		return all
	}
	lost := w.RenewLeases()
	if len(lost) == 0 {
		return lost
	}
	assigned := w.Assignment()
	for _, unit := range lost {
		if !assigned.Has(unit) {
			w.Act.Actuate("stop", Camera{ID: unit})
			delete(w.Reconciler.Actual, unit)
			w.Release(unit)
		} else {
			w.Fence("camera " + unit + ": a newer epoch was issued to another instance of " + w.Name)
			break
		}
	}
	return lost
}

func (w *VmsWorker) Fence(why string) {
	if !w.RecordingAllowed {
		return
	}
	log.Printf("%s: FENCED (%s). Stopping every pipeline.", w.Name, why)
	w.RecordingAllowed, w.FencedReason = false, why
	w.Act.StopAll()
	w.Reconciler.Clear()
}

// Observe: an event, written by this worker, now, into the camera's bucket
// on this server's resource, under the epoch this worker holds for it —
// recording or not. A camera it holds no epoch for is not its to observe.
// Returns "" when it was not.
func (w *VmsWorker) Observe(cid string, kind string, fields map[string]any) string {
	epoch, ok := w.Epochs[cid]
	if !ok || !w.RecordingAllowed {
		return ""
	}
	t := w.Wall()
	w.ObservedEvents = append(w.ObservedEvents, Observed{cid, t, kind})
	path, err := EventLogFor(w.ArchiveRoot, cid, epoch, w.BucketSeconds).Append(t, kind, fields)
	if err != nil {
		return ""
	}
	return path
}

// PumpOnce: the bus, drained — what elements posted becomes events (if I
// still hold the epoch), and what died becomes Lost and a `silent` event.
func (w *VmsWorker) PumpOnce() {
	dead, posted := w.Act.Pump()
	for _, ps := range posted {
		w.Observe(ps.Cam, ps.Kind, ps.Fields)
	}
	for _, cid := range dead {
		w.Reconciler.Lost(cid, w.Now())
		w.Observe(cid, "silent", nil) // the event with no segment open, by definition
	}
	if w.AfterPump != nil {
		w.AfterPump()
	}
}

func (w *VmsWorker) Status() []map[string]any {
	st := w.Reconciler.Status()
	out := []map[string]any{}
	for _, cam := range w.Rows {
		pos, ok := st[cam.ID]
		if !ok {
			pos = Position{Converged, 0}
		}
		_, running := w.Reconciler.Actual[cam.ID]
		phase := "pending"
		switch {
		case running:
			phase = "running"
		case !cam.Enabled:
			phase = "pending"
		case w.Reconciler.Failures[cam.ID] != nil:
			phase = "failed"
		}
		st := map[string]any{"id": cam.ID, "ref": cam.Ref, "name": cam.Name, "enabled": cam.Enabled, "phase": phase,
			"position": pos.State, "revision": cam.Revision, "observed_revision": w.Reconciler.Actual[cam.ID],
			"epoch": w.Epochs[cam.ID]}
		if w.StatusExtra != nil {
			for k, v := range w.StatusExtra(cam) {
				st[k] = v
			}
		}
		out = append(out, st)
	}
	for i, cam := range w.Rows { // `held`: the device is on the line, no stream is built
		if cam.Live == "on-demand" && out[i]["phase"] != "running" {
			if w.DeviceOfRow(cam) != nil {
				out[i]["phase"] = "held"
			} else {
				out[i]["phase"] = "pending"
			}
		}
	}
	if w.StatusFix != nil {
		w.StatusFix(out)
	}
	return out
}

// Headroom is what the autoscaler reads: cameras this worker could still take.
func (w *VmsWorker) Headroom() int {
	if h := w.Capacity - len(w.Rows); h > 0 {
		return h
	}
	return 0
}

func (w *VmsWorker) HeartbeatExtra() map[string]any {
	out := map[string]any{"server": w.Server, "instance": w.Instance, "alloc": w.Alloc, "labels": strings.Join(w.Labels, ","),
		"assignment_rev": w.AssignmentRev, "fenced": !w.RecordingAllowed, "conflicts": w.Conflicts(), "passes": w.Passes,
		"capacity": w.Capacity, "headroom": w.Headroom(), "started": w.StartedWall,
		"previous_hb": w.PreviousHb, "previous_instance": w.PreviousInstance,
		"archive": w.ArchiveRoot, "devices": w.DeviceStatus()} // the resource its events (a recorder: its footage) go to — on a cluster Nomad's meta.archive, through $ARCHIVE
	if w.HeartbeatFix != nil {
		w.HeartbeatFix(out)
	}
	return out
}

func (w *VmsWorker) HeartbeatOnce() error {
	return w.HeartbeatWith(w.Status(), w.HeartbeatExtra())
}

// Run: the loop as a process. Nomad or systemd restarts it.
func (w *VmsWorker) Run(poll time.Duration, stop <-chan struct{}) {
	heartbeat := w.HeartbeatOnce
	leaseEvery := (w.LeaseTTL - w.LeaseMargin) / 3
	if leaseEvery < 1 {
		leaseEvery = 1
	}
	// The first heartbeat goes BEFORE the loop, not on the first tick that happens to be ten seconds old.
	// Monotonic() counts from process start here and time.monotonic() counts from boot in Python, so
	// "clock() - 0 >= 10" is false in one and true in the other: the same loop, and a Go worker invisible
	// to the controller for its first ten seconds while a Python one is visible at once — ten seconds of
	// a restarted worker's cameras sitting unplaced, from an accident of what a clock counts from.
	// Both loops now say it instead of relying on it. (../vmsserver/tests/test_cross_go_worker.py found
	// this by running this binary against the Python controller; neither suite alone could have.)
	heartbeat()
	lastHb := w.Clock()
	var lastLease float64
	for {
		select {
		case <-stop:
			w.Act.StopAll()
			heartbeat()
			w.ReleaseSlot() // an orderly stop says so; a crash says nothing
			return
		default:
		}
		w.ReconcileOnce()
		w.PumpOnce()
		if w.Clock()-lastLease >= leaseEvery {
			w.LeasePass()
			lastLease = w.Clock()
		}
		if w.Clock()-lastHb >= 10 {
			heartbeat()
			lastHb = w.Clock()
		}
		select {
		case <-stop:
		case <-time.After(poll):
		}
	}
}
