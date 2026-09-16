# deploy/recworker-policy.hcl — bound to job recworker's workload identity.
# A recorder writes its epochs (by CAS, when it starts a recording) and its
# slot (by CAS, when it claims r-<i>) and nothing else — never a recording
# row, never placement, never anything under vms/. It READS vms/: the
# worker's heartbeat is where the camera's fan-out is.
namespace "default" {
  variables {
    path "rec/epoch/*"   { capabilities = ["write", "read", "list"] }
    path "rec/slots/*"   { capabilities = ["write", "read", "list"] }
    path "objects/rec/*" { capabilities = ["write", "read", "list"] }   # its heartbeat, as an object-as-Variable
    path "rec/*"         { capabilities = ["read", "list"] }
    path "objects/vms/*" { capabilities = ["read", "list"] }            # the workers' heartbeats: live_url
    path "vms/*"         { capabilities = ["read", "list"] }
  }
}
