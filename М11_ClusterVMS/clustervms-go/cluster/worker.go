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

// ClusterRecorder: М10's RecWorker as an allocation — the recorder job. Nomad hands it what it hands a
// worker (NOMAD_ALLOC_INDEX → slot r-<i>, NOMAD_NODE_NAME → the server whose archive it writes into and
// whose shared memory it may read); it subscribes to each camera's worker from the VMS heartbeat and writes
// rec/<cam>/e<epoch>/ on ITS server's archive. Its jobspec has the archive constraint and `spread`; its
// controller moves its recordings when its server dies (rec/policy {servers: distinct} by default).
type ClusterRecorder = vms.RecWorker

func NewClusterRecorder(vars Variables, objects ObjectStore, act vms.Actuator, archive *vms.ArchiveResource, env Env, o vms.VmsWorkerOptions) (*ClusterRecorder, error) {
	o.Env = env
	return vms.NewRecWorker("", vars, objects, act, archive, o)
}
