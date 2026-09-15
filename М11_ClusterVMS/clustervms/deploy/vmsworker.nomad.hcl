# deploy/vmsworker.nomad.hcl — the worker: DriverPack as a service job. It
# HOLDS the camera — one connection, one epoch, one fan-out (rtsp://<server>:
# 8554/<cam>) that the recorder, the gateway and the detectors subscribe to —
# and writes the camera's events into the resource on its server. It records
# nothing: footage is vmsrecorder.nomad.hcl's, the job with the disks.
# count = N and NOTHING in the VMS decides N: the operator sets the bounds,
# the Nomad Autoscaler moves count from the workers' own load. Each
# allocation claims slot w-<NOMAD_ALLOC_INDEX> by CAS on a Variable — the
# index is the preference, the Variable is the proof (Lesson 2).
job "vmsworker" {
  datacenters = ["room-a"]
  type        = "service"

  group "vmsworker" {
    count = 2

    scaling {
      enabled = true
      min     = 1
      max     = 12                                   # the servers' budget: B + n·I from М9 Lesson 7
      policy {
        cooldown            = "5m"                   # longer than a failover, so a reschedule is not read as demand
        evaluation_interval = "1m"
        check "load" {
          source = "prometheus"
          query  = "avg(vms_worker_load)"            # assigned ÷ capacity, from the heartbeats — never CPU
          strategy "target-value" { target = 0.9 }
        }
      }
    }

    # a worker writes its camera's EVENTS into the resource on its own server (as a detector does): only
    # servers that have one. Footage is not its concern — that constraint is the recorder's.
    constraint {
      attribute = "${meta.archive}"
      operator  = "is_set"
    }
    # spread, not distinct_hosts: Nomad puts workers on different servers when it can and doubles up when
    # it must (a dead server's worker rescheduled onto a neighbour). Whether a second worker on one server
    # CARRIES cameras is the administrator's choice on the console, not the scheduler's — `vms/policy
    # {servers: shared | distinct}`: shared (the default: a worker holds a camera, and several on one server
    # hold different cameras) a dead server's worker comes back on a neighbour with its cameras; distinct,
    # one worker per server carries cameras, a doubled-up worker idles by policy, and the CONTROLLER moves
    # a dead server's cameras (two silences: the slot lapsed and the server's resource silent). Both
    # readable in every placement reason. The recorder's own knob is rec/policy, distinct by default.
    spread {
      attribute = "${node.unique.id}"
    }

    disconnect {                                     # Lesson 4: the defaults are wrong for a process that holds a camera
      lost_after           = "45s"
      replace              = true
      stop_on_client_after = "25s"                   # the holder stops at TTL − margin on its own clock anyway
      reconcile            = "best_score"
    }

    task "vmsworker" {
      driver = "podman"
      kill_timeout = "20s"                           # room to release the slot: scale-in says so, a crash cannot
      identity { env = true }                        # NOMAD_TOKEN: the task's own workload identity, scoped by the policy
      config {
        image        = "localhost/clustervms:latest"
        network_mode = "host"
        args         = ["python3", "-m", "cluster", "worker"]
        volumes      = ["/data/archive:/data/archive", "/data/media:/data/media"]   # no spool: it writes events, never segments
      }
      env {
        OBJECTS   = "variables://objects"          # heartbeats and the snapshot as Variables; no MinIO on this cluster
        CAPACITY  = "50"                             # this server's number; per node class in a product
        ARCHIVE   = "${meta.archive}"                # the label the constraint above placed by, handed to the worker: its events go there,
      }                                              # and it reports it in its heartbeat — the console's /servers shows the label beside the fact
      resources { cpu = 2000  memory = 2048 }        # B + n·I, rounded up
    }
  }
}
