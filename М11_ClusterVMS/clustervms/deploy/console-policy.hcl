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
    path "vms/sweep"       { capabilities = ["write", "read"] }                   # what the blob sweep marked, and when
    path "rec/sweep"       { capabilities = ["write", "read"] }
    # Requests (М10A Lesson 24, М10B Lessons 16 and 21) — an operator asking a worker to DO something: open
    # a relay, fetch a range from a card. The console writes them and removes what a worker reports done,
    # hence `destroy`. And the volumes (М10B Lesson 10): the administrator's list of archives, created and
    # deleted from the page. Both named by `acl_console()` since they were written, and absent here until the
    # policies were checked against the code again.
    path "vms/requests/*"  { capabilities = ["write", "read", "list", "destroy"] }
    path "rec/requests/*"  { capabilities = ["write", "read", "list", "destroy"] }
    path "rec/volumes/*"   { capabilities = ["write", "read", "list", "destroy"] }
    # `destroy` — the only grant in this cluster that lets anything remove an object, and it is bounded to
    # the one prefix whose contents can be proved unreferenced (М10A Lesson 29). Heartbeats and snapshot
    # shards are deliberately NOT here: a worker reads the heartbeat its previous instance left to measure
    # its own failover, so collecting them would collect the measurement.
    path "objects/vms/blobs/*" { capabilities = ["write", "read", "list", "destroy"] }   # the bytes of a blob field, beside the row that names them
    path "objects/rec/blobs/*" { capabilities = ["write", "read", "list", "destroy"] }
    path "objects/*"       { capabilities = ["read", "list"] }
    # The operator's planned stop (М10A Lesson 22): `acl_console()` has named this row since it was
    # written, and this file did not — so a drain would have been refused on a cluster, and the machine
    # the operator meant to take out gently would have gone out as a silence instead. Nothing noticed
    # until the policies were checked against the code.
    path "platform/drain"  { capabilities = ["write", "read"] }
    path "platform/*"      { capabilities = ["read", "list"] }
  }
}
