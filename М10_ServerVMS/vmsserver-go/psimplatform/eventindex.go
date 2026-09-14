package psimplatform

// eventindex — the event "database", which is a cache. A platform job.
//
// Events are observations: written by the worker that holds a unit's epoch,
// into that unit's bucket on its server's resource. Cross-unit search needs
// an index over all of them, and this is it: one per cluster (or per box),
// holding a table it can rebuild entirely by re-reading every resource's
// buckets. (The Python version keeps the table in SQLite; this one keeps it
// in memory — it is a cache either way, and the standard library has no
// SQLite.) It knows which subsystems exist by what it finds in the
// resources' heartbeats. It knows nothing about what an event means: `cam`
// is a field an event may carry, indexed if present.
//
//	Rebuild(resources)   read every subsystem's closed buckets on every resource and index them
//	Tail(resources)      the same, for buckets it has not seen (by (server, path))
//	Query(...)           subsystem, unit, cam, kind, time window; Unreachable names silent resources

import (
	"fmt"
	"io"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ResourceReader: HTTP against the resource job; tests substitute a directory reader.
type ResourceReader interface {
	Buckets(url, sub, unit string) ([]Bucket, error)
	Events(url string, b Bucket) ([]Event, error)
	Mirrored(url, server string) ([]Bucket, error)
	MirroredEvents(url, server string, b Bucket) ([]Event, error)
}

type HTTPResourceReader struct{ Client *http.Client }

func NewHTTPResourceReader() *HTTPResourceReader {
	return &HTTPResourceReader{&http.Client{Timeout: 3 * time.Second}}
}

func (r *HTTPResourceReader) get(u string) ([]byte, error) {
	resp, err := r.Client.Get(u)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("GET %s: %d", u, resp.StatusCode)
	}
	return io.ReadAll(resp.Body)
}

func parseEvents(raw []byte) []Event {
	var out []Event
	for _, l := range strings.Split(string(raw), "\n") {
		if strings.TrimSpace(l) == "" {
			continue
		}
		var e Event
		if err := jsonUnmarshal([]byte(l), &e); err == nil {
			out = append(out, e)
		}
	}
	return out
}

func (r *HTTPResourceReader) Buckets(url, sub, unit string) ([]Bucket, error) {
	raw, err := r.get(url + "/buckets/" + sub + "/" + unit)
	if err != nil {
		return nil, err
	}
	return ParseBucketLines(strings.NewReader(string(raw)))
}

func (r *HTTPResourceReader) Events(url string, b Bucket) ([]Event, error) {
	raw, err := r.get(url + "/events/" + b.Path)
	if err != nil {
		return nil, err
	}
	return parseEvents(raw), nil
}

func (r *HTTPResourceReader) Mirrored(url, server string) ([]Bucket, error) {
	raw, err := r.get(url + "/mirrored/" + server)
	if err != nil {
		return nil, err
	}
	return ParseBucketLines(strings.NewReader(string(raw)))
}

func (r *HTTPResourceReader) MirroredEvents(url, server string, b Bucket) ([]Event, error) {
	raw, err := r.get(url + "/events/" + MirrorDir + "/" + server + "/" + b.Path)
	if err != nil {
		return nil, err
	}
	return parseEvents(raw), nil
}

// IndexedEvent is one row of the index. Cam is nil when the event named no
// cam and its unit is not numeric.
type IndexedEvent struct {
	Subsystem, Unit string
	Cam             *int
	Epoch           int
	T               float64
	Kind, Server    string
	Bucket          string
	Fenced          bool
	Fields          map[string]any
}

// CamIs is the test's shorthand: a Cam that is set and equals n.
func (e IndexedEvent) CamIs(n int) bool { return e.Cam != nil && *e.Cam == n }

type IndexReport struct {
	Added       int
	Unreachable []string
	FromMirror  []string
	Segments    int
}

type QueryResult struct {
	Events []IndexedEvent
	State  string
}

// Query parameters; nil pointers mean "any".
type Query struct {
	T0, T1        float64
	Cam           *int
	Kind          *string
	Subsystem     *string
	Unit          *string
	CurrentEpochs map[[2]string]int // (subsystem, unit) -> epoch: fencing is per unit
	Limit         int
}

func IntPtr(n int) *int       { return &n }
func StrPtr(s string) *string { return &s }

type EventIndex struct {
	mu              sync.Mutex
	Reader          ResourceReader
	Wall            Clock
	LostAfter       float64
	State           string
	IndexedSegments int
	seen            map[[2]string]bool
	rows            []IndexedEvent
}

func NewEventIndex(reader ResourceReader, wall Clock) *EventIndex {
	if wall == nil {
		wall = Wall()
	}
	return &EventIndex{Reader: reader, Wall: wall, LostAfter: 45, State: "empty", seen: map[[2]string]bool{}}
}

// Rebuild from nothing: what a failed-over instance does first.
func (x *EventIndex) Rebuild(resources map[string]ResourceHeartbeat) IndexReport {
	x.mu.Lock()
	x.seen, x.rows, x.IndexedSegments = map[[2]string]bool{}, nil, 0
	x.mu.Unlock()
	return x.tail(resources, true)
}

func (x *EventIndex) Tail(resources map[string]ResourceHeartbeat) IndexReport {
	return x.tail(resources, false)
}

func camOf(e Event, unit string) *int {
	if v, ok := e["cam"]; ok {
		n := int(ToFloat(v))
		return &n
	}
	if n, err := strconv.Atoi(unit); err == nil {
		return &n
	}
	return nil
}

func (x *EventIndex) insert(server string, b Bucket, events []Event) int {
	x.mu.Lock()
	defer x.mu.Unlock()
	n := 0
	for _, e := range events {
		fields := map[string]any{}
		for k, v := range e {
			if k != "t" && k != "kind" && k != "cam" {
				fields[k] = v
			}
		}
		x.rows = append(x.rows, IndexedEvent{b.Subsystem, b.Unit, camOf(e, b.Unit), b.Epoch, e.T(), e.Kind(), server, b.Path, false, fields})
		n++
	}
	x.seen[[2]string{server, b.Path}] = true
	x.IndexedSegments++
	return n
}

func (x *EventIndex) hasSeen(server, path string) bool {
	x.mu.Lock()
	defer x.mu.Unlock()
	return x.seen[[2]string{server, path}]
}

func (x *EventIndex) tail(resources map[string]ResourceHeartbeat, rebuild bool) IndexReport {
	if rebuild {
		x.State = "catching up"
	}
	now := x.Wall()
	rep := IndexReport{Unreachable: []string{}, FromMirror: []string{}}
	live := map[string]ResourceHeartbeat{}
	var servers []string
	for s, hb := range resources {
		servers = append(servers, s)
		if now-hb.Ts <= x.LostAfter {
			live[s] = hb
		}
	}
	sort.Strings(servers)
	for _, server := range servers {
		hb := resources[server]
		if _, ok := live[server]; !ok {
			if added, ok := x.fromMirror(server, live); ok {
				rep.FromMirror = append(rep.FromMirror, server)
				rep.Added += added
			} else {
				rep.Unreachable = append(rep.Unreachable, server)
			}
			continue
		}
		if err := x.indexServer(server, hb, &rep); err != nil { // fresh heartbeat, server not answering
			rep.Unreachable = append(rep.Unreachable, server)
		}
	}
	state := "live"
	if len(rep.Unreachable) > 0 {
		state += "; " + strings.Join(rep.Unreachable, ", ") + " unreachable"
	}
	if len(rep.FromMirror) > 0 {
		state += "; " + strings.Join(rep.FromMirror, ", ") + " from mirror"
	}
	x.State = state
	rep.Segments = x.IndexedSegments
	return rep
}

func (x *EventIndex) indexServer(server string, hb ResourceHeartbeat, rep *IndexReport) error {
	var subs []string
	for s := range hb.Units {
		subs = append(subs, s)
	}
	sort.Strings(subs)
	for _, sub := range subs {
		for _, unit := range hb.Units[sub] {
			bs, err := x.Reader.Buckets(hb.URL, sub, unit)
			if err != nil {
				return err
			}
			for _, b := range bs {
				if b.Events == 0 || x.hasSeen(server, b.Path) {
					continue
				}
				evs, err := x.Reader.Events(hb.URL, b)
				if err != nil {
					return err
				}
				rep.Added += x.insert(server, b, evs)
			}
		}
	}
	return nil
}

// fromMirror: a silent server's closed buckets, from whichever live peer
// holds copies. Rows are inserted under the REAL server: only the source
// differed. Never used while the resource answers.
func (x *EventIndex) fromMirror(server string, live map[string]ResourceHeartbeat) (int, bool) {
	var holders []ResourceHeartbeat
	for _, hb := range live {
		if _, ok := hb.Mirrors[server]; ok {
			holders = append(holders, hb)
		}
	}
	if len(holders) == 0 {
		return 0, false
	}
	added := 0
	for _, hb := range holders {
		bs, err := x.Reader.Mirrored(hb.URL, server)
		if err != nil {
			continue // that peer is not answering either
		}
		for _, b := range bs {
			if b.Events == 0 || x.hasSeen(server, b.Path) {
				continue
			}
			evs, err := x.Reader.MirroredEvents(hb.URL, server, b)
			if err != nil {
				break
			}
			added += x.insert(server, b, evs)
		}
	}
	return added, true
}

func (x *EventIndex) Query(q Query) QueryResult {
	x.mu.Lock()
	defer x.mu.Unlock()
	limit := q.Limit
	if limit == 0 {
		limit = 1000
	}
	var out []IndexedEvent
	for _, r := range x.rows {
		if r.T < q.T0 || r.T >= q.T1 {
			continue
		}
		if q.Cam != nil && !r.CamIs(*q.Cam) {
			continue
		}
		if q.Kind != nil && r.Kind != *q.Kind {
			continue
		}
		if q.Subsystem != nil && r.Subsystem != *q.Subsystem {
			continue
		}
		if q.Unit != nil && r.Unit != *q.Unit {
			continue
		}
		if cur, ok := q.CurrentEpochs[[2]string{r.Subsystem, r.Unit}]; ok {
			r.Fenced = r.Epoch < cur
		}
		out = append(out, r)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].T < out[j].T })
	if len(out) > limit {
		out = out[:limit]
	}
	if out == nil {
		out = []IndexedEvent{}
	}
	return QueryResult{out, x.State}
}

// Forget: retention on a resource removed a segment; its events go with it.
func (x *EventIndex) Forget(server string, paths []string) int {
	x.mu.Lock()
	defer x.mu.Unlock()
	gone := map[string]bool{}
	for _, p := range paths {
		gone[p] = true
		delete(x.seen, [2]string{server, p})
	}
	var keep []IndexedEvent
	for _, r := range x.rows {
		if !(r.Server == server && gone[r.Bucket]) {
			keep = append(keep, r)
		}
	}
	x.rows = keep
	return len(paths)
}

// ToMap renders an indexed event as the console's JSON row.
func (e IndexedEvent) ToMap() map[string]any {
	m := map[string]any{"subsystem": e.Subsystem, "unit": e.Unit, "epoch": e.Epoch, "t": e.T, "kind": e.Kind,
		"server": e.Server, "bucket": e.Bucket, "fenced": e.Fenced}
	if e.Cam != nil {
		m["cam"] = *e.Cam
	} else {
		m["cam"] = nil
	}
	for k, v := range e.Fields {
		m[k] = v
	}
	return m
}
