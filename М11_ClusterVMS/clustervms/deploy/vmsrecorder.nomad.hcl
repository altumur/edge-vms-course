# deploy/vmsrecorder.nomad.hcl — the recorder: the fourth subsystem's worker
# and the ONLY job placed on top of the archive for footage. A worker holds the
# camera (one connection, one fan-out); a recorder subscribes to that fan-out
# and writes rec/<cam>/e<epoch>/ on ITS server's disks. count = N as for the
# worker: the operator's bounds, the Autoscaler's move; each allocation
# claims slot r-<NOMAD_ALLOC_INDEX> by CAS (Lesson 2, the same proof).
job "vmsrecorder" {
  datacenters = ["room-a"]
  type        = "service"

  group "vmsrecorder" {
    count = 2

    scaling {
      enabled = true
      min     = 1
      max     = 12                                   # ≤ the archive servers under `servers: distinct` — a 13th would idle by policy
      policy {
        cooldown            = "5m"
        evaluation_interval = "1m"
        check "load" {
          source = "prometheus"
          query  = "avg(rec_worker_load)"            # assigned ÷ capacity from the recorders' heartbeats — never disk I/O
          strategy "target-value" { target = 0.9 }
        }
      }
    }

    # a recorder writes into the resource on its own server: only servers that have one. This is the
    # constraint that USED to be the worker's reason for the archive; it is the recorder's now.
    constraint {
      attribute = "${meta.archive}"
      operator  = "is_set"
    }
    # `near: vms` in rec.subsystem.yaml: the rec controller prefers the recorder on the server whose worker holds the
    # camera — there it reads the worker's tee through shared memory (/run/vms) instead of the RTSP fan-out. An
    # affinity, never a filter: no room beside the worker and the recording goes elsewhere, over RTSP, and the
    # placement reason says so ("away from w-1 on srv-a (no room there)").
    # spread, not distinct_hosts: a dead server's recorder comes back on a neighbour — and idles there by
    # default, because `rec/policy {servers: distinct}`: a second recorder on the same disks is no second
    # place to record. The rec CONTROLLER moves the dead server's recordings to a server whose resource
    # answers (Lesson 4's two silences); the footage before the move stays on the old disks, unavailable
    # until the server returns — not lost.
    spread {
      attribute = "${node.unique.id}"
    }

    disconnect {
      lost_after           = "45s"
      replace              = true
      stop_on_client_after = "25s"
      reconcile            = "best_score"
    }

    task "vmsrecorder" {
      driver = "podman"
      kill_timeout = "20s"                           # SIGTERM finalizes the open segment; the last pass promotes it
      identity { env = true }
      config {
        image        = "localhost/clustervms:latest"
        network_mode = "host"                        # it subscribes to workers' RTSP fan-outs, on this server or elsewhere
        args         = ["python3", "-m", "cluster", "recorder"]
        volumes      = ["/data/spool:/data/spool", "/data/archive:/data/archive", "/run/vms:/run/vms"]   # the only writer of segments; no /data/media: it never reads a camera; /run/vms: the workers' shared memory on this server
      }
      env {
        # The runtime's part of the seam (`w2cplatform/runtime.py`): the neutral names the loop
        # reads, filled here from Nomad's own. This file already knows the orchestrator — the
        # worker must not. A k8s manifest fills the same four from an ordinal and a fieldRef.
        SLOT_INDEX  = "${NOMAD_ALLOC_INDEX}"
        SERVER_NAME = "${node.unique.name}"
        LABELS      = "${meta.labels}"
        INSTANCE_ID = "${NOMAD_ALLOC_ID}"
        OBJECTS   = "variables://objects"
        CAPACITY  = "50"                             # recordings this server's disks and NIC can take — its own number
        ARCHIVE   = "${meta.archive}"                # the label the constraint placed by, handed to the recorder
      }
      resources { cpu = 1000  memory = 1024 }
    }
  }
}
