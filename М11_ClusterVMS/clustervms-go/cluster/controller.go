package cluster

// The controller as a job. М10's VmsController plus the two things a
// cluster adds to placement and the one thing it owes the domain:
//
//	constraints   a camera's labels ("vlan:cctv-a") must be a subset of what the worker's
//	              server reports; "the system is full" now also means "nothing that can reach it"
//	servers       the heartbeat says which server a worker is on; the placement reason names it
//	the snapshot  vms/snapshot in the object store — cameras and placement as one object for
//	              М12's read model. The only thing that leaves the cluster, and it is a copy.
//
// Still no Nomad client: it never places a process, never sets count,
// never retires a slot from a silence.

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"vmsserver/vms"
	p "vmsserver/vmsplatform"
)

type ClusterController struct {
	*vms.VmsController
	Cluster string
}

func NewClusterController(vars Variables, objects ObjectStore, capacity int, wall p.Clock, cluster string) *ClusterController {
	if cluster == "" {
		cluster = "cluster-a"
	}
	return &ClusterController{vms.NewVmsController(vars, objects, capacity, wall), cluster}
}

func (c *ClusterController) LabelsOf(worker string) map[string]bool {
	out := map[string]bool{}
	if hb, ok := c.WorkersSeen(1e12)[worker]; ok {
		for _, l := range strings.Split(hb.ExtraString("labels", ""), ",") {
			if l != "" {
				out[l] = true
			}
		}
	}
	return out
}

func (c *ClusterController) ServerOf(worker string) string {
	if hb, ok := c.WorkersSeen(1e12)[worker]; ok {
		return hb.ExtraString("server", "?")
	}
	return "?"
}

func (c *ClusterController) Eligible(cam vms.Camera, workers []string) []string {
	out := []string{}
	for _, w := range workers {
		have := c.LabelsOf(w)
		ok := true
		for _, need := range cam.Labels {
			if !have[need] {
				ok = false
			}
		}
		if ok {
			out = append(out, w)
		}
	}
	return out
}

func (c *ClusterController) liveWorkers(workers []string) []string {
	if workers == nil {
		for w := range c.WorkersSeen(45) {
			workers = append(workers, w)
		}
	}
	out := append([]string{}, workers...)
	sort.Strings(out)
	return out
}

func (c *ClusterController) Place(cid int, workers []string) (*vms.Placement, error) {
	if have := c.Placement(cid); have != nil {
		return have, nil
	}
	cam := c.Camera(cid)
	if cam == nil {
		return nil, nil
	}
	pool := c.Eligible(*cam, c.liveWorkers(workers))
	if len(pool) == 0 {
		return nil, nil // "the system is full" — or nothing that can reach it
	}
	best, free := "", 0
	for _, w := range pool {
		if f := c.CapacityOf(w) - c.Load(w); f > free {
			best, free = w, f
		}
	}
	if best == "" {
		return nil, nil
	}
	reason := fmt.Sprintf("most free capacity (%d) among %d worker(s)", free, len(pool))
	if len(cam.Labels) > 0 {
		ls := append([]string{}, cam.Labels...)
		sort.Strings(ls)
		reason += " reaching " + strings.Join(ls, ",")
	}
	reason += "; on " + c.ServerOf(best)
	return c.StorePlacement(vms.Placement{Camera: cid, Worker: best, Reason: reason, At: c.Wall()})
}

func (c *ClusterController) EnsurePlaced(workers []string) ([]vms.Placement, error) {
	return vms.EnsurePlacedWith(c.VmsController, c, workers)
}

type Unplaceable struct {
	ID          int      `json:"id"`
	Labels      []string `json:"labels"`
	WorkersLive int      `json:"workers_live"`
}

// Unplaceable: cameras nothing can reach — the console's honest answer, with the labels named.
func (c *ClusterController) Unplaceable() []Unplaceable {
	live := c.liveWorkers(nil)
	out := []Unplaceable{}
	for _, cam := range c.Cameras() {
		if c.Placement(cam.ID) == nil && len(c.Eligible(cam, live)) == 0 {
			out = append(out, Unplaceable{cam.ID, cam.Labels, len(live)})
		}
	}
	return out
}

// Snapshot: cameras and placement as one object — what М12 reads. A copy
// with an age; never the rows themselves, which do not leave raft.
func (c *ClusterController) Snapshot() map[string]any {
	cams := []map[string]any{}
	for _, cam := range c.Cameras() {
		m := cam.ToMap()
		w := c.Where(cam.ID)
		if w != "" {
			m["worker"] = w
		} else {
			m["worker"] = nil
		}
		m["server"] = c.ServerOf(w)
		cams = append(cams, m)
	}
	return map[string]any{"cluster": c.Cluster, "ts": c.Wall(), "cameras": cams}
}

func (c *ClusterController) PublishSnapshot() error {
	raw, _ := json.Marshal(c.Snapshot())
	return c.Objects.Put(c.Sub.Config("snapshot"), raw)
}

// FailoverSeconds per worker: the gap between the heartbeat before its
// current instance started and that instance's first — measured from what
// the workers wrote, not from Nomad.
func (c *ClusterController) FailoverSeconds() map[string]float64 {
	out := map[string]float64{}
	for w, hb := range c.WorkersSeen(1e12) {
		started := hb.Ts
		if v, ok := hb.Extra["started"]; ok {
			started = p.ToFloat(v)
		}
		if prev := p.ToFloat(hb.Extra["previous_hb"]); prev > 0 {
			out[w] = float64(int64((started-prev)*10+0.5)) / 10
		}
	}
	return out
}

// Heartbeats: every worker's last heartbeat, whatever its age — the console's read model.
func Heartbeats(objects ObjectStore, prefix string) map[string]p.Heartbeat {
	if prefix == "" {
		prefix = "vms/"
	}
	out := map[string]p.Heartbeat{}
	keys, _ := objects.List(prefix)
	for _, k := range keys {
		if !strings.HasSuffix(k, "/heartbeat") {
			continue
		}
		raw, _ := objects.Get(k)
		if hb, err := p.HeartbeatFromBytes(raw); err == nil && len(raw) > 0 {
			out[hb.Worker] = hb
		}
	}
	return out
}
