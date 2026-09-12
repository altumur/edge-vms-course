package cluster

// Lesson 4 — the numbers this module exports, in Prometheus text form.
//
//	node_failover_seconds{kind="last"|"worst"}   the RTO. Worst case is the one that matters.
//	node_epoch_conflicts                          zero forever on a healthy cluster. Alarm on it anyway.
//	node_config_replicated                        1 if the directory holds the local revision
//	node_lease_seconds_left                       how long this instance may still write without a renewal

import "fmt"

func RenderMetrics(h *ClusterAppHost) string {
	n := h.Identity.recorder
	conflicts, left := 0, 0.0
	if h.Lease != nil {
		conflicts, left = h.Lease.Conflicts, h.Lease.SecondsLeft()
	}
	rep := 0
	if h.ReplicatedNow {
		rep = 1
	}
	return fmt.Sprintf(`# HELP node_failover_seconds Power pulled to recording resumed. Report the worst case.
# TYPE node_failover_seconds gauge
node_failover_seconds{node=%q,kind="last"} %.1f
node_failover_seconds{node=%q,kind="worst"} %.1f
# HELP node_epoch_conflicts Times a stale instance of this recorder was fenced. Should be zero forever.
# TYPE node_epoch_conflicts counter
node_epoch_conflicts{node=%q} %d
# HELP node_epoch The epoch this instance records into.
# TYPE node_epoch gauge
node_epoch{node=%q} %d
# HELP node_config_replicated 1 if the directory holds this recorder's current configuration revision.
# TYPE node_config_replicated gauge
node_config_replicated{node=%q} %d
# HELP node_lease_seconds_left Seconds this instance may still write without renewing its lease.
# TYPE node_lease_seconds_left gauge
node_lease_seconds_left{node=%q} %.1f
`, n, h.Failover["last"], n, h.Failover["worst"], n, conflicts, n, h.Settings.Epoch, n, rep, n, left)
}
