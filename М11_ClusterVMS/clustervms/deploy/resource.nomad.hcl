# deploy/resource.nomad.hcl — the resource: a PLATFORM system job, one
# allocation on every server that declares meta.archive, pinned there for
# as long as the server exists. It serves every subsystem's buckets, takes
# mirrors from its peers, retains buckets by each subsystem's own policy,
# runs the passes subsystems register on it (the VMS: manifests, media
# retention), and keeps the event index over its own tree — a SQLite cache
# rebuilt on every start, served as GET /events; the console merges these.
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
        OBJECTS      = "variables://objects"       # its heartbeat as a Variable; no MinIO on this cluster
        RESOURCE_URL = "http://${attr.unique.network.ip-address}:8090"   # where peers PUT mirrors and the console asks /events, /manifest, /segment
        NOMAD_NODE_NAME = "${node.unique.name}"
        # EVENTINDEX_DB unset: the index over this tree is :memory:, rebuilt on every start — a cache
      }
      service {                                    # peers find each other here; verify-bench uses it
        name = "resource"
        port = "manifests"
      }
      resources { cpu = 200  memory = 384 }             # the tree, the passes, and a SQLite index over this server's buckets
    }
  }
}
