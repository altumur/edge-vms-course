# deploy/resource.nomad.hcl — the resource: a PLATFORM system job, one
# allocation on every server that declares meta.archive, pinned there for
# as long as the server exists. It serves every subsystem's buckets, takes
# mirrors from its peers, retains buckets by each subsystem's own policy,
# runs the passes subsystems register on it (the VMS: manifests, media
# retention), and keeps the event database over its own tree — a SQLite cache
# rebuilt on every start, served as GET /events; the console merges these.
# The same process М10 runs on a box (python3 -m vms resource).
# No controller. Its heartbeat is platform/resources/<server>.
job "resource" {
  datacenters = ["room-a"]
  type        = "system"

  constraint {
    attribute = "${meta.archive}"
    operator  = "is_set"
  }

  group "resource" {
    network {
      mode = "host"
      port "manifests" { static = 8090 }
    }
    task "resource" {
      driver = "podman"
      identity { env = true }
      config {
        image        = "localhost/clustervms:latest"
        network_mode = "host"
        args         = ["python3", "-m", "cluster", "resource"]
        volumes      = ["/data/spool:/data/spool", "/data/archive:/data/archive"]
      }
      env {
        # The runtime's part of the seam (`w2cplatform/runtime.py`): the neutral names the loop
        # reads, filled here from Nomad's own. This file already knows the orchestrator — the
        # worker must not. A k8s manifest fills the same four from an ordinal and a fieldRef.
        SLOT_INDEX  = "${NOMAD_ALLOC_INDEX}"
        SERVER_NAME = "${node.unique.name}"
        LABELS      = "${meta.labels}"
        INSTANCE_ID = "${NOMAD_ALLOC_ID}"
        OBJECTS      = "variables://objects"       # its heartbeat as a Variable; no MinIO on this cluster
        RESOURCE_URL = "http://${attr.unique.network.ip-address}:8090"   # where peers PUT mirrors and the console asks /events, /manifest, /segment
        NOMAD_NODE_NAME = "${node.unique.name}"
        # EVENTDB unset: the event database over this tree is :memory:, rebuilt on every start — a cache
      }
      service {                                    # peers find each other here; verify-bench uses it
        name = "resource"
        port = "manifests"
      }
      resources { cpu = 200  memory = 384 }             # the tree, the passes, and the event database over this server's buckets
    }
  }
}
