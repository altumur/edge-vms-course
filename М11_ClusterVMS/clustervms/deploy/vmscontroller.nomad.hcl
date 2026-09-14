# deploy/vmscontroller.nomad.hcl — the controller: one, and safe at two.
# count = 1 is economy, not correctness: correctness is CAS, and a second
# instance would only repeat the same five-second pass. It has no HTTP —
# nothing asks it anything — and no Nomad client: it needs no more of the
# API than any task gets, Variables under its own prefixes (the policy).
job "vmscontroller" {
  datacenters = ["room-a"]
  type        = "service"

  group "vmscontroller" {
    count = 1
    task "vmscontroller" {
      driver = "podman"
      identity { env = true }
      config {
        image        = "localhost/clustervms:latest"
        network_mode = "host"
        args         = ["python3", "-m", "cluster", "controller"]
      }
      env {
        OBJECTS = "variables://objects"          # heartbeats and the snapshot as Variables; no MinIO on this cluster
        CLUSTER = "room-a"
      }
      resources { cpu = 200  memory = 128 }
    }
  }
}
