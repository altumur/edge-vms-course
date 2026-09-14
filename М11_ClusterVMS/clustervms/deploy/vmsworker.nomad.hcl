# deploy/vmsworker.nomad.hcl — the worker: DriverPack as a service job.
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
      max     = 12                                   # ≤ the archive servers (distinct_hosts): the budget B + n·I from М9 Lesson 7 is per server
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

    # a worker records into the resource on its own server: only servers that have one
    constraint {
      attribute = "${meta.archive}"
      operator  = "is_set"
    }
    # and one worker per server: a second worker on the same disks and NIC is no second place to record.
    # So `count` ≤ the archive servers (scaling.max says the same), and when a server dies there is nowhere
    # to reschedule its worker — the slot stays pending, and the CONTROLLER moves the cameras (two
    # silences: the slot lapsed and the server's resource silent), rather than Nomad piling two workers on
    # one server. Explicit, and visible in every placement reason.
    constraint {
      distinct_hosts = true
    }

    disconnect {                                     # Lesson 4: the defaults are wrong for a recorder
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
        volumes      = ["/data/spool:/data/spool", "/data/archive:/data/archive", "/data/media:/data/media"]
      }
      env {
        OBJECTS   = "variables://objects"          # heartbeats and the snapshot as Variables; no MinIO on this cluster
        CAPACITY  = "50"                             # this server's number; per node class in a product
        ARCHIVE   = "${meta.archive}"                # the label the constraint above placed by, handed to the worker: it records there,
      }                                              # and reports it in its heartbeat — the console's /servers shows the label beside the fact
      resources { cpu = 2000  memory = 2048 }        # B + n·I, rounded up
    }
  }
}
