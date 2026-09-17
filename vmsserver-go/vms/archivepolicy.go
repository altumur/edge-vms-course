package vms

// ArchivePolicy — what the archive is to the RESOURCE process, as opposed to what it is to the recorder.
// The recorder writes into the tree (archive.go); this is the pass over that tree that nobody writing it
// performs: repair the manifests, retain media per the recording's own row, and answer the watermark's
// one question, "free N bytes" (space.go).

import (
	p "vmsserver/w2cplatform"
)

// ArchivePolicy is what the RECORDER registers with the platform's resource
// job (the "rec" hook): repair the manifests, retain media per camera from
// the recording rows (rec/recordings/<cam>, retention_days; 30 for a camera
// with no row).
type ArchivePolicy struct {
	Res  *ArchiveResource
	Vars p.Variables
	// Only Free needs these: the peers' heartbeats say who writes what and who has room, and the client
	// carries the bytes. Absent, the policy still repairs and retains — an archive on a box with no
	// neighbours has nowhere to evacuate to and does not pretend otherwise.
	Objects p.ObjectStore
	Peers   SegmentPeer
	Server  string
}

// Free is the resource's watermark answered in the recorder's own terms: evacuate what is not ours, then
// cut above the floor, then report the shortfall. The order is in space.go, and so is why.
func (ap *ArchivePolicy) Free(need int64, now, minDays float64) map[string]any {
	var freed int64
	out := map[string]any{}
	if ap.Objects != nil && ap.Peers != nil && ap.Server != "" {
		rep := Evacuate(ap.Res, ap.Objects, ap.Peers, ap.Vars, ap.Server, need, now, 45, 0)
		freed += rep.Freed
		out["evacuated"] = rep.Moved
		if rep.Skipped != nil {
			out["skipped"] = rep.Skipped
		}
	}
	if freed < need {
		cut, removed := Cut(ap.Res, need-freed, now, minDays)
		freed += cut
		out["cut"] = removed
	}
	if short := need - freed; short > 0 { // everything on the floor: said out loud, not cut into
		out["shortfall"] = short
	}
	out["freed"] = freed
	return out
}

func (ap *ArchivePolicy) Pass(now float64) map[string]any {
	rep := ap.Res.Repair()
	removed := 0
	for _, unit := range ap.Res.Units() {
		items, _, _ := ap.Vars.Get(Sub + "/recordings/" + unit) // the unit's own row: its retention, not the camera's
		days := 30
		if items != nil {
			days = atoiDef(items["retention_days"], 30)
		}
		removed += ap.Res.Retain(unit, float64(days), now)
	}
	return map[string]any{"added": rep.Added, "dropped": rep.Dropped, "media_removed": removed}
}
