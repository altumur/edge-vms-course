# deploy/recworker.nomad.hcl — the recorder: the fourth subsystem's worker
# and the ONLY job placed on top of the archive for footage. A worker holds the
# camera (one connection, one fan-out); a recorder subscribes to that fan-out
# and writes rec/<cam>/e<epoch>/ on ITS server's disks. count = N as for the
# worker: the operator's bounds, the Autoscaler's move; each allocation
# claims slot r-<NOMAD_ALLOC_INDEX> by CAS (Lesson 2, the same proof).
job "recworker" {
  datacenters = ["room-a"]
  type        = "service"

  group "recworker" {
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
        # The second demand, and the one `load` cannot see. A recorder holds ONE archive
        # (`servers: distinct` over `place_by: volume`), so an archive the operator declared and nobody
        # holds needs a PROCESS, not a busier one — and the recorders that exist may be perfectly loaded
        # while it sits unserved. `rec_volumes_unserved` is that count, published by the console beside
        # the rest; with several checks the Autoscaler takes the larger count, so whichever demand is
        # bigger wins and neither hides the other.
        #
        # Scale-IN is `load`'s alone: this check asks for at least as many processes as there are live
        # ones, so it never brings the count down — a spare is cheap and it is what makes the NEXT
        # archive get served in a pass instead of a deploy.
        # `rec_recorders_needed`, not `rec_volumes_unserved`: an archive nobody CAN take does not become
        # takeable by starting processes. A local volume declared for a server the scheduler puts no
        # recorder on would leave `unserved` at one for ever, and this check would then ask for one more
        # worker, and another, to the ceiling — each of them a spare that cannot help. The console
        # subtracts the spares, so one free process is proof the shortage is not a shortage of processes.
        #
        # Counting LIVE workers rather than the configured count matters for the same reason: an
        # allocation that cannot be placed leaves the query where it was instead of compounding.
        check "unserved-archives" {
          source = "prometheus"
          query  = "rec_workers_live + rec_recorders_needed"
          strategy "pass-through" {}
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

    task "recworker" {
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
