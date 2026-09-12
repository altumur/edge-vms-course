# The console: UI, API façade, the read model, TLS, token verification.
# Runs in EVERY cluster (a single-cluster customer has it with no domain);
# the domain cluster's instance is the same image pointed at every
# cluster's stores. Stateless; count = 2; placed anywhere.
job "console" {
  datacenters = ["*"]
  type        = "service"

  group "console" {
    count = 2

    network { port "https" { static = 8443 } }

    service {
      name     = "console"
      port     = "https"
      provider = "nomad"
      check { type = "http"; path = "/healthz"; interval = "10s"; timeout = "2s" }
    }

    task "console" {
      driver = "podman"
      config { image = "vms/domainvms:latest"; args = ["python3", "-m", "domain.console"] }
      identity { env = true }    # reads vms/snapshot, vms/*/heartbeat and domain/*; forwards writes to the owning cluster
      template {
        data        = <<-EOT
          # One line per cluster the console aggregates. The cluster-level
          # console lists its own cluster only; the domain's lists all.
          CLUSTERS=north=http://nomad.north:4646|http://minio.north:9000/cluster-restore,south=http://nomad.south:4646|http://minio.south:9000/cluster-restore
          LOST_AFTER=45
          REFRESH_INTERVAL=5
        EOT
        destination = "local/console.env"
        env         = true
      }
      resources { cpu = 500  memory = 256 }
    }
  }
}
