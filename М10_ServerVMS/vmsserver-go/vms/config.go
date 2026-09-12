// Package vms is the VMS — the first subsystem the platform hosts.
//
//	reconciler.go   М9 Lesson 6's loop, unchanged: desired persisted, actual derived
//	archive.go      the archive as a resource: spool → promote → manifest; retention as a policy
//	worker.go       vmsworker — DriverPack as the worker: N pipelines against an assignment
//	controller.go   vmscontroller — the only writer of vms/*: cameras, assignment, placement
//	console.go      the one-box console: the read model from heartbeats; writes go to the controller
//
// Nothing here imports from vmsplatform except through its public
// interfaces, and nothing in vmsplatform imports from here.
package vms

// The VMS's schema, as items in the platform's config store.
//
//	vms/cameras/<id>      id, name, source, enabled, retention_days, events_retention_days, priority, labels, ref, revision
//	vms/workers/<worker>  units, rev
//	vms/placement/<id>    worker, reason, at, rev
//	vms/epoch/<id>        epoch                        (a worker takes, by CAS)
//	vms/next_id           n

import (
	"strconv"
	"strings"

	p "vmsserver/vmsplatform"
)

var OperatorFields = []string{"name", "source", "enabled", "retention_days", "events_retention_days", "priority", "labels", "ref"}

// ref: the name a layer above knows this camera by — the domain's id (М12).
// labels: where the camera is reachable from — "vlan:cctv-a" — matched
// against the labels a worker reports from its server.
var ForbiddenFields = []string{"worker", "placement", "epoch", "revision", "observed_revision", "phase", "id"}

// Camera is a row. Epoch is not a column: the worker's gate sets it on the
// copy it hands the actuator.
type Camera struct {
	ID                  int
	Name, Source        string
	Enabled             bool
	RetentionDays       int
	EventsRetentionDays int
	Priority            int
	Revision            int
	Labels              []string
	Ref                 string
	Epoch               int
}

func atoiDef(s string, def int) int {
	if n, err := strconv.Atoi(s); err == nil {
		return n
	}
	return def
}

func Row(items p.Items) Camera {
	var labels []string
	for _, l := range strings.Split(items["labels"], ",") {
		if l != "" {
			labels = append(labels, l)
		}
	}
	if labels == nil {
		labels = []string{}
	}
	enabled := true
	if v, ok := items["enabled"]; ok {
		enabled = v == "true"
	}
	return Camera{ID: atoiDef(items["id"], 0), Name: items["name"], Source: items["source"], Enabled: enabled,
		RetentionDays: atoiDef(items["retention_days"], 30), EventsRetentionDays: atoiDef(items["events_retention_days"], 365),
		Priority: atoiDef(items["priority"], 100), Revision: atoiDef(items["revision"], 1), Labels: labels, Ref: items["ref"]}
}

func ItemsOf(c Camera) p.Items {
	return p.Items{"id": strconv.Itoa(c.ID), "name": c.Name, "source": c.Source, "enabled": p.Str(c.Enabled),
		"retention_days": strconv.Itoa(c.RetentionDays), "events_retention_days": strconv.Itoa(c.EventsRetentionDays),
		"priority": strconv.Itoa(c.Priority), "revision": strconv.Itoa(c.Revision), "labels": strings.Join(c.Labels, ","), "ref": c.Ref}
}

// ToMap renders a row as the console's JSON.
func (c Camera) ToMap() map[string]any {
	return map[string]any{"id": c.ID, "name": c.Name, "source": c.Source, "enabled": c.Enabled, "retention_days": c.RetentionDays,
		"events_retention_days": c.EventsRetentionDays, "priority": c.Priority, "revision": c.Revision, "labels": c.Labels, "ref": c.Ref}
}
