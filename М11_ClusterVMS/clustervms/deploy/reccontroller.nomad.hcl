# deploy/reccontroller.nomad.hcl — the recorder's controller: one, and
# safe at two, exactly as vmscontroller. The same class over rec.subsystem.yaml
# (`SpecController(REC_SPEC)`): it places recordings on recorders whose server's
# resource answers, one recorder per server by default (rec/policy), and moves
# a dead server's recordings when its slot lapsed AND its resource is silent.
job "reccontroller" {
  datacenters = ["room-a"]
  type        = "service"

  group "reccontroller" {
    count = 1
    task "reccontroller" {
      driver = "podman"
      identity { env = true }
      config {
        image        = "localhost/clustervms:latest"
        network_mode = "host"
        args         = ["python3", "-m", "cluster", "reccontroller"]
      }
      env {
        # The runtime's part of the seam (`w2cplatform/runtime.py`): the neutral names the loop
        # reads, filled here from Nomad's own. This file already knows the orchestrator — the
        # worker must not. A k8s manifest fills the same four from an ordinal and a fieldRef.
        SLOT_INDEX  = "${NOMAD_ALLOC_INDEX}"
        SERVER_NAME = "${node.unique.name}"
        LABELS      = "${meta.labels}"
        INSTANCE_ID = "${NOMAD_ALLOC_ID}"
        OBJECTS = "variables://objects"
      }
      resources { cpu = 200  memory = 128 }
    }
  }
}
