# deploy/console-policy.hcl — bound to job console's workload identity.
# The operator's rows and nothing else: a camera's row, the id counter, the
# retention row derived from it, the administrator's policy knob — and the
# same for the recorder it fronts at /rec/… (a recording's row: the Record
# toggle). Not an assignment, not a placement, not a slot — a console that
# could place would be a second controller with a browser in front of it.
# verify-bench.sh proves the "nothing else".
namespace "default" {
  variables {
    path "vms/cameras/*"   { capabilities = ["write", "read", "list"] }
    path "vms/next_id"     { capabilities = ["write", "read"] }
    path "vms/retention/*" { capabilities = ["write", "read", "list", "destroy"] }
    path "vms/idem/*"      { capabilities = ["write", "read", "list", "destroy"] }   # a retried POST, answered the same by any instance
    path "vms/policy"      { capabilities = ["write", "read"] }                      # servers: shared | distinct
    path "vms/*"           { capabilities = ["read", "list"] }
    path "rec/recordings/*" { capabilities = ["write", "read", "list"] }
    path "rec/next_id"     { capabilities = ["write", "read"] }
    path "rec/idem/*"      { capabilities = ["write", "read", "list", "destroy"] }
    path "rec/policy"      { capabilities = ["write", "read"] }
    path "rec/*"           { capabilities = ["read", "list"] }
    path "objects/vms/blobs/*" { capabilities = ["write", "read", "list"] }      # the bytes of a blob field, beside the row that names them
    path "objects/rec/blobs/*" { capabilities = ["write", "read", "list"] }
    path "objects/*"       { capabilities = ["read", "list"] }
    path "platform/*"      { capabilities = ["read", "list"] }
  }
}
