package cluster

// The worker as an allocation. М10's VmsWorker, unchanged, plus what Nomad
// hands a process and what a server knows about itself:
//
//	NOMAD_ALLOC_INDEX   -> the slot to claim: w-<index>. The index is the preference;
//	                       the claim (CAS on vms/slots/w-N) is the proof.
//	NOMAD_NODE_NAME     -> `server` in the heartbeat: which resource it records into
//	NOMAD_META_labels   -> `labels` in the heartbeat: what this server can reach
//	CAPACITY            -> the worker's own number, from М9 Lesson 7's probe on THIS server

import (
	"os"
	"strconv"
	"strings"
	"time"

	"vmsserver/vms"
	p "vmsserver/vmsplatform"
)

// Env is an allocation's environment; nil means the process's own.
type Env map[string]string

func (e Env) get(k string) string {
	if e == nil {
		return os.Getenv(k)
	}
	return e[k]
}

func SlotFromEnvironment(env Env) string {
	if n := env.get("WORKER_NAME"); n != "" {
		return n
	}
	if i := env.get("NOMAD_ALLOC_INDEX"); i != "" {
		n, _ := strconv.Atoi(i)
		return "w-" + strconv.Itoa(n)
	}
	return "" // claim whatever is free — a lapsed slot first
}

func LabelsFromEnvironment(env Env) []string {
	out := []string{}
	for _, l := range strings.Split(env.get("NOMAD_META_labels"), ",") {
		if l != "" {
			out = append(out, l)
		}
	}
	return out
}

type ClusterWorker struct {
	*vms.VmsWorker
	Labels           []string
	Alloc            string
	StartedWall      float64
	PreviousHb       float64 // the previous instance of this slot, if it left a heartbeat: what failover is measured from
	PreviousInstance string
}

func NewClusterWorker(vars Variables, objects ObjectStore, act vms.Actuator, env Env, o vms.VmsWorkerOptions) (*ClusterWorker, error) {
	if o.Server == "" {
		o.Server = env.get("NOMAD_NODE_NAME")
		if o.Server == "" {
			o.Server = env.get("NOMAD_NODE_ID")
		}
		if o.Server == "" {
			o.Server, _ = os.Hostname()
		}
	}
	if o.Capacity == 0 {
		o.Capacity, _ = strconv.Atoi(env.get("CAPACITY"))
		if o.Capacity == 0 {
			o.Capacity = 50
		}
	}
	if o.Instance == "" {
		o.Instance = env.get("NOMAD_ALLOC_ID")
	}
	if act == nil {
		act = vms.NewFakeActuator()
	}
	base, err := vms.NewVmsWorker(SlotFromEnvironment(env), vars, objects, act, o)
	if err != nil {
		return nil, err
	}
	w := &ClusterWorker{VmsWorker: base, Labels: LabelsFromEnvironment(env), Alloc: env.get("NOMAD_ALLOC_ID"), StartedWall: base.Wall()}
	if raw, _ := objects.Get(w.Sub.HeartbeatKey(w.Name)); len(raw) > 0 {
		if old, err := p.HeartbeatFromBytes(raw); err == nil && old.ExtraString("instance", "") != w.Instance {
			w.PreviousHb, w.PreviousInstance = old.Ts, old.ExtraString("instance", "")
		}
	}
	return w, nil
}

func (w *ClusterWorker) HeartbeatOnce() error {
	extra := w.HeartbeatExtra()
	extra["alloc"] = w.Alloc
	extra["labels"] = strings.Join(w.Labels, ",")
	extra["started"] = w.StartedWall
	extra["previous_hb"] = w.PreviousHb
	extra["previous_instance"] = w.PreviousInstance
	return w.HeartbeatWith(w.Status(), extra)
}

// Run is М10's loop with this worker's heartbeat.
func (w *ClusterWorker) Run(poll time.Duration, stop <-chan struct{}) {
	w.VmsWorker.Run(poll, stop, w.HeartbeatOnce)
}
