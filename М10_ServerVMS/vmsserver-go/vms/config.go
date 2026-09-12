// Package vms is the VMS — the first subsystem the platform hosts.
//
//	vms.subsystem.yaml   the spec: what the controller does for this subsystem — rows, fields, placement, snapshot
//	config.go            Camera, the typed view of a spec row, for the worker and the tests
//	reconciler.go        М9 Lesson 6's loop, unchanged: desired persisted, actual derived
//	archive.go           the archive as a resource: spool → promote → manifest; retention as a policy
//	worker.go            vmsworker — DriverPack as the worker: N pipelines against an assignment
//	controller.go        vmscontroller — the platform's SpecController run from the spec, in the VMS's words
//	console.go           the one-box console: the read model from heartbeats; writes go to the controller
//
// Nothing here imports from vmsplatform except through its public
// interfaces, and nothing in vmsplatform imports from here.
package vms

import (
	_ "embed"
	"strconv"

	p "vmsserver/vmsplatform"
)

//go:embed vms.subsystem.yaml
var specYAML string

// Spec is the VMS as the platform sees it; everything the controller does is here.
var Spec = mustSpec()

func mustSpec() *p.SubsystemSpec {
	v, err := p.ParseYAML(specYAML)
	if err != nil {
		panic(err)
	}
	s, err := p.SpecFromMap(v.(map[string]any))
	if err != nil {
		panic(err)
	}
	return s
}

var (
	OperatorFields  = Spec.FieldOrder
	ForbiddenFields = p.PlatformFields
)

// Camera is a row, typed. Epoch is not a column: the worker's gate sets it
// on the copy it hands the actuator.
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

func CameraOf(r p.Row) Camera {
	return Camera{ID: r.Int("id"), Name: r.String("name"), Source: r.String("source"), Enabled: r.Bool("enabled"),
		RetentionDays: r.Int("retention_days"), EventsRetentionDays: r.Int("events_retention_days"), Priority: r.Int("priority"),
		Revision: r.Int("revision"), Labels: r.List("labels"), Ref: r.String("ref")}
}

func RowOf(c Camera) p.Row {
	labels := c.Labels
	if labels == nil {
		labels = []string{}
	}
	return p.Row{"id": c.ID, "name": c.Name, "source": c.Source, "enabled": c.Enabled, "retention_days": c.RetentionDays,
		"events_retention_days": c.EventsRetentionDays, "priority": c.Priority, "revision": c.Revision, "labels": labels, "ref": c.Ref}
}

func Row(items p.Items) Camera { return CameraOf(Spec.RowOf(items)) }
func ItemsOf(c Camera) p.Items { return Spec.ItemsOf(RowOf(c)) }
func atoiDef(s string, def int) int {
	if n, err := strconv.Atoi(s); err == nil {
		return n
	}
	return def
}

// ToMap renders a row as the console's JSON.
func (c Camera) ToMap() map[string]any {
	return map[string]any(RowOf(c))
}
