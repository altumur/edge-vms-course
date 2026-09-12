package cluster

// Lesson 3 — the one-way publication upward, and what the operator is told.
//
//	object first, then the Variable (by CAS) — a pointer never dangles
//	publish on change, with a floor — the RPO is the floor
//	acknowledge on local commit, and SHOW durability: the `replicated` condition
//
// The directory is each recorder's off-box backup. It never writes back.

import (
	"errors"
	"fmt"
	"log"
	"strconv"
	"strings"
)

type Publisher struct {
	recorder    string
	Store   Store
	Vars    Variables
	Objects ObjectStore
	Floor   float64
	Clock   Clock

	PublishedRev int64
	LastPublish  float64
	FailedSince  float64 // < 0: the last attempt succeeded
	Publishes    int
}

func NewPublisher(node string, store Store, v Variables, objects ObjectStore, floor float64, clock Clock) *Publisher {
	return &Publisher{recorder: node, Store: store, Vars: v, Objects: objects, Floor: floor, Clock: clock,
		LastPublish: -1e9, FailedSince: -1}
}

// PublishOnce publishes if the local revision moved past what the directory
// holds. True if a publish happened or nothing was needed; false if it was
// attempted and failed (the recorder keeps running, the edit stays local).
func (p *Publisher) PublishOnce() bool {
	rev, err := p.Store.ConfigRevision()
	if err != nil {
		return p.failed(err)
	}
	if rev <= p.PublishedRev {
		return true
	}
	if p.Clock()-p.LastPublish < p.Floor {
		return true // the floor: not yet
	}
	blob, err := p.Store.DumpConfig()
	if err != nil {
		return p.failed(err)
	}
	key := fmt.Sprintf("%s/rev-%d", p.recorder, rev)
	if err := p.Objects.Put(key, blob); err != nil { // 1. the object, first
		return p.failed(err)
	}
	items, idx, err := p.Vars.Get("nodes/" + p.recorder)
	if err != nil {
		return p.failed(err)
	}
	if items == nil {
		items = Items{}
	}
	cams, err := p.Store.Cameras()
	if err != nil {
		return p.failed(err)
	}
	ids := make([]string, 0, len(cams))
	for _, c := range cams {
		ids = append(ids, strconv.FormatInt(c.ID, 10))
	}
	items["node"], items["config"] = p.recorder, key
	items["revision"], items["cameras"] = strconv.FormatInt(rev, 10), strings.Join(ids, ",")
	if _, err := p.Vars.Put("nodes/"+p.recorder, items, idx); err != nil { // 2. then the pointer
		if errors.Is(err, ErrConflict) {
			log.Printf("publish: cas conflict on nodes/%s — somebody else wrote our Variable; retrying next tick", p.recorder)
			return false
		}
		return p.failed(err)
	}
	p.PublishedRev, p.LastPublish, p.FailedSince = rev, p.Clock(), -1
	p.Publishes++
	return true
}

func (p *Publisher) failed(err error) bool {
	if p.FailedSince < 0 {
		p.FailedSince = p.Clock()
	}
	log.Printf("publish failed (%v); the recorder keeps running, the edit stays local", err)
	return false
}

// Replicated is the `replicated` condition for the console: status and reason.
func (p *Publisher) Replicated(localRev int64) (bool, string) {
	if localRev <= p.PublishedRev {
		return true, ""
	}
	if p.FailedSince >= 0 {
		return false, fmt.Sprintf("not yet replicated: directory unreachable for %.0fs", p.Clock()-p.FailedSince)
	}
	return false, fmt.Sprintf("not yet replicated (%d edit(s) pending)", localRev-p.PublishedRev)
}
