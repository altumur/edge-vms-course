# deploy/vmsreccontroller-policy.hcl — bound to job vmsreccontroller's workload identity.
# The only writer of the recorder's PLACEMENT — which recorder writes which
# camera's footage. Never a recording row (the console's), never the VMS's.
namespace "default" {
  variables {
    path "rec/workers/*"   { capabilities = ["write", "read", "list"] }
    path "rec/placement/*" { capabilities = ["write", "read", "list"] }
    path "rec/slots/*"     { capabilities = ["write", "read", "list"] }
    path "objects/*"       { capabilities = ["read", "list"] }
    path "*"               { capabilities = ["read", "list"] }
  }
}
