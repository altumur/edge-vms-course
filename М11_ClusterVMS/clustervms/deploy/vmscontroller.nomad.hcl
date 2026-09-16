# deploy/vmscontroller.nomad.hcl — the controller: one, and safe at two.
# count = 1 is economy, not correctness: correctness is CAS, and a second
# instance would only repeat the same five-second pass. It has no HTTP —
# nothing asks it anything — and no Nomad client: it needs no more of the
# API than any task gets, Variables under its own prefixes (the policy).
job "vmscontroller" {
  datacenters = ["room-a"]
  type        = "service"

  group "vmscontroller" {
    count = 1
    task "vmscontroller" {
      driver = "podman"
      identity { env = true }
      config {
        image        = "localhost/clustervms:latest"
        network_mode = "host"
        args         = ["python3", "-m", "cluster", "controller"]
      }
      env {
        # The runtime's part of the seam (`w2cplatform/runtime.py`): the neutral names the loop
        # reads, filled here from Nomad's own. This file already knows the orchestrator — the
        # worker must not. A k8s manifest fills the same four from an ordinal and a fieldRef.
        SLOT_INDEX  = "${NOMAD_ALLOC_INDEX}"
        SERVER_NAME = "${node.unique.name}"
        LABELS      = "${meta.labels}"
        INSTANCE_ID = "${NOMAD_ALLOC_ID}"
        OBJECTS = "variables://objects"          # heartbeats and the snapshot as Variables; no MinIO on this cluster
        CLUSTER = "room-a"
      }
      resources { cpu = 200  memory = 128 }
    }
  }
}
