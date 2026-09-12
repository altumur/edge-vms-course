package cluster

// Lesson 5 — the cluster directory you already built.
//
// Every recorder's Variable lists its camera ids. Scan nodes/ and you have
// answered "where is camera 7" — tens of entries, one raft, strongly
// consistent inside the cluster. Cached briefly by the console; invalidated
// by time, because there is nothing else to invalidate it with and a few
// seconds of staleness is the cost of not reading raft on every page load.

import (
	"fmt"
	"strings"
	"sync"
)

type Holding struct {
	Cameras  []int64 `json:"cameras"`
	Config   string  `json:"config"`
	Revision int64   `json:"revision"`
}

type Directory struct {
	Vars  Variables
	TTL   float64
	Clock Clock

	mu    sync.Mutex
	cache map[string]Holding
	at    float64
}

func NewDirectory(v Variables, ttl float64, clock Clock) *Directory {
	return &Directory{Vars: v, TTL: ttl, Clock: clock, at: -1e9}
}

// Scan returns node id -> holdings, from the cache while it is fresh.
func (d *Directory) Scan(force bool) (map[string]Holding, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if !force && d.cache != nil && d.Clock()-d.at < d.TTL {
		return d.cache, nil
	}
	paths, err := d.Vars.List("nodes/")
	if err != nil {
		return nil, err
	}
	out := map[string]Holding{}
	for _, p := range paths {
		if strings.Count(p, "/") != 1 { // nodes/<node> only, not nodes/<node>/epoch
			continue
		}
		items, _, err := d.Vars.Get(p)
		if err != nil {
			return nil, err
		}
		if items == nil || items["node"] == "" {
			continue
		}
		out[items["node"]] = Holding{Cameras: parseIDs(items["cameras"]), Config: items["config"], Revision: atoi(items["revision"])}
	}
	d.cache, d.at = out, d.Clock()
	return out, nil
}

// Where answers "which recorder has camera N" — "" for none, an error for two,
// because two is one-writer-per-key not being enforced, not a tie to break.
func (d *Directory) Where(camera int64) (string, error) {
	scan, err := d.Scan(false)
	if err != nil {
		return "", err
	}
	var hits []string
	for n, h := range scan {
		for _, c := range h.Cameras {
			if c == camera {
				hits = append(hits, n)
			}
		}
	}
	if len(hits) > 1 {
		return "", fmt.Errorf("camera %d listed by %v: one-writer-per-key is not being enforced", camera, hits)
	}
	if len(hits) == 0 {
		return "", nil
	}
	return hits[0], nil
}

func (d *Directory) Holdings(node string) ([]int64, error) {
	scan, err := d.Scan(false)
	if err != nil {
		return nil, err
	}
	return scan[node].Cameras, nil
}
