# reference/node-3-policy.hcl — Lesson 2/28: recorder 3 may write ONLY under
# its own prefix. Bind it to the job's workload identity:
#   nomad acl policy apply -namespace default -job node-3 node-3 node-3-policy.hcl
# This is what makes "one writer per key" a property of the cluster rather
# than a convention — and it is the open question the module asks you to
# verify on the bench before building on it.
namespace "default" {
  variables {
    path "nodes/node-3" {
      capabilities = ["read", "write"]
    }
    path "nodes/node-3/*" {
      capabilities = ["read", "write"]
    }
    path "nodes/*" {
      capabilities = ["read"]            # Lesson 5: scanning the directory is reading everyone's
    }
  }
}
