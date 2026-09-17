// Package vms — what the archive does when the disk is full: the recorder's answer to the resource's one
// question, "free N bytes" (w2cplatform/resource.go, Relieve).
//
// Retention by days is a promise to the operator: thirty days of camera 7. This file is what happens when
// the promise cannot be kept, and it is deliberately not the same thing. Three steps, in this order:
//
//  1. give up what is not ours    a unit whose recorder now writes on another server: send it there
//  2. cut above the floor         from the unit with the most days over min_days, oldest first
//  3. say the shortfall out loud  everything on the floor and still no room: a number, not a quiet cut
//
// Step 1 is why there is no separate "evacuation" job, schedule or button. A recording written here while
// the owner's server was down is not lost, not wrong, and not urgent — the console already merges
// timelines across resources. It becomes work only when the disk it sits on needs the space, and then the
// server that needs it is the one that acts.
package vms

import (
	"os"
	"path/filepath"
	"sort"

	p "vmsserver/w2cplatform"
)

const (
	MaxEvacSegments = 50  // one pass moves a batch, not an archive: the next pass continues
	RoomMargin      = 0.9 // never fill the destination's last tenth — that is its own watermark's air
)

// DepthDays: how many days of footage a unit has HERE, from the manifest — the operator's real answer to
// "how far back does camera 7 go", which retention_days only promises.
func DepthDays(archive *ArchiveResource, unit string, now float64) float64 {
	segs := NewManifest(archive.Root, unit).Read()
	if len(segs) == 0 {
		return 0
	}
	oldest := segs[0].Start
	for _, s := range segs {
		if s.Start < oldest {
			oldest = s.Start
		}
	}
	return (now - oldest) / 86400
}

// UnitBytes: what a unit occupies here — from the manifest's bytes, not from the disk. Every line already
// carries the size, so the answer costs a read of one file instead of a walk of the tree.
func UnitBytes(archive *ArchiveResource, unit string) int64 {
	var total int64
	for _, s := range NewManifest(archive.Root, unit).Read() {
		total += s.Bytes
	}
	return total
}

// Foreign: units on this disk whose recorder is now on ANOTHER server -> that server. No event, no outage
// journal, no "recovery mode": what is on the disk (Units) against who holds it (the heartbeats). The same
// comparison answers the operator's "where is camera 7's footage" and drives the evacuation below.
func Foreign(archive *ArchiveResource, objects p.ObjectStore, server string, now, lostAfter float64) map[string]string {
	out := map[string]string{}
	for _, unit := range archive.Units() {
		held, ok := p.HolderOf(objects, Sub+"/", unit, now, p.HolderQuery{LostAfter: lostAfter})
		if !ok {
			continue // nobody holds it: not ours to send anywhere
		}
		where := held.HB.ExtraString("server", "")
		if where != "" && where != server {
			out[unit] = where
		}
	}
	return out
}

// EvacReport is what one pass of step 1 managed.
type EvacReport struct {
	Freed   int64
	Moved   int
	Skipped map[string]string
}

// Evacuate is step 1: push a bounded batch of a foreign unit's segments to the server that writes it now,
// then delete locally only what that server's own manifest confirms it has. The deletion follows an
// observed fact, not a 204: a copy that never arrived is a copy we still hold.
//
// Push and not pull, unlike backfill: there the initiative belongs to whoever lacks something, and here
// what is lacking is space — on the source. Waiting for the neighbour to notice would make solving our
// problem depend on someone else's timer.
//
// The destination's free space is read from its heartbeat FIRST. Evacuating onto a disk that is itself
// tight moves the problem and invites the pair to trade gigabytes back and forth; a destination with no
// room — or one the operator is about to stop — is skipped, and step 2 answers instead.
func Evacuate(archive *ArchiveResource, objects p.ObjectStore, peers SegmentPeer, vars p.Variables,
	server string, need int64, now, lostAfter float64, maxSegments int) EvacReport {
	if maxSegments == 0 {
		maxSegments = MaxEvacSegments
	}
	rep := EvacReport{Skipped: map[string]string{}}
	seen := p.ResourcesSeen(objects)
	drains := ""
	if vars != nil {
		drains = p.Draining(vars)
	}
	away := Foreign(archive, objects, server, now, lostAfter)
	units := make([]string, 0, len(away))
	for u := range away {
		units = append(units, u)
	}
	sort.Strings(units)
	for _, unit := range units {
		if rep.Freed >= need {
			break
		}
		to := away[unit]
		hb, ok := seen[to]
		if !ok || now-hb.Ts > lostAfter {
			rep.Skipped[unit] = to + " silent"
			continue
		}
		if to == drains { // about to stop: do not hand it gigabytes first
			rep.Skipped[unit] = to + " draining"
			continue
		}
		room := int64(float64(hb.Space.Free) * RoomMargin)
		man := NewManifest(archive.Root, unit)
		segs := man.Read()
		sort.Slice(segs, func(i, j int) bool {
			if segs[i].Start != segs[j].Start {
				return segs[i].Start < segs[j].Start
			}
			return segs[i].Epoch < segs[j].Epoch
		})
		sent := map[string]bool{}
		var size int64
		for _, s := range segs {
			if rep.Freed+size >= need || len(sent) >= maxSegments {
				break
			}
			if size+s.Bytes > room {
				rep.Skipped[unit] = to + " has no room"
				break
			}
			data, err := os.ReadFile(filepath.Join(archive.Root, s.Path))
			if err != nil {
				rep.Skipped[unit] = err.Error()
				break
			}
			if err := peers.PutSegment(hb.URL, s.Path, data, s.Line()); err != nil {
				rep.Skipped[unit] = err.Error()
				break
			}
			sent[s.Path] = true
			size += s.Bytes
		}
		if len(sent) == 0 {
			continue
		}
		there := Confirmed(peers, hb.URL, unit)
		keep := []Segment{}
		var gone int64
		for _, s := range segs {
			if sent[s.Path] && there[s.Path] {
				os.Remove(filepath.Join(archive.Root, s.Path))
				gone += s.Bytes
				rep.Moved++
			} else {
				keep = append(keep, s)
			}
		}
		if gone > 0 {
			man.Rewrite(keep)
			rep.Freed += gone
		}
	}
	if len(rep.Skipped) == 0 {
		rep.Skipped = nil
	}
	return rep
}

// SegmentPeer is how one archive hands a segment to another: the VMS's own two calls over the routes the
// console already uses to draw a timeline. Tests substitute an in-process client.
type SegmentPeer interface {
	PutSegment(url, rel string, data []byte, line string) error
	Manifest(url, unit string) ([]byte, error)
}

// Confirmed: what the destination says it HAS — the paths in its manifest for this unit. A read, over the
// same route the console uses; nothing was added for the sake of the check. Unreachable now means confirm
// nothing and therefore delete nothing; the next pass asks again.
func Confirmed(peers SegmentPeer, url, unit string) map[string]bool {
	out := map[string]bool{}
	body, err := peers.Manifest(url, unit)
	if err != nil {
		return out
	}
	for _, line := range splitLines(string(body)) {
		if seg, err := SegmentFromLine(line); err == nil {
			out[seg.Path] = true
		}
	}
	return out
}

// Cut is step 2: take from whoever has the most days over the floor, oldest segment first, ONE at a time
// so the choice is made again after every deletion — the unit that was deepest stops being deepest, and
// the loss spreads instead of falling on one camera.
//
// Not "the oldest segments on the resource": that empties the camera with the longest retention, which is
// the one the operator cared most about. Not "the biggest file": that empties the camera with the highest
// bitrate, which is usually the same camera.
func Cut(archive *ArchiveResource, need int64, now, minDays float64) (freed int64, removed int) {
	for freed < need {
		best, slack := "", 0.0
		for _, unit := range archive.Units() {
			if over := DepthDays(archive, unit, now) - minDays; over > slack {
				best, slack = unit, over
			}
		}
		if best == "" {
			break // everything is on the floor
		}
		man := NewManifest(archive.Root, best)
		segs := man.Read()
		if len(segs) == 0 {
			break
		}
		sort.Slice(segs, func(i, j int) bool {
			if segs[i].Start != segs[j].Start {
				return segs[i].Start < segs[j].Start
			}
			return segs[i].Epoch < segs[j].Epoch
		})
		s := segs[0]
		os.Remove(filepath.Join(archive.Root, s.Path))
		man.Rewrite(segs[1:])
		freed += s.Bytes
		removed++
	}
	return freed, removed
}

func splitLines(s string) []string {
	out := []string{}
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '\n' {
			if line := trimSpace(s[start:i]); line != "" {
				out = append(out, line)
			}
			start = i + 1
		}
	}
	if line := trimSpace(s[start:]); line != "" {
		out = append(out, line)
	}
	return out
}

func trimSpace(s string) string {
	for len(s) > 0 && (s[0] == ' ' || s[0] == '\t' || s[0] == '\r' || s[0] == '\n') {
		s = s[1:]
	}
	for len(s) > 0 && (s[len(s)-1] == ' ' || s[len(s)-1] == '\t' || s[len(s)-1] == '\r' || s[len(s)-1] == '\n') {
		s = s[:len(s)-1]
	}
	return s
}
