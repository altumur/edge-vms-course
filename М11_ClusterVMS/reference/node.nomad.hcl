# reference/node.nomad.hcl — Lesson 2: М9's recorder as a Nomad job.
# One job per recorder. The job NAME is the recorder's identity; the server it lands
# on is Nomad's business. Requires Nomad >= 1.8.0 (the disconnect block) and
# the nomad-driver-podman plugin on every client.
job "node-3" {
  datacenters = ["room-a"]          # one cluster = one datacenter here; regions are М12's
  type        = "service"

  group "node" {
    count = 1                       # "count = 1" does not mean "exactly one" during a
                                    # reschedule — see Lesson 4. The epoch does.

    # Lesson 2, Step 6: cameras are not uniformly reachable. The client's
    # meta.vlans is set in its agent config from what its NICs can see.
    constraint {
      attribute = "${meta.vlans}"
      operator  = "set_contains"
      value     = "cctv-a"
    }

    # Lesson 4, Step 2: the default (lost immediately, replaced while the
    # partitioned client keeps running its tasks) is wrong for a recorder.
    disconnect {
      lost_after           = "45s"     # TTL + margin (Lesson 4): how long a silent client keeps its allocation
      replace              = true      # then place a replacement elsewhere
      stop_on_client_after = "25s"     # TTL − margin: the partitioned client stops the old one itself
      reconcile            = "keep_replacement"   # when it comes back, the new one wins
    }

    reschedule {                       # Lesson 4, Step 1: reschedule = different server
      delay          = "15s"
      delay_function = "exponential"
      max_delay      = "2m"
      unlimited      = true
    }

    restart {                          # restart = same server, first
      attempts = 3
      interval = "10m"
      delay    = "15s"
      mode     = "delay"
    }

    network {
      mode = "host"
      port "console" { static = 8080 }
    }

    task "postgres" {
      driver = "podman"
      config {
        image        = "docker.io/library/postgres:16"
        network_mode = "host"
        # Per-recorder, per-server local disk. Empty on a fresh server: that is
        # the point — Lesson 3 rehydrates it. NEVER a shared volume here.
        volumes = ["/data/nodes/node-3/pg:/var/lib/postgresql/data:z"]
      }
      template {
        # The database password lives in a Variable, never in the jobspec.
        data        = <<-EOT
          POSTGRES_USER=recorder
          POSTGRES_DB=recorder
          POSTGRES_PASSWORD={{ with nomadVar "nodes/node-3/pg" }}{{ .password }}{{ end }}
        EOT
        destination = "secrets/pg.env"
        env         = true
      }
      resources { cpu = 500  memory = 512 }
    }

    task "apphost" {
      driver = "podman"
      config {
        image        = "localhost/recorder-apphost:latest"
        network_mode = "host"
        volumes = [
          "/data/nodes/node-3/archive:/data/archive:z",   # recordings stay LOCAL
          "/data/nodes/node-3/config:/data/config:z",
        ]
      }
      # Workload identity: the task gets a token it can use against the
      # Variables API — to read its own Variable and to CAS the epoch.
      identity {
        env  = true
        file = true
      }
      template {
        # Step 2 of the rehydration sequence, delivered by the scheduler:
        # "I am recorder 3; my configuration is this object at this revision."
        data        = <<-EOT
          {{ with nomadVar "nodes/node-3" }}
          NODE_ID={{ .node }}
          CONFIG_OBJECT={{ .config }}
          CONFIG_REVISION={{ .revision }}
          CAMERA_IDS={{ .cameras }}
          {{ end }}
          DATABASE_URL=postgresql://recorder:{{ with nomadVar "nodes/node-3/pg" }}{{ .password }}{{ end }}@127.0.0.1:5432/recorder
          OBJECT_STORE_URL=http://minio.service.consul:9000/cluster-restore   # or any S3-compatible endpoint on this cluster
          ARCHIVE_DIR=/data/archive
          COLUMN_KEY_FILE=/data/config/column.key
        EOT
        destination = "local/node.env"
        env         = true
        change_mode = "restart"
      }
      resources { cpu = 2000  memory = 2048 }   # from the probe: B + 50 x I, rounded up
    }
  }
}
