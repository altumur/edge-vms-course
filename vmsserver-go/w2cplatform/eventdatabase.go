package w2cplatform

// eventdatabase — the event "database", which is a cache. Part of the
// resource job: one per RESOURCE, over that resource's own tree — its
// buckets, and the copies it holds of its peers' closed buckets
// (.mirror/<server>/). Nothing per console, nothing cluster-wide: a console
// with a question asks every live resource's GET /events and merges the
// answers by time (MergedIndex), on one box and on a cluster alike.
//
// Events are observations: written by the worker that holds a unit's epoch,
// into that unit's bucket on its server's resource. The database is a table
// the resource job can rebuild entirely by re-reading its tree (the Python
// version keeps it in SQLite; this one in memory — a cache either way). It
// knows which subsystems exist by the directories it finds. It knows nothing
// about what an event means: `cam` is a field an event may carry, indexed if
// present.
//
//	NewEventDatabase(root, server)   the resource job's: rebuilt on start, tailed every few seconds
//	Rebuild() / Tail()               read the tree: closed buckets once, open ones by the lines past what is held
//	Query(...)                       subsystem, unit, cam, kind, time window
//	Forget(server, paths)            retention removed a bucket: its rows go with it (the resource calls this)
//	MergedIndex                      what a console has instead: every live resource's /events, merged

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// IndexedEvent is one row of the database. Cam is nil when the event named no
// cam and its unit is not numeric.
type IndexedEvent struct {
	Subsystem, Unit string
	Cam             *int
	Epoch           int
	T               float64
	Kind, Server    string
	Bucket          string
	Fenced          bool
	// EpochIs: "current", "fenced", or "earlier-run". Same comparison, two meanings — a writer that lost
	// the race, or a finished earlier run of work that ends. Fenced is kept, computed from this, so a page
	// written before the difference existed keeps working.
	EpochIs string
	Fields  map[string]any
}

// CamIs is the test's shorthand: a Cam that is set and equals n.
func (e IndexedEvent) CamIs(n int) bool { return e.Cam != nil && *e.Cam == n }

// DBReport is what one rebuild or tail did.
type DBReport struct {
	Added    int
	Segments int
	Mirrored []string // servers whose copies this resource holds
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
	// EpochPolicy: subsystem -> "fenced" | "earlier-run". What an older epoch MEANS there. The database is
	// told; it does not decide. A subsystem nobody named keeps the default, which is the old behaviour.
	EpochPolicy map[string]string
	Limit       int
}

func IntPtr(n int) *int       { return &n }
func StrPtr(s string) *string { return &s }

// EventQuerier is what a console's /events needs behind it: the box's or the
// cluster's MergedIndex, or a database directly.
type EventQuerier interface {
	Query(q Query) QueryResult
}

// EventDatabase: the event database of ONE resource — its own buckets and the
// mirror copies it holds. Not a subsystem: a cache with nothing to place.
type EventDatabase struct {
	mu              sync.Mutex
	Root, Server    string
	Wall            Clock
	BucketSeconds   int
	Interval        time.Duration
	State           string
	IndexedSegments int
	seen            map[[2]string]int // (server, path) -> lines held
	rows            []IndexedEvent
	stop            chan struct{}
}

func NewEventDatabase(root, server string, wall Clock, bucketSeconds int) *EventDatabase {
	if wall == nil {
		wall = Wall()
	}
	if bucketSeconds == 0 {
		bucketSeconds = 600
	}
	return &EventDatabase{Root: root, Server: server, Wall: wall, BucketSeconds: bucketSeconds, Interval: 3 * time.Second,
		State: "empty", seen: map[[2]string]int{}, stop: make(chan struct{})}
}

// Rebuild from nothing: what a restarted resource job does first.
func (d *EventDatabase) Rebuild() DBReport {
	d.mu.Lock()
	d.seen, d.rows, d.IndexedSegments = map[[2]string]int{}, nil, 0
	d.mu.Unlock()
	return d.tail(true)
}

func (d *EventDatabase) Tail() DBReport { return d.tail(false) }

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

// ingest one bucket: the lines past what is held, under the real owning server.
func (d *EventDatabase) ingest(server string, b Bucket, file string) int {
	d.mu.Lock()
	defer d.mu.Unlock()
	have, known := d.seen[[2]string{server, b.Path}]
	if b.Events <= have { // nothing new: a closed bucket never grows, an open one may
		return 0
	}
	evs := ReadBucket(file)
	if len(evs) < have {
		return 0
	}
	n := 0
	for _, e := range evs[have:] {
		fields := map[string]any{}
		for k, v := range e {
			if k != "t" && k != "kind" && k != "cam" {
				fields[k] = v
			}
		}
		d.rows = append(d.rows, IndexedEvent{b.Subsystem, b.Unit, camOf(e, b.Unit), b.Epoch, e.T(), e.Kind(), server, b.Path, false, "current", fields})
		n++
	}
	d.seen[[2]string{server, b.Path}] = have + n
	if !known {
		d.IndexedSegments++
	}
	return n
}

func (d *EventDatabase) tail(rebuild bool) DBReport {
	if rebuild {
		d.State = "catching up"
	}
	rep := DBReport{Mirrored: []string{}}
	units := SubsystemsUnder(d.Root)
	var subs []string
	for s := range units {
		subs = append(subs, s)
	}
	sort.Strings(subs)
	for _, sub := range subs {
		for _, unit := range units[sub] {
			for _, b := range BucketsUnder(d.Root, sub, unit, d.BucketSeconds) {
				rep.Added += d.ingest(d.Server, b, filepath.Join(d.Root, b.Path))
			}
		}
	}
	for _, server := range MirroredServers(d.Root) {
		rep.Mirrored = append(rep.Mirrored, server)
		for _, b := range MirroredBuckets(d.Root, server, d.BucketSeconds) {
			rep.Added += d.ingest(server, b, filepath.Join(d.Root, MirrorDir, server, b.Path))
		}
	}
	d.State = "live"
	rep.Segments = d.IndexedSegments
	return rep
}

func (d *EventDatabase) Query(q Query) QueryResult {
	d.mu.Lock()
	defer d.mu.Unlock()
	return QueryResult{filterRows(d.rows, q), d.State}
}

func filterRows(rows []IndexedEvent, q Query) []IndexedEvent {
	limit := q.Limit
	if limit == 0 {
		limit = 1000
	}
	var out []IndexedEvent
	for _, r := range rows {
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
		r.EpochIs = "current"
		if cur, ok := q.CurrentEpochs[[2]string{r.Subsystem, r.Unit}]; ok && r.Epoch < cur {
			r.EpochIs = "fenced"
			if was, ok := q.EpochPolicy[r.Subsystem]; ok && was != "" {
				r.EpochIs = was
			}
		}
		r.Fenced = r.EpochIs == "fenced"
		out = append(out, r)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].T < out[j].T })
	if len(out) > limit {
		out = out[:limit]
	}
	if out == nil {
		out = []IndexedEvent{}
	}
	return out
}

// Forget: retention on the resource removed a bucket; its events go with it.
func (d *EventDatabase) Forget(server string, paths []string) int {
	d.mu.Lock()
	defer d.mu.Unlock()
	gone := map[string]bool{}
	for _, p := range paths {
		gone[p] = true
		delete(d.seen, [2]string{server, p})
	}
	var keep []IndexedEvent
	for _, r := range d.rows {
		if !(r.Server == server && gone[r.Bucket]) {
			keep = append(keep, r)
		}
	}
	d.rows = keep
	return len(paths)
}

// Start: rebuild now, then tail every Interval in a goroutine.
func (d *EventDatabase) Start() *EventDatabase {
	d.Rebuild()
	go func() {
		t := time.NewTicker(d.Interval)
		defer t.Stop()
		for {
			select {
			case <-d.stop:
				return
			case <-t.C:
				d.Tail()
			}
		}
	}()
	return d
}

func (d *EventDatabase) Stop() {
	select {
	case <-d.stop:
	default:
		close(d.stop)
	}
}

// ToMap renders an indexed event as the console's (and the resource's) JSON row.
func (e IndexedEvent) ToMap() map[string]any {
	was := e.EpochIs
	if was == "" {
		was = "current"
	}
	m := map[string]any{"subsystem": e.Subsystem, "unit": e.Unit, "epoch": e.Epoch, "t": e.T, "kind": e.Kind,
		"server": e.Server, "bucket": e.Bucket, "fenced": e.Fenced, "epoch_is": was}
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

// EventFromMap is the reverse: a JSON row from a resource's /events.
func EventFromMap(m map[string]any) IndexedEvent {
	e := IndexedEvent{Fields: map[string]any{}}
	e.Subsystem, _ = m["subsystem"].(string)
	e.Unit, _ = m["unit"].(string)
	e.Kind, _ = m["kind"].(string)
	e.Server, _ = m["server"].(string)
	e.Bucket, _ = m["bucket"].(string)
	e.Epoch = int(ToFloat(m["epoch"]))
	e.T = ToFloat(m["t"])
	e.Fenced, _ = m["fenced"].(bool)
	e.EpochIs, _ = m["epoch_is"].(string)
	if c, ok := m["cam"]; ok && c != nil {
		n := int(ToFloat(c))
		e.Cam = &n
	}
	for k, v := range m {
		switch k {
		case "subsystem", "unit", "kind", "server", "bucket", "epoch", "t", "fenced", "epoch_is", "cam":
		default:
			e.Fields[k] = v
		}
	}
	return e
}

// QueryResultToMap renders a reply as the JSON the routes send.
func (r QueryResult) ToMap() map[string]any {
	evs := make([]map[string]any, 0, len(r.Events))
	for _, e := range r.Events {
		evs = append(evs, e.ToMap())
	}
	return map[string]any{"events": evs, "state": r.State}
}

// QueryFromValues parses the /events query string a resource or a console receives.
func QueryFromValues(v url.Values) Query {
	q := Query{T0: 0, T1: 1e12, Limit: 1000}
	if s := v.Get("from"); s != "" {
		q.T0, _ = strconv.ParseFloat(s, 64)
	}
	if s := v.Get("to"); s != "" {
		q.T1, _ = strconv.ParseFloat(s, 64)
	}
	if s := v.Get("cam"); s != "" {
		n, _ := strconv.Atoi(s)
		q.Cam = &n
	}
	if s := v.Get("kind"); s != "" {
		q.Kind = StrPtr(s)
	}
	if s := v.Get("subsystem"); s != "" {
		q.Subsystem = StrPtr(s)
	}
	if s := v.Get("unit"); s != "" {
		q.Unit = StrPtr(s)
	}
	if s := v.Get("limit"); s != "" {
		q.Limit, _ = strconv.Atoi(s)
	}
	return q
}

func (q Query) values() url.Values {
	v := url.Values{}
	v.Set("from", strconv.FormatFloat(q.T0, 'f', -1, 64))
	v.Set("to", strconv.FormatFloat(q.T1, 'f', -1, 64))
	if q.Cam != nil {
		v.Set("cam", strconv.Itoa(*q.Cam))
	}
	if q.Kind != nil {
		v.Set("kind", *q.Kind)
	}
	if q.Subsystem != nil {
		v.Set("subsystem", *q.Subsystem)
	}
	if q.Unit != nil {
		v.Set("unit", *q.Unit)
	}
	if q.Limit > 0 {
		v.Set("limit", strconv.Itoa(q.Limit))
	}
	return v
}

// Fetch asks one resource: GET <url>/events?…; tests substitute a call into the resource's database.
type Fetch func(url string, q Query) (QueryResult, error)

// MergedIndex: what stands behind a console's /events — nothing of its own.
// Query asks every LIVE resource's GET /events (each answers from the
// database over its own tree), merges by time, dedupes a dead server's copies
// when two peers hold them, drops a copy when the owner is live (it answered
// for itself), fences by CurrentEpochs, and names in State the servers nobody
// answered for: "live; srv-a unreachable" for a resource that is silent and
// unmirrored (or live but not answering), "live; srv-a from mirror" when a
// peer's copy stood in. The same shape EventDatabase.Query returns.
type MergedIndex struct {
	Objects   ObjectStore
	Fetch     Fetch
	Wall      Clock
	LostAfter float64
	State     string
}

func NewMergedIndex(objects ObjectStore, fetch Fetch, wall Clock) *MergedIndex {
	if wall == nil {
		wall = Wall()
	}
	if fetch == nil {
		fetch = HTTPFetch(3 * time.Second)
	}
	return &MergedIndex{Objects: objects, Fetch: fetch, Wall: wall, LostAfter: 45, State: "live"}
}

// HTTPFetch: GET <url>/events?… over the network.
func HTTPFetch(timeout time.Duration) Fetch {
	client := &http.Client{Timeout: timeout}
	return func(u string, q Query) (QueryResult, error) {
		resp, err := client.Get(u + "/events?" + q.values().Encode())
		if err != nil {
			return QueryResult{}, err
		}
		defer resp.Body.Close()
		if resp.StatusCode != 200 {
			return QueryResult{}, fmt.Errorf("GET %s/events: %d", u, resp.StatusCode)
		}
		raw, _ := io.ReadAll(resp.Body)
		var body struct {
			Events []map[string]any `json:"events"`
			State  string           `json:"state"`
		}
		if err := json.Unmarshal(raw, &body); err != nil {
			return QueryResult{}, err
		}
		out := QueryResult{State: body.State, Events: []IndexedEvent{}}
		for _, m := range body.Events {
			out.Events = append(out.Events, EventFromMap(m))
		}
		return out, nil
	}
}

func (m *MergedIndex) Query(q Query) QueryResult {
	now := m.Wall()
	seen := ResourcesSeen(m.Objects)
	live := map[string]bool{}
	var servers []string
	for s, hb := range seen {
		servers = append(servers, s)
		if now-hb.Ts <= m.LostAfter {
			live[s] = true
		}
	}
	sort.Strings(servers)
	ask := q
	ask.CurrentEpochs = nil
	var events []IndexedEvent
	unreachable := map[string]bool{}
	fromMirror := map[string]bool{}
	have := map[string]bool{}
	for _, server := range servers {
		if !live[server] {
			continue
		}
		rep, err := m.Fetch(seen[server].URL, ask)
		if err != nil { // live by heartbeat, not answering
			unreachable[server] = true
			continue
		}
		for _, e := range rep.Events {
			if e.Server != server { // a copy this resource holds for a peer
				if live[e.Server] {
					continue // the owner answers for itself
				}
				key := fmt.Sprintf("%s|%s|%v|%s|%s", e.Server, e.Bucket, e.T, e.Kind, e.Unit)
				if have[key] {
					continue // two peers hold the same copy
				}
				have[key] = true
				fromMirror[e.Server] = true
			}
			events = append(events, e)
		}
	}
	for _, server := range servers {
		if !live[server] && !fromMirror[server] {
			unreachable[server] = true // silent, and nobody holds its copies
		}
	}
	out := filterRows(events, Query{T0: q.T0, T1: q.T1, CurrentEpochs: q.CurrentEpochs, EpochPolicy: q.EpochPolicy, Limit: q.Limit})
	state := "live"
	if len(unreachable) > 0 {
		state += "; " + strings.Join(sortedKeys(unreachable), ", ") + " unreachable"
	}
	if len(fromMirror) > 0 {
		state += "; " + strings.Join(sortedKeys(fromMirror), ", ") + " from mirror"
	}
	m.State = state
	return QueryResult{out, state}
}

func sortedKeys(m map[string]bool) []string {
	var out []string
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
