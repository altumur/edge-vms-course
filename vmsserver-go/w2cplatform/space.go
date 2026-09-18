package w2cplatform

// The disk, and the two marks on it. Both sides of the platform need this, for different halves of the
// same question: the RESOURCE asks "am I over the mark" before it starts freeing, and a WORKER asks it
// before it fetches more (backfill on a full disk chases the resource that is deleting behind it).
//
// The probe is a seam because a test cannot fill a disk.

const SpaceKey = "platform/space"

// DiskFree is the volume under the root, not the tree on it.
type DiskFree struct {
	Total int64   `json:"total"`
	Free  int64   `json:"free"`
	Used  int64   `json:"used"`
	Full  float64 `json:"full"`
}

// SpaceSettings is the watermark. high and low are USED fractions: over high the resource starts freeing
// and stops at low, and the gap between them is the whole point — one mark alone gives a saw, a file
// freed and a file written, for ever. Choose the gap in HOURS OF INGEST, not in percent. min_days is the
// floor no unit is cut below; everything on the floor and still no room is a shortfall, said out loud.
type SpaceSettings struct {
	Enabled bool
	High    float64
	Low     float64
	MinDays float64
}

func GetSpaceSettings(vars Variables) SpaceSettings {
	items, _, _ := vars.Get(SpaceKey)
	out := SpaceSettings{Enabled: items != nil && items["enabled"] == "true", High: 0.85, Low: 0.75, MinDays: 3}
	if v, ok := items["high"]; ok {
		out.High = ToFloat(v)
	}
	if v, ok := items["low"]; ok {
		out.Low = ToFloat(v)
	}
	if v, ok := items["min_days"]; ok {
		out.MinDays = ToFloat(v)
	}
	return out
}
