# deploy/console-policy.hcl — bound to job console's workload identity.
# The operator's rows and nothing else: a camera's row, the id counter, the
# retention row derived from it. Not an assignment, not a placement, not a
# slot — a console that could place would be a second controller with a
# browser in front of it. verify-bench.sh proves the "nothing else".
namespace "default" {
  variables {
    path "vms/cameras/*"   { capabilities = ["write", "read", "list"] }
    path "vms/next_id"     { capabilities = ["write", "read"] }
    path "vms/retention/*" { capabilities = ["write", "read", "list", "destroy"] }
    path "vms/idem/*"      { capabilities = ["write", "read", "list", "destroy"] }   # a retried POST, answered the same by any instance
    path "vms/*"           { capabilities = ["read", "list"] }
    path "objects/*"       { capabilities = ["read", "list"] }
    path "platform/*"      { capabilities = ["read", "list"] }
  }
}
