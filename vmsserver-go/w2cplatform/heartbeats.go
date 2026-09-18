package w2cplatform

import (
	"sort"
	"strings"
)

// Reading heartbeats — the one thing BOTH sides of the platform do. A controller reads them to know who
// is alive and what they carry; a worker reads them to find the process holding what it needs (the
// recorder's source is the fan-out of whoever holds the camera, from its heartbeat and never a call).
//
// So this is contract, not console: it is the shape of a heartbeat and the freshness rule over it, and
// it is the surface a second language has to keep byte for byte.

// Heartbeats: every worker's last heartbeat for a subsystem, whatever its age — the read model's source.
//
// `sub` is the subsystem's name ("vms", or "vms/" — both spellings the callers already use). Where its
// heartbeats LIVE is this function's business and not its callers': before they lived under
// `<name>/heartbeats/`, every caller listed `<name>/` and filtered by a suffix, and the filter was not
// the point — it was the price of a key layout that mixed them in with everything else.
func Heartbeats(objects ObjectStore, sub string) map[string]Heartbeat {
	out := map[string]Heartbeat{}
	keys, _ := objects.List(strings.TrimSuffix(sub, "/") + "/" + HeartbeatsDir + "/")
	for _, k := range keys {
		raw, _ := objects.Get(k)
		if raw != nil {
			if hb, err := HeartbeatFromBytes(raw); err == nil {
				out[hb.Worker] = hb
			}
		}
	}
	return out
}

// Holders: the heartbeats fresh enough to act on. Heartbeats returns every
// worker's last one whatever its age, which is right for a read model — a
// console must be able to say "silent for 4 minutes". It is wrong for anything
// that then goes and TALKS to that worker: a subscriber, a playback door, a
// backfill fetch. Those want this.
func Holders(objects ObjectStore, prefix string, now, lostAfter float64) map[string]Heartbeat {
	if lostAfter == 0 {
		lostAfter = 45
	}
	out := map[string]Heartbeat{}
	for w, hb := range Heartbeats(objects, prefix) {
		if now-hb.Ts <= lostAfter {
			out[w] = hb
		}
	}
	return out
}

// HolderQuery narrows HolderOf past "is it held at all".
type HolderQuery struct {
	LostAfter float64 // 0 = 45s
	Phase     string  // "" = any phase
	Field     string  // "" = any: else the status entry must carry a non-empty value there
}

// Held is the answer HolderOf gives: the process holding a unit right now, its
// heartbeat, and that unit's entry in it.
type Held struct {
	Worker string
	HB     Heartbeat
	Status map[string]any
}

// HolderOf: who holds `unit` right now, or ok=false. Phase narrows it further
// when the caller needs the unit to be DOING something and not merely held — a
// recorder subscribes to a fan-out only in `running`, while a playback door
// answers in `held` too.
func HolderOf(objects ObjectStore, prefix, unit string, now float64, q HolderQuery) (Held, bool) {
	hs := Holders(objects, prefix, now, q.LostAfter)
	names := make([]string, 0, len(hs))
	for w := range hs {
		names = append(names, w)
	}
	sort.Strings(names) // two holders of one unit is a bug being fenced; answer the same way every time
	for _, w := range names {
		for _, st := range hs[w].Status {
			if Str(st["id"]) != unit {
				continue
			}
			if q.Phase != "" && Str(st["phase"]) != q.Phase {
				continue
			}
			if q.Field != "" {
				v, present := st[q.Field]
				if !present || v == nil || Str(v) == "" {
					continue // no door published: no answer, rather than a broken URL
				}
			}
			return Held{w, hs[w], st}, true
		}
	}
	return Held{}, false
}
