# The same one-writer-per-prefix pattern М11's policies enforce for the VMS:
# a worker may write vms/<w>/* and its slot, the controller vms/*; the agent
# may write domain/*; nobody else.
namespace "default" {
  variables {
    path "domain/keys"    { capabilities = ["read", "write"] }
    path "domain/revoked" { capabilities = ["read", "write"] }
    path "domain/grants"  { capabilities = ["read", "write"] }
    path "domain/*"       { capabilities = ["read"] }
    path "vms/*"          { capabilities = ["deny"] }
  }
}
