# deploy/recworker-policy.hcl — bound to job recworker's workload identity.
# A recorder writes its epochs (by CAS, when it starts a recording) and its
# slot (by CAS, when it claims r-<i>) and nothing else — never a recording
# row, never placement, never anything under vms/. It READS vms/: the
# worker's heartbeat is where the camera's fan-out is.
namespace "default" {
  variables {
    path "rec/epoch/*"   { capabilities = ["write", "read", "list"] }
    path "rec/slots/*"   { capabilities = ["write", "read", "list"] }
    path "rec/holds/*"   { capabilities = ["write", "read", "list"] }   # the volume it took (М10B Lesson 10): a claim about this process, like its slot
    path "objects/rec/heartbeats/*" { capabilities = ["write", "read", "list"] }   # its heartbeat, as an object-as-Variable
    path "objects/rec/*" { capabilities = ["read", "list"] }            # the snapshot and the blobs: read, never written by a recorder
    path "rec/*"         { capabilities = ["read", "list"] }
    path "objects/vms/*" { capabilities = ["read", "list"] }            # the workers' heartbeats: live_url
    path "vms/*"         { capabilities = ["read", "list"] }
  }
}
