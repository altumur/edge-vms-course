# deploy/vmscontroller-policy.hcl — bound to job vmscontroller's workload identity.
# The only writer of PLACEMENT: which worker runs which camera, a released
# slot's redistribution, an operator's `retire`, and the snapshot that leaves
# the cluster. It never writes a camera row — those are the console's.
namespace "default" {
  variables {
    path "vms/workers/*"        { capabilities = ["write", "read", "list"] }
    path "vms/placement/*"      { capabilities = ["write", "read", "list"] }
    path "vms/slots/*"          { capabilities = ["write", "read", "list"] }
    path "objects/vms/snapshot/*" { capabilities = ["write", "read", "list"] }   # one object per worker (М10A Lesson 25)
    path "objects/*"            { capabilities = ["read", "list"] }
    path "*"                    { capabilities = ["read", "list"] }
  }
}
