package vms

// ArchivePolicy — what the archive is to the RESOURCE process, as opposed to what it is to the recorder.
// The recorder writes into the tree (archive.go); this is the pass over that tree that nobody writing it
// performs: repair the manifests, retain media per the recording's own row, and answer the watermark's
// one question, "free N bytes" (space.go).

import (
	"sort"
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
	// One archive tree per VOLUME on a box with several disks. The single-disk case is this map empty and
	// nobody naming anything: the policy that was written before there were volumes reads the same.
	Volumes map[string]*ArchiveResource
}

// resOn is the tree the resource asked about: the named volume, or the one archive there is.
func (ap *ArchivePolicy) resOn(volume string) *ArchiveResource {
	if r, ok := ap.Volumes[volume]; ok && volume != "" {
		return r
	}
	return ap.Res
}

// FreeOn is Free on the disk that is short. The resource measured ONE volume, so the answer has to come
// off that volume: bytes freed on another disk of the same box close nothing, because the recording that
// cannot write is on this one.
func (ap *ArchivePolicy) FreeOn(need int64, now, minDays float64, volume string) map[string]any {
	return ap.free(ap.resOn(volume), need, now, minDays)
}

// Free is the resource's watermark answered in the recorder's own terms: evacuate what is not ours, then
// cut above the floor, then report the shortfall. The order is in space.go, and so is why.
func (ap *ArchivePolicy) Free(need int64, now, minDays float64) map[string]any {
	return ap.free(ap.Res, need, now, minDays)
}

func (ap *ArchivePolicy) free(res *ArchiveResource, need int64, now, minDays float64) map[string]any {
	var freed int64
	out := map[string]any{}
	if ap.Objects != nil && ap.Peers != nil && ap.Server != "" {
		rep := Evacuate(res, ap.Objects, ap.Peers, ap.Vars, ap.Server, need, now, 45, 0)
		freed += rep.Freed
		out["evacuated"] = rep.Moved
		if rep.Skipped != nil {
			out["skipped"] = rep.Skipped
		}
	}
	if freed < need {
		cut, removed := Cut(res, need-freed, now, minDays)
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
	trees := []*ArchiveResource{ap.Res}
	if len(ap.Volumes) > 0 {
		trees = trees[:0]
		for _, name := range sortedVolumeNames(ap.Volumes) {
			trees = append(trees, ap.Volumes[name])
		}
	}
	added, dropped, removed := 0, 0, 0
	for _, res := range trees {
		rep := res.Repair()
		added, dropped = added+rep.Added, dropped+rep.Dropped
		for _, unit := range res.Units() {
			items, _, _ := ap.Vars.Get(Sub + "/recordings/" + unit) // the unit's own row: its retention, not the camera's
			days := 30
			if items != nil {
				days = atoiDef(items["retention_days"], 30)
			}
			removed += res.Retain(unit, float64(days), now)
		}
	}
	return map[string]any{"added": added, "dropped": dropped, "media_removed": removed}
}

func sortedVolumeNames(m map[string]*ArchiveResource) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
