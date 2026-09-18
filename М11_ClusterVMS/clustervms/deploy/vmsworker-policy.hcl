# deploy/vmsworker-policy.hcl — bound to job vmsworker's workload identity.
# A worker writes its epochs (by CAS, when it starts a camera) and its slot
# (by CAS, when it claims a name) and nothing else. verify-bench.sh proves
# the "nothing else" from inside an allocation.
namespace "default" {
  variables {
    path "vms/epoch/*"  { capabilities = ["write", "read", "list"] }
    path "vms/slots/*"  { capabilities = ["write", "read", "list"] }
    path "objects/vms/heartbeats/*" { capabilities = ["write", "read", "list"] }   # its heartbeat, as an object-as-Variable
    path "objects/vms/*" { capabilities = ["read", "list"] }            # the snapshot and the blobs: read, never written by a worker
    path "vms/*"        { capabilities = ["read", "list"] }
  }
}
