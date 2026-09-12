# The live gateway: one subscription per camera to the owning worker's live tee, N
# browser sessions out. Runs in EVERY cluster. This is the one place a
# specific server comes back into the design — as a CONSTRAINT: a gateway
# that transcodes wants the GPU; one that faces the internet wants the
# public address. Nomad places it there because of them; nobody types its name.
job "live-gateway" {
  datacenters = ["*"]
  type        = "service"

  group "gateway" {
    count = 1

    constraint {
      attribute = "${meta.gpu}"
      operator  = "is_set"
    }
    constraint {
      attribute = "${meta.public_addr}"
      operator  = "is_set"
    }

    network {
      port "whep"  { static = 8444 }
      port "turn"  { static = 3478 }
    }

    service { name = "live-gateway"; port = "whep"; provider = "nomad" }

    task "gateway" {
      driver = "podman"
      config {
        image   = "vms/live-gateway:latest"
        devices = ["/dev/dri:/dev/dri"]     # the GPU, for transcoding what a browser cannot decode
      }
      identity { env = true }
      template {
        data        = <<-EOT
          NOMAD_ADDR=http://127.0.0.1:4646
          DIRECTORY=vms/workers/            # where is camera N -> which worker's tee to subscribe to (url from its heartbeat)
          PUBLIC_ADDR={{ env "meta.public_addr" }}
          UPSTREAM_QUEUE=30                 # frames; leaky — a stalled viewer loses frames, never stalls the worker
        EOT
        destination = "local/gateway.env"
        env         = true
      }
      resources { cpu = 4000  memory = 2048 }
    }
  }
}
