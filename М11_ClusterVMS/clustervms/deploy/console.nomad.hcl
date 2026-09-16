# deploy/console.nomad.hcl — the console: the page and the API.
# A system job: one instance on every server that runs a resource, so that
# any server's :8080 is the console and nothing sits in front of it — no
# load balancer, no ingress; a person types any server's name, or a DNS
# name that resolves to all of them. It is stateless and holds no database:
# every instance reads the same raft and the same heartbeats, /events asks
# every live resource's event database and merges, and a retried POST is answered
# the same by whichever instance gets it because the Idempotency-Key is a
# Variable (vms/idem/*).
# Placed on servers that run a resource so that an operator's marks have a
# bucket to go into; drop the constraint and /marks answers 503 there.
job "console" {
  datacenters = ["room-a"]
  type        = "system"

  group "console" {
    constraint {
      attribute = "${meta.archive}"
      operator  = "is_set"
    }
    network {
      mode = "host"
      port "console" { static = 8080 }
    }
    task "console" {
      driver = "podman"
      identity { env = true }
      config {
        image        = "localhost/clustervms:latest"
        network_mode = "host"
        args         = ["python3", "-m", "cluster", "console"]
        volumes      = ["/data/archive:/data/archive"]
      }
      env {
        # The runtime's part of the seam (`psimplatform/runtime.py`): the neutral names the loop
        # reads, filled here from Nomad's own. This file already knows the orchestrator — the
        # worker must not. A k8s manifest fills the same four from an ordinal and a fieldRef.
        SLOT_INDEX  = "${NOMAD_ALLOC_INDEX}"
        SERVER_NAME = "${node.unique.name}"
        LABELS      = "${meta.labels}"
        INSTANCE_ID = "${NOMAD_ALLOC_ID}"
        OBJECTS      = "variables://objects"
        ARCHIVE      = "/data/archive"
        CONSOLE_PORT = "8080"
        CLUSTER      = "room-a"
      }
      service {                                      # what the autoscaler scrapes, what М12's read model and a browser reach
        name = "vms-console"
        port = "console"
        tags = ["metrics"]
      }
      resources { cpu = 300  memory = 128 }             # the page and the API; no event database here
    }
  }
}
