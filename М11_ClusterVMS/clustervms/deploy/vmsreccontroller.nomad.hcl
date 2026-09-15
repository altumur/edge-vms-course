# deploy/vmsreccontroller.nomad.hcl — the recorder's controller: one, and
# safe at two, exactly as vmscontroller. The same class over rec.subsystem.yaml
# (`SpecController(REC_SPEC)`): it places recordings on recorders whose server's
# resource answers, one recorder per server by default (rec/policy), and moves
# a dead server's recordings when its slot lapsed AND its resource is silent.
job "vmsreccontroller" {
  datacenters = ["room-a"]
  type        = "service"

  group "vmsreccontroller" {
    count = 1
    task "vmsreccontroller" {
      driver = "podman"
      identity { env = true }
      config {
        image        = "localhost/clustervms:latest"
        network_mode = "host"
        args         = ["python3", "-m", "cluster", "reccontroller"]
      }
      env {
        OBJECTS = "variables://objects"
      }
      resources { cpu = 200  memory = 128 }
    }
  }
}
