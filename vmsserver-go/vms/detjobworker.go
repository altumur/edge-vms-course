package vms

// The scan worker — the fifth subsystem's worker, and the first whose work ENDS.
//
// A unit is a job: one model over one interval of one recording. The worker does not subscribe to
// anything. It plans the interval out of the recording's manifest (scan.go), decodes each stretch in
// turn, writes what the model saw into the job's own bucket on the resource, and appends a line to the
// job's manifest for every stretch it finishes.
//
// Three things separate it from DetWorker, and all three come from the work having both ends. It runs a
// BUDGET of stretches per pass rather than a frame: a job is hours of video and a pass that finished it
// would starve the heartbeat — its lease lapses, the controller calls it dead, and two workers end up
// writing the same job's events. Its events are stamped with MEDIA time, not the clock. And it is the only
// worker that can say `done`, which the console's reaper turns into the row's state.

import (
	"os"
	"sort"
	"strconv"
	"strings"

	p "vmsserver/w2cplatform"
)

// DETJOB is the subsystem's name — the keys its slot, epoch, assignment and heartbeat live under.
var DETJOB = DetJobSpec.Sub()

// Terminal states: what the console's reaper writes when a job is over.
var terminal = map[string]bool{"done": true, "failed": true}

// DetJobWorker is a worker of the `detjob` subsystem.
type DetJobWorker struct {
	*p.Worker
	Models      map[string]func(p.Row) Model
	Capacity    int
	Server      string
	Labels      []string
	ArchiveRoot string
	// StretchesPerPass is the budget. See the note above: bounded work run to completion inside one pass
	// is a worker that stops heartbeating while it does it.
	StretchesPerPass int
	Step             float64 // seconds of media between two looks
	Index            Index   // the device's index, through its holder's door; a test hands in its own

	running       map[string]Model
	statusByUnit  map[string]map[string]any
	EventsWritten int
}

type DetJobOptions struct {
	Models           map[string]func(p.Row) Model
	Capacity         int
	Server           string
	Labels           []string
	ArchiveRoot      string
	StretchesPerPass int
	Step             float64
	Index            Index
	Worker           p.WorkerOptions
}

func NewDetJobWorker(name string, vars p.Variables, objects p.ObjectStore, o DetJobOptions) (*DetJobWorker, error) {
	wo := o.Worker
	wo.Name = ""
	w := p.NewWorker(DETJOB, vars, objects, wo)
	if _, err := w.ClaimSlot(slotOr(name, "j")); err != nil {
		return nil, err
	}
	models := o.Models
	if models == nil {
		models = map[string]func(p.Row) Model{"motion": NewFakeModel, "linecross": NewFakeModel, "lpr": NewFakeModel}
	}
	d := &DetJobWorker{Worker: w, Models: models, Capacity: o.Capacity, Server: o.Server, Labels: o.Labels,
		ArchiveRoot: o.ArchiveRoot, StretchesPerPass: o.StretchesPerPass, Step: o.Step, Index: o.Index,
		running: map[string]Model{}, statusByUnit: map[string]map[string]any{}}
	if d.Capacity == 0 {
		d.Capacity = 2
	}
	if d.Labels == nil {
		d.Labels = []string{"gpu"}
	}
	if d.ArchiveRoot == "" {
		d.ArchiveRoot = "/data/archive"
	}
	if d.Server == "" {
		d.Server, _ = os.Hostname()
	}
	if d.StretchesPerPass == 0 {
		d.StretchesPerPass = 4
	}
	if d.Step == 0 {
		d.Step = 1
	}
	if d.Index == nil {
		d.Index = DeviceRecordings
	}
	return d, nil
}

func (d *DetJobWorker) jobRow(job string) p.Row {
	items, _, _ := d.Vars.Get(DETJOB.Config("jobs", job))
	if items == nil || items["deleted"] == "true" {
		return nil
	}
	return DetJobSpec.RowOf(items)
}

// DeviceHas: whether the camera's own device holds any of [t0, t1). Enough to tell "nobody recorded this"
// from "somebody did, just not us".
func (d *DetJobWorker) DeviceHas(cam string, t0, t1 float64) bool {
	h, found := p.HolderOf(d.Objects, "vms/", cam, d.Wall(), p.HolderQuery{Field: "coverage"})
	if !found {
		return false
	}
	cov, isMap := h.Status["coverage"].(map[string]any)
	if !isMap {
		return false
	}
	if !(p.ToFloat(cov["to"]) > t0 && p.ToFloat(cov["from"]) < t1) {
		return false // outside what the device holds at all
	}
	// Inside the summary is not the same as "there is footage there". A device recording on motion has
	// mostly nothing between its first and last minute, and a job told `fetching` about minutes that do not
	// exist waits for a fetch that will never bring anything. So the index is asked — and when the driver
	// cannot list, the summary stands, because refusing work that would succeed is the worse of the two
	// mistakes.
	url := p.Str(h.Status["index_url"])
	if url == "" || url == "<nil>" {
		return true
	}
	spans, err := d.Index(url, t0, t1)
	if err != nil {
		return true // cannot list, or the holder is there and not answering: the summary stands
	}
	return CoveredBy(spans, true, t0, t1) > 0
}

// ReconcileOnce: advance every assigned job by at most a budget of stretches.
func (d *DetJobWorker) ReconcileOnce() []string {
	wanted := map[string]bool{}
	jobs := append([]string{}, d.Assignment().Units...)
	sort.Strings(jobs)
	for _, job := range jobs {
		wanted[job] = true
		row := d.jobRow(job)
		if row == nil {
			continue
		}
		factory, known := d.Models[p.Str(row["kind"])]
		if !known {
			d.statusByUnit[job] = d.jobStatus(job, row, "unsupported", "", nil, nil)
			continue
		}
		if state := p.Str(row["state"]); terminal[state] {
			d.statusByUnit[job] = d.jobStatus(job, row, state, "", nil, nil) // the reaper has read this and not yet unplaced it
			continue
		}

		scans := Plan(d.ArchiveRoot, p.Str(row["rec"]), p.ToFloat(row["from"]), p.ToFloat(row["to"]))
		log := NewScanLog(d.ArchiveRoot, job)
		if len(scans) == 0 {
			// Not "no events": no FOOTAGE, here. Two different silences, and which one it is decides what
			// happens next — so the worker says which.
			//
			// If the DEVICE has those minutes (a card, an NVR — М10B Lesson 15), this is not a dead end but a
			// step: the footage exists, it is simply not ours yet. Reading it over the device's playback door
			// would be wrong twice — that door admits two sessions per device, and they belong to the
			// operator watching and to the recorder saving — so the job says `fetching`, the console asks
			// the recorder for the range (Lesson 16), and the scan runs afterwards over footage we own.
			d.stopJob(job)
			if d.DeviceHas(p.Str(row["cam"]), p.ToFloat(row["from"]), p.ToFloat(row["to"])) {
				d.statusByUnit[job] = d.jobStatus(job, row, "fetching",
					"the device has these minutes and we do not — asking the recorder", nil, nil)
			} else {
				d.statusByUnit[job] = d.jobStatus(job, row, "waiting",
					"no footage for that interval on this server — the recording may be on another server", nil, nil)
			}
			continue
		}
		left := Remaining(scans, log)
		if len(left) == 0 {
			d.stopJob(job)
			d.statusByUnit[job] = d.jobStatus(job, row, "done", "", scans, log)
			continue
		}
		if _, have := d.Epochs[job]; !have {
			if _, err := d.TakeEpoch(job); err != nil { // one writer of detjob/<job>/… at a time
				continue
			}
		}
		model, up := d.running[job]
		if !up {
			model = factory(row)
			d.running[job] = model
		}
		if d.MayWrite(job) {
			budget := d.StretchesPerPass
			if budget > len(left) {
				budget = len(left)
			}
			cam, _ := strconv.Atoi(p.Str(row["cam"]))
			for _, sc := range left[:budget] {
				n := 0
				// The file is read from its HEAD — a segment opened at 10:00 must be decoded from 10:00
				// even when the operator asked from 10:05 — so the model sees the lead-in, and `Accepts`
				// is what keeps what it saw there out of the answer.
				for ts := sc.Seg.Start; ts < sc.T1; ts += d.Step {
					for _, obs := range model.Observe(ts) {
						if !sc.Accepts(ts) {
							continue
						}
						fields := map[string]any{"cam": cam, "job": job, "source": "archive"}
						for k, v := range obs.Fields {
							if k != "cam" {
								fields[k] = v
							}
						}
						if _, err := p.NewEventLog(d.ArchiveRoot, DETJOB.Name, job, d.Epochs[job], 0).Append(ts, obs.Kind, fields); err != nil {
							continue
						}
						n++
						d.EventsWritten++
					}
				}
				log.Append(sc, n, d.Wall()) // the line AFTER the events: a crash costs one re-scan
			}
		}
		d.statusByUnit[job] = d.jobStatus(job, row, "running", "", scans, log)
	}
	for job := range d.running {
		if !wanted[job] {
			d.stopJob(job)
			d.Release(job)
		}
	}
	for job := range d.statusByUnit {
		if !wanted[job] {
			delete(d.statusByUnit, job)
		}
	}
	d.RenewLeases()
	out := make([]string, 0, len(d.running))
	for job := range d.running {
		out = append(out, job)
	}
	sort.Strings(out)
	return out
}

// jobStatus is what the console and the controller read. `done_through` and `covered` are here and not
// computed by a reader, because the log they come from is a file on THIS server's disk.
func (d *DetJobWorker) jobStatus(job string, row p.Row, phase, why string, scans []Scan, log *ScanLog) map[string]any {
	st := map[string]any{"id": job, "cam": p.Str(row["cam"]), "rec": p.Str(row["rec"]),
		"kind": p.Str(row["kind"]), "phase": phase, "from": p.ToFloat(row["from"]), "to": p.ToFloat(row["to"])}
	if why != "" {
		st["why"] = why
	}
	if log != nil {
		st["done_through"], st["events"] = log.DoneThrough(), log.Events()
	}
	if scans != nil {
		st["covered"] = Covered(scans) // seconds of the interval that footage exists for
		asked := p.ToFloat(row["to"]) - p.ToFloat(row["from"])
		if asked < 0 {
			asked = 0
		}
		st["asked"] = asked
	}
	return st
}

func (d *DetJobWorker) stopJob(job string) {
	if m, ok := d.running[job]; ok {
		m.Close()
		delete(d.running, job)
	}
}

func (d *DetJobWorker) Headroom() int {
	if n := d.Capacity - len(d.running); n > 0 {
		return n
	}
	return 0
}

func (d *DetJobWorker) HeartbeatOnce() error {
	names := make([]string, 0, len(d.statusByUnit))
	for u := range d.statusByUnit {
		names = append(names, u)
	}
	sort.Strings(names)
	status := make([]map[string]any, 0, len(names))
	for _, u := range names {
		status = append(status, d.statusByUnit[u])
	}
	return d.HeartbeatWith(status, map[string]any{
		"server": d.Server, "instance": d.Instance, "labels": strings.Join(d.Labels, ","),
		"capacity": d.Capacity, "headroom": d.Headroom(), "conflicts": d.Conflicts(), "events": d.EventsWritten,
	})
}

// Status is what the tests read: the per-unit entry this worker would publish.
func (d *DetJobWorker) Status(job string) map[string]any { return d.statusByUnit[job] }
