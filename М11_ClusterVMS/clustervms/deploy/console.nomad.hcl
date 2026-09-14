# deploy/console.nomad.hcl — the console: the page, the API, the eventindex.
# count = 2 because a person is waiting on it, and it is stateless: every
# instance reads the same raft and the same heartbeats and rebuilds its
# index from the resources on start. Placed on servers that run a resource
# so that an operator's marks have a bucket to go into; drop the constraint
# and /marks answers 503 on a server without one.
job "console" {
  datacenters = ["room-a"]
  type        = "service"

  group "console" {
    count = 2
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
      resources { cpu = 300  memory = 256 }
    }
  }
}
