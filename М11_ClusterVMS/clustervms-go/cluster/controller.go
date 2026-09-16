package cluster

// The controller as a job — and it is М10's, unchanged. What the first
// ClusterVMS design added here (label constraints, the server in the
// reason, the snapshot for М12, the measured failover) turned out to be the
// general behaviour with N = 1: on one box the labels are empty, the server
// is the hostname, and the snapshot is what a single-cluster customer's
// console reads. So it lives in the platform's SpecController, the VMS's
// spec names it, and this file keeps only the names М11's lessons used.
//
// Still no Nomad client: it never places a process, never sets count,
// never retires a slot from a silence.

import (
	"strings"

	p "vmsserver/w2cplatform"
	"vmsserver/vms"
)

type (
	ClusterController = vms.VmsController
	Unplaceable       = vms.Unplaceable
)

func NewClusterController(vars Variables, objects ObjectStore, capacity int, wall p.Clock, cluster string) *ClusterController {
	return vms.NewVmsControllerIn(cluster, vars, objects, capacity, wall)
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
