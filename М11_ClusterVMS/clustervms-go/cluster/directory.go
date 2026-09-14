package cluster

// Where is camera 7 — answered from the cluster in one scan.
//
// vms/workers/* is the assignment, written by the controller into one raft
// and read by every worker from the same raft; scanning it is the cluster
// directory, and it is consistent because it is one store. М12 aggregates
// several of these and cannot be — which is why the question is answered here.

import (
	"sort"
	"strconv"
	"strings"
	"sync"

	p "vmsserver/psimplatform"
)

type Directory struct {
	mu     sync.Mutex
	Vars   Variables
	TTL    float64
	Clock  p.Clock
	Prefix string
	cache  map[string][]string
	at     float64
	Scans  int
}

func NewDirectory(vars Variables, ttl float64, clock p.Clock) *Directory {
	if clock == nil {
		clock = p.Monotonic()
	}
	if ttl == 0 {
		ttl = 5
	}
	return &Directory{Vars: vars, TTL: ttl, Clock: clock, Prefix: "vms/workers/", at: -1e9}
}

func (d *Directory) Scan(force bool) map[string][]string {
	d.mu.Lock()
	defer d.mu.Unlock()
	if force || d.Clock()-d.at >= d.TTL {
		out := map[string][]string{}
		paths, _ := d.Vars.List(d.Prefix)
		for _, pth := range paths {
			w := strings.TrimPrefix(pth, d.Prefix)
			items, _, _ := d.Vars.Get(pth)
			out[w] = p.AssignmentFromItems(w, items).Units
		}
		d.cache, d.at, d.Scans = out, d.Clock(), d.Scans+1
	}
	return d.cache
}

// Where: the worker; "" when nobody lists it; "w-1+w-2" during a
// reassignment window — two rows list the camera, and the directory says so.
func (d *Directory) Where(cameraID int) string {
	var hits []string
	unit := strconv.Itoa(cameraID)
	for w, units := range d.Scan(false) {
		for _, u := range units {
			if u == unit {
				hits = append(hits, w)
			}
		}
	}
	sort.Strings(hits)
	return strings.Join(hits, "+")
}

func (d *Directory) Holdings(worker string) []int {
	out := []int{}
	for _, u := range d.Scan(false)[worker] {
		n, _ := strconv.Atoi(u)
		out = append(out, n)
	}
	sort.Ints(out)
	return out
}
