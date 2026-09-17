// What a runtime hands a process — and nothing about WHICH runtime.
//
//	<ROLE>_NAME   the slot to claim outright: systemd's %i, a StatefulSet
//	              ordinal, an operator starting one by hand
//	SLOT_INDEX    else the index, which the process turns into `<prefix>-<n>`
//	SERVER_NAME   whose resource this process writes into, and the host in the
//	              URLs it publishes
//	LABELS        what this server can reach, comma-separated
//	INSTANCE_ID   this incarnation — what failover is measured from
//
// Not one of these names an orchestrator, and that is the whole point of the
// file. A Quadlet sets `WORKER_NAME=%i`; a Nomad jobspec maps
// `NOMAD_ALLOC_INDEX`, `node.unique.name` and `meta.labels` into these; a
// Kubernetes manifest maps the StatefulSet ordinal and a `fieldRef`. The loop
// reads five names and never learns which of them filled them in — the same
// rule this package already keeps for the stores (see OpenVars).
//
// The index is a PREFERENCE, never proof: whatever a runtime says, the slot is
// still taken by CAS, and a runtime that hands the same index twice loses the
// second claim rather than corrupting the first.
package w2cplatform

import (
	"os"
	"strconv"
	"strings"
)

// The five names, spelled once.
const (
	SlotIndexVar  = "SLOT_INDEX"
	ServerNameVar = "SERVER_NAME"
	LabelsVar     = "LABELS"
	InstanceIDVar = "INSTANCE_ID"
)

// Env is a process's environment. A nil Env is the real one, so tests hand in
// a map and production hands in nothing.
type Env map[string]string

func (e Env) Get(k string) string {
	if e == nil {
		return os.Getenv(k)
	}
	return e[k]
}

// SlotName is the slot to prefer: an explicit name, else `<prefix>-<index>`,
// else "" — "whichever is free, a lapsed one first", so a replacement inherits
// the assignment.
func SlotName(env Env, nameVar, prefix string) string {
	if n := env.Get(nameVar); n != "" {
		return n
	}
	if i := env.Get(SlotIndexVar); i != "" {
		n, err := strconv.Atoi(strings.TrimSpace(i))
		if err != nil {
			return ""
		}
		return prefix + "-" + strconv.Itoa(n)
	}
	return ""
}

// ServerOfEnv is the server this process runs on: what a runtime says, else
// this host's name. On one box the hostname is right and no runtime has to say
// anything.
func ServerOfEnv(env Env, given string) string {
	if given != "" {
		return given
	}
	if s := env.Get(ServerNameVar); s != "" {
		return s
	}
	h, err := os.Hostname()
	if err != nil {
		return "localhost"
	}
	return h
}

// LabelsOf: what this server can reach, comma-separated.
func LabelsOf(env Env, def string) []string {
	raw := env.Get(LabelsVar)
	if raw == "" {
		raw = def
	}
	out := []string{}
	for _, l := range strings.Split(raw, ",") {
		if l = strings.TrimSpace(l); l != "" {
			out = append(out, l)
		}
	}
	return out
}

// InstanceOf: this incarnation. "" lets the worker fall back to its own
// `hostname:pid:6hex`, which is enough to tell one instance from the next on a
// box.
func InstanceOf(env Env) string { return env.Get(InstanceIDVar) }
