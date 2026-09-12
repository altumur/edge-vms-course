package cluster

// The worker as an allocation — and it is М10's VmsWorker, unchanged. What
// Nomad hands a process (NOMAD_ALLOC_INDEX, NOMAD_NODE_NAME,
// NOMAD_META_labels, NOMAD_ALLOC_ID, CAPACITY) the worker reads from its
// environment on a box exactly as in an allocation; this file keeps the
// names М11's lessons used and the env-first calling convention.

import "vmsserver/vms"

type (
	ClusterWorker = vms.VmsWorker
	Env           = vms.Env
)

func NewClusterWorker(vars Variables, objects ObjectStore, act vms.Actuator, env Env, o vms.VmsWorkerOptions) (*ClusterWorker, error) {
	o.Env = env
	return vms.NewVmsWorker("", vars, objects, act, o)
}
