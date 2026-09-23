# deploy/vms-scaler-policy.hcl — the token `vms-scaler` runs with.
#
# `scale-job` and the reads that go with it. Not `submit-job`: this agent changes a number on a job that
# already exists and must not be able to replace what that job IS. Not `read-logs`, not `alloc-exec`, no
# variables — it never reads state, only counts allocations and scales.
#
# Nomad OSS scopes ACL at the namespace, so this grants scaling on every job in the namespace, not on
# `recworker` alone. A cluster that needs that narrower puts the recorder in a namespace of its own; the
# alternative — Enterprise's job-level ACL — is not something a course should require. Written down
# because the gap between what the policy says and what was intended is exactly where a review stops.
namespace "default" {
  policy       = "read"
  capabilities = ["scale-job"]
}

# No `node` block: it does not place anything and does not need to see the fleet.
# No `operator` or `acl` block: it hands out nothing and configures nothing.
