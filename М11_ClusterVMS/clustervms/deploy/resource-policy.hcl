# deploy/resource-policy.hcl — bound to job resource's workload identity.
# The platform's resource writes its own heartbeat and reads the mirror
# knob and every subsystem's retention rows. It never writes any
# subsystem's configuration. Mirrors go resource to resource over HTTP,
# not through any store.
namespace "default" {
  variables {
    path "objects/platform/resources/*" { capabilities = ["write", "read", "list"] }
    path "platform/mirror"              { capabilities = ["read"] }
    path "*/retention"                  { capabilities = ["read"] }
    path "*/retention/*"                { capabilities = ["read"] }
    path "rec/recordings/*"             { capabilities = ["read", "list"] }   # the recorder's registered pass: media retention per recording row
  }
}
