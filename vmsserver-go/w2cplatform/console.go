package w2cplatform

// The console as data — the other half of spec.go. A subsystem's YAML already
// says what its units are, which fields the operator owns and what leaves the
// cluster; that is everything a console needs to list, edit and show them.
// So the console is one type, run from the same spec:
//
//	GET  /                       the page (console.html): the unit list, the edit form built from the spec's
//	                             fields, and — when the subsystem registered media routes — a timeline and a player
//	GET  /spec                   what the page reads first: name, rows, id rule, fields, media, metric names
//	GET  /<rows>                 {rows: the read model from every worker's heartbeat, configured: the units}
//	GET  /where/<id>             the stored placement (why) and the assignments' answer (where, one scan)
//	GET  /resources              the platform's resources: usage, units, live | silent
//	GET  /unplaceable            units nothing live can serve, with the labels that say why
//	GET  /events?from&to&unit&kind&subsystem   the resources' event databases, merged (MergedIndex), fenced by every subsystem's epochs
//	GET  /metrics                <name>_workers_live · _worker_headroom{worker,server} · _worker_load · _epoch_conflicts ·
//	                             _failover_seconds{kind="worst"} · _resources_live · <name>_<running> (the spec names the gauge)
//	POST /<rows>  (Idempotency-Key)   the row only — the controller places it on its next pass; the key is a Variable
//	                             (<sub>/idem/<key>), so the retry is answered the same by whichever console gets it
//	PUT  /<rows>/<id>            the operator's fields; a new revision; refused where the controller refuses
//	DELETE /<rows>/<id>          the row is marked; the controller's pass takes its placement back
//	POST /marks  (Idempotency-Key)    an operator's observation {unit|cam, note}: the CONSOLE's event, into
//	                             console/<instance>/… on this server's resource — never a worker's bucket
//
// What a subsystem adds is registered, not subclassed: an Extra (the same
// type the resource's server takes) gets every request the routes above do
// not claim — the VMS: /timeline and /segment. The console holds the
// subsystem's SpecController with the console's token — the operator's rows,
// never placement — so a write it should not make is a 403 from the store,
// not a rule in this file.

import (
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

//go:embed console.html
var Page []byte

func SendPage(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Content-Length", strconv.Itoa(len(Page)))
	w.Write(Page)
}

// SendFile serves a file whole or by Range — what a <video> element asks for.
func SendFile(w http.ResponseWriter, req *http.Request, path, contentType string) {
	st, err := os.Stat(path)
	if err != nil || st.IsDir() {
		SendJSON(w, 404, map[string]any{"detail": "no such segment", "error": "no such segment"})
		return
	}
	start, end := int64(0), st.Size()-1
	rng := req.Header.Get("Range")
	if strings.HasPrefix(rng, "bytes=") {
		a, b, _ := strings.Cut(rng[6:], "-")
		if a != "" {
			start, _ = strconv.ParseInt(a, 10, 64)
		}
		if b != "" {
			end, _ = strconv.ParseInt(b, 10, 64)
		}
	}
	f, err := os.Open(path)
	if err != nil {
		w.WriteHeader(404)
		return
	}
	defer f.Close()
	f.Seek(start, 0)
	data := make([]byte, end-start+1)
	n, _ := io.ReadFull(f, data)
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Accept-Ranges", "bytes")
	w.Header().Set("Content-Length", strconv.Itoa(n))
	if rng != "" {
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, end, st.Size()))
		w.WriteHeader(206)
	}
	w.Write(data[:n])
}

func SendJSON(w http.ResponseWriter, status int, body any) {
	raw, _ := json.Marshal(body)
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", strconv.Itoa(len(raw)))
	w.WriteHeader(status)
	w.Write(raw)
}

func SendText(w http.ResponseWriter, status int, body string) {
	w.Header().Set("Content-Type", "text/plain")
	w.Header().Set("Content-Length", strconv.Itoa(len(body)))
	w.WriteHeader(status)
	io.WriteString(w, body)
}

func ReadBody(req *http.Request) map[string]any {
	raw, _ := io.ReadAll(req.Body)
	out := map[string]any{}
	if len(raw) > 0 {
		json.Unmarshal(raw, &out)
	}
	return out
}

func LastSegment(path string) string { return path[strings.LastIndex(path, "/")+1:] }

func LastSegmentInt(path string) (int, error) { return strconv.Atoi(LastSegment(path)) }

// QueryRange reads ?from&to, defaulting to everything.
func QueryRange(req *http.Request) (float64, float64) {
	q := req.URL.Query()
	from, _ := strconv.ParseFloat(q.Get("from"), 64)
	to := 1e12
	if q.Get("to") != "" {
		to, _ = strconv.ParseFloat(q.Get("to"), 64)
	}
	return from, to
}

type Reply struct {
	Status int
	Body   any
}

// IdempotencyKeys: a retried POST must be the same POST whichever console
// answers it, so the key lives in the store, not in a process: <sub>/idem/<key>
// is claimed by a create-only CAS before the write and filled with the reply
// after it. A second instance that sees the claim waits for the reply and
// serves it; it never repeats the write. Keys older than TTL are pruned on
// the way past, at most once a minute.
type IdempotencyKeys struct {
	Vars   Variables
	Prefix string
	Wall   Clock
	TTL    float64
	Sleep  func(time.Duration)

	mu     sync.Mutex
	pruned time.Time
}

func NewIdempotencyKeys(vars Variables, prefix string, wall Clock) *IdempotencyKeys {
	return &IdempotencyKeys{Vars: vars, Prefix: prefix, Wall: wall, TTL: 86400, Sleep: time.Sleep}
}

var ErrBadKey = errors.New("Idempotency-Key must be one path segment")

func (k *IdempotencyKeys) path(key string) (string, error) {
	if key == "" || strings.Contains(key, "/") || strings.Contains(key, "..") || len(key) > 200 {
		return "", ErrBadKey
	}
	return k.Prefix + key, nil
}

// Claim: (nil, nil) means ours to answer — do the write, then Store. Otherwise the reply to serve.
func (k *IdempotencyKeys) Claim(key string) (*Reply, error) {
	path, err := k.path(key)
	if err != nil {
		return nil, err
	}
	for attempt := 0; attempt < 2; attempt++ {
		_, err := k.Vars.Put(path, Items{"state": "pending", "at": strconv.FormatFloat(k.Wall(), 'f', 3, 64)}, Absent) // create-only: the first claimant wins
		if err == nil {
			k.Prune()
			return nil, nil
		}
		if !errors.Is(err, ErrConflict) {
			return nil, err
		}
		for i := 0; i < 40; i++ { // another instance holds it: its reply, when it lands
			items, _, _ := k.Vars.Get(path)
			if items == nil {
				break // pruned or crashed mid-flight: claim again
			}
			if items["state"] == "done" {
				st, _ := strconv.Atoi(items["status"])
				var body any
				json.Unmarshal([]byte(items["body"]), &body)
				return &Reply{st, body}, nil
			}
			k.Sleep(50 * time.Millisecond)
		}
		if items, _, _ := k.Vars.Get(path); items != nil {
			return &Reply{409, map[string]any{"detail": "the same request is in flight on another console", "error": "in flight"}}, nil
		}
	}
	return &Reply{409, map[string]any{"detail": "the same request is in flight on another console", "error": "in flight"}}, nil
}

func (k *IdempotencyKeys) Store(key string, r Reply) {
	path, err := k.path(key)
	if err != nil {
		return
	}
	raw, _ := json.Marshal(r.Body)
	k.Vars.Put(path, Items{"state": "done", "status": strconv.Itoa(r.Status), "body": string(raw), "at": strconv.FormatFloat(k.Wall(), 'f', 3, 64)}, NoCAS)
}

// Prune deletes keys older than TTL; at most once a minute. Returns how many went.
func (k *IdempotencyKeys) Prune() int {
	k.mu.Lock()
	if time.Since(k.pruned) < time.Minute {
		k.mu.Unlock()
		return 0
	}
	k.pruned = time.Now()
	k.mu.Unlock()
	n, now := 0, k.Wall()
	paths, _ := k.Vars.List(k.Prefix)
	for _, p := range paths {
		items, idx, _ := k.Vars.Get(p)
		if items == nil {
			continue
		}
		at, _ := strconv.ParseFloat(items["at"], 64)
		if now-at > k.TTL {
			if k.Vars.Delete(p, idx) == nil {
				n++
			}
		}
	}
	return n
}

// ForcePrune runs Prune regardless of the once-a-minute guard (tests, an operator's request).
func (k *IdempotencyKeys) ForcePrune() int {
	k.mu.Lock()
	k.pruned = time.Time{}
	k.mu.Unlock()
	return k.Prune()
}

// WorkerLoad = 1 − headroom/capacity: what a target-value policy scales on.
func WorkerLoad(hb Heartbeat, capacityFrom, headroomFrom string) float64 {
	cap := hb.ExtraInt(capacityFrom, 1)
	if cap < 1 {
		cap = 1
	}
	return 1 - float64(hb.ExtraInt(headroomFrom, 0))/float64(cap)
}

// ConsoleOptions: the pieces a console may have beside its controller.
type ConsoleOptions struct {
	MarksRoot     string       // this server's resource, for the console's own event log; "" = no marks
	Index         EventQuerier // the box's or the cluster's MergedIndex; nil = 503
	WorstFailover float64
	Wall          Clock
	Extra         Extra // the subsystem's own routes; nil = none
	Media         bool  // the page may draw a timeline and play (Extra serves /timeline and /segment)
	LostAfter     float64
}

// SpecConsole: one console for every subsystem. Ctl is the subsystem's
// SpecController holding the console's token.
type SpecConsole struct {
	Ctl      *SpecController
	Spec     *SubsystemSpec
	O        ConsoleOptions
	Instance string
	Marks    *EventLog
	Seen     *IdempotencyKeys // in the store: any instance answers a retry

	mu     sync.Mutex
	scanAt time.Time
	scan   map[string][]string
	Scans  int
}

func NewSpecConsole(ctl *SpecController, o ConsoleOptions) *SpecConsole {
	if o.Wall == nil {
		o.Wall = ctl.Wall
	}
	if o.LostAfter == 0 {
		o.LostAfter = 45
	}
	host, _ := os.Hostname()
	c := &SpecConsole{Ctl: ctl, Spec: ctl.Spec, O: o, Instance: fmt.Sprintf("%s:%d", host, os.Getpid()), Seen: NewIdempotencyKeys(ctl.Vars, ctl.Spec.Name+"/idem/", o.Wall)}
	if o.MarksRoot != "" {
		c.Marks = NewEventLog(o.MarksRoot, "console", c.Instance, 1, 600) // the console's own log: one writer, so epoch 1
	}
	return c
}

// Describe is what the page reads first: the YAML, not code.
func (c *SpecConsole) Describe() map[string]any {
	fields := []map[string]any{}
	for _, name := range c.Spec.FieldOrder {
		f := c.Spec.Fields[name]
		fields = append(fields, map[string]any{"name": f.Name, "type": f.Type, "default": f.DefaultValue(), "required": f.Required})
	}
	return map[string]any{"name": c.Spec.Name, "rows": c.Spec.Rows, "id": c.Spec.ID, "media": c.O.Media, "fields": fields,
		"metrics": map[string]any{"prefix": c.Spec.Name, "running": c.Spec.RunningGauge}}
}

// Directory: where is unit N, in one scan of the assignments, cached five seconds.
func (c *SpecConsole) Directory() map[string][]string {
	c.mu.Lock()
	defer c.mu.Unlock()
	if time.Since(c.scanAt) >= 5*time.Second {
		c.scan = map[string][]string{}
		for w, a := range c.Ctl.Assignments() {
			c.scan[w] = a.Units
		}
		c.scanAt = time.Now()
		c.Scans++
	}
	return c.scan
}

func (c *SpecConsole) WhereScanned(uid string) string {
	var hits []string
	for w, units := range c.Directory() {
		for _, u := range units {
			if u == uid {
				hits = append(hits, w)
			}
		}
	}
	sort.Strings(hits)
	return strings.Join(hits, "+") // a reassignment window shows as both
}

// Servers: every server this subsystem runs on, as placement sees it — the
// archive its workers say they record into (on a cluster Nomad's meta.archive,
// the label the scheduler placed by), its resource's state (the fact), its
// workers with load and capacity, and whether the controller would place
// there now, with the reason when it would not.
// DrainState: what ONE subsystem can say about a machine that is about to stop — how many of ITS units
// are still assigned there, whether anything it holds would be stranded by the stop, and, for a subsystem
// whose workers keep something unwritten, how deep that is. The Mount composes these into one answer,
// because an upgrade is a question about a server and a server carries several subsystems.
//
// `safe` is the whole point of the route: an upgrade script polls a CONDITION instead of sleeping and
// hoping. Nothing here decides anything; it reports.
func (c *SpecConsole) DrainState(server string) map[string]any {
	now := c.O.Wall()
	if server == "" {
		server = c.Ctl.Draining()
	}
	if server == "" {
		return map[string]any{"draining": "", "subsystem": c.Spec.Name}
	}
	hbs := Heartbeats(c.Ctl.Objects, c.Spec.Name+"/")
	here := []string{}
	units, pending := 0, 0
	for w, hb := range hbs {
		if c.Ctl.ServerOf(w) != server {
			continue
		}
		here = append(here, w)
		units += len(c.Ctl.Assignment(w).Units)
		if now-hb.Ts <= c.O.LostAfter {
			pending += hb.ExtraInt("spool", 0)
		}
	}
	sort.Strings(here)
	return map[string]any{"draining": server, "subsystem": c.Spec.Name, "workers": here,
		"units": units, "spool": pending, "would_strand": c.Ctl.WouldStrand(server, nil),
		"safe": units == 0 && pending == 0}
}

func (c *SpecConsole) Servers() map[string]any {
	type srv struct {
		archive string
		workers []map[string]any
	}
	now := c.O.Wall()
	seen := map[string]*srv{}
	for w, hb := range Heartbeats(c.Ctl.Objects, c.Spec.Name+"/") {
		name := hb.ExtraString("server", "?")
		s, ok := seen[name]
		if !ok {
			s = &srv{}
			seen[name] = s
		}
		if a := hb.ExtraString("archive", ""); a != "" {
			s.archive = a
		}
		state := "live"
		if now-hb.Ts > c.O.LostAfter {
			state = "stale"
		}
		s.workers = append(s.workers, map[string]any{"worker": w, "load": c.Ctl.Load(w), "capacity": c.Ctl.CapacityOf(w),
			"labels": hb.ExtraString("labels", ""), "state": state, "idle_by_policy": false})
	}
	var names []string
	for w := range Heartbeats(c.Ctl.Objects, c.Spec.Name+"/") {
		names = append(names, w)
	}
	for _, w := range c.Ctl.IdleByPolicy(names) { // servers: distinct — one worker per server carries units
		for _, s := range seen {
			for _, row := range s.workers {
				if row["worker"] == w {
					row["idle_by_policy"] = true
				}
			}
		}
	}
	for name := range ResourcesSeen(c.Ctl.Objects) {
		if _, ok := seen[name]; !ok {
			seen[name] = &srv{}
		}
	}
	out := map[string]any{}
	for name, s := range seen {
		sort.Slice(s.workers, func(i, j int) bool { return s.workers[i]["worker"].(string) < s.workers[j]["worker"].(string) })
		res := c.Ctl.ResourceState(name, c.O.LostAfter)
		req := c.Spec.Requires == "resource"
		drains := name == c.Ctl.Draining()
		placeable := !(req && res == "silent") && !drains
		var why any
		if drains {
			why = "server " + name + " draining"
		} else if !placeable {
			why = "resource on " + name + " silent"
		}
		var archive any
		if s.archive != "" {
			archive = s.archive
		}
		if s.workers == nil {
			s.workers = []map[string]any{}
		}
		out[name] = map[string]any{"archive": archive, "resource": res, "workers": s.workers, "requires_resource": req,
			"draining": drains, "placeable": placeable, "why": why}
	}
	return map[string]any{"policy": c.Ctl.Policy(), "servers": out}
}

func (c *SpecConsole) MetricsText() string {
	p, s := c.Spec.Name, c.Spec
	hbs := Heartbeats(c.Ctl.Objects, p+"/")
	now := c.O.Wall()
	var all, live []string
	for w, hb := range hbs {
		all = append(all, w)
		if now-hb.Ts <= c.O.LostAfter {
			live = append(live, w)
		}
	}
	sort.Strings(all)
	sort.Strings(live)
	var b strings.Builder
	fmt.Fprintf(&b, "# TYPE %s_workers_live gauge\n%s_workers_live %d\n# TYPE %s_worker_headroom gauge\n", p, p, len(live), p)
	total, running, resLive := 0, 0, 0
	for _, w := range live {
		fmt.Fprintf(&b, "%s_worker_headroom{worker=\"%s\",server=\"%s\"} %d\n", p, w, hbs[w].ExtraString("server", "?"), hbs[w].ExtraInt(s.HeadroomFrom, 0))
		total += hbs[w].ExtraInt(s.HeadroomFrom, 0)
	}
	fmt.Fprintf(&b, "%s_headroom %d\n# TYPE %s_worker_load gauge\n", p, total, p) // assigned / capacity: what a target-value policy scales on
	for _, w := range live {
		fmt.Fprintf(&b, "%s_worker_load{worker=\"%s\"} %.3f\n", p, w, WorkerLoad(hbs[w], s.CapacityFrom, s.HeadroomFrom))
	}
	fmt.Fprintf(&b, "# TYPE %s_epoch_conflicts counter\n", p)
	for _, w := range all {
		fmt.Fprintf(&b, "%s_epoch_conflicts{worker=\"%s\"} %d\n", p, w, hbs[w].ExtraInt("conflicts", 0))
	}
	fmt.Fprintf(&b, "# TYPE %s_failover_seconds gauge\n%s_failover_seconds{kind=\"worst\"} %s\n", p, p, strconv.FormatFloat(c.O.WorstFailover, 'f', 1, 64))
	for _, hb := range ResourcesSeen(c.Ctl.Objects) {
		if now-hb.Ts <= c.O.LostAfter {
			resLive++
		}
	}
	for _, w := range live {
		for _, st := range hbs[w].Status {
			if st["phase"] == "running" {
				running++
			}
		}
	}
	fmt.Fprintf(&b, "# TYPE %s_resources_live gauge\n%s_resources_live %d\n# TYPE %s_%s gauge\n%s_%s %d\n", p, p, resLive, p, s.RunningGauge, p, s.RunningGauge, running)
	return b.String()
}

func refusal(err error) (Reply, bool) {
	var refused *Refused
	switch {
	case errors.As(err, &refused):
		return Reply{400, map[string]any{"detail": refused.Msg, "error": refused.Msg}}, true
	case errors.Is(err, ErrNoSuchUnit):
		return Reply{404, map[string]any{"detail": "no such unit", "error": "no such unit"}}, true
	case err != nil:
		return Reply{500, map[string]any{"detail": err.Error(), "error": err.Error()}}, true
	}
	return Reply{}, false
}

// Create writes the row and nothing else: placement is the controller's next
// pass, never the console's — its token could not do it anyway.
func (c *SpecConsole) Create(body map[string]any) Reply {
	r, err := c.Ctl.Create(body)
	if rep, bad := refusal(err); bad {
		return rep
	}
	out := map[string]any{}
	for k, v := range r {
		out[k] = v
	}
	out["worker"] = nil
	return Reply{201, out}
}

func (c *SpecConsole) Update(uid string, body map[string]any) Reply {
	r, err := c.Ctl.Update(uid, body)
	if rep, bad := refusal(err); bad {
		return rep
	}
	return Reply{200, r}
}

func (c *SpecConsole) Delete(uid string) Reply {
	if c.Ctl.Unit(uid) == nil {
		return Reply{404, map[string]any{"detail": "no such unit", "error": "no such unit"}}
	}
	if err := c.Ctl.Delete(uid); err != nil {
		return Reply{500, map[string]any{"detail": err.Error()}}
	}
	return Reply{200, map[string]any{"deleted": c.Spec.parseID(uid)}}
}

func (c *SpecConsole) Mark(body map[string]any, user string) Reply {
	if c.Marks == nil {
		msg := "no resource on this server to write marks into"
		return Reply{503, map[string]any{"detail": msg, "error": msg}}
	}
	fields := map[string]any{"user": user, "note": Str(body["note"])}
	if cam, ok := body["cam"]; ok {
		fields["cam"] = int(ToFloat(cam)) // the field the index joins on
	} else if unit, ok := body["unit"]; ok {
		fields["unit"] = Str(unit)
	} else {
		return Reply{400, map[string]any{"detail": "a mark names a unit", "error": "a mark names a unit"}}
	}
	path, err := c.Marks.Append(c.O.Wall(), "mark", fields)
	if err != nil {
		return Reply{500, map[string]any{"detail": err.Error()}}
	}
	rel, _ := filepath.Rel(c.O.MarksRoot, path)
	return Reply{201, map[string]any{"subsystem": "console", "unit": c.Instance, "bucket": filepath.ToSlash(rel)}}
}

func (c *SpecConsole) events(req *http.Request) Reply {
	if c.O.Index == nil {
		return Reply{503, map[string]any{"error": "no event database behind this console"}}
	}
	q := req.URL.Query()
	cur := map[[2]string]int{} // every subsystem's epochs: the timeline shows them all
	paths, _ := c.Ctl.Vars.List("")
	for _, pth := range paths {
		if !strings.Contains(pth, "/epoch/") {
			continue
		}
		e, _ := CurrentEpoch(c.Ctl.Vars, pth)
		cur[[2]string{pth[:strings.Index(pth, "/")], LastSegment(pth)}] = e
	}
	qq := Query{CurrentEpochs: cur}
	qq.T0, qq.T1 = QueryRange(req)
	cam := q.Get("cam")
	if cam == "" && q.Get("unit") != "" {
		if _, err := strconv.Atoi(q.Get("unit")); err == nil {
			cam = q.Get("unit")
		}
	}
	if cam != "" {
		n, _ := strconv.Atoi(cam)
		qq.Cam = &n
	} else if v := q.Get("unit"); v != "" {
		qq.Unit = StrPtr(v)
	}
	if v := q.Get("kind"); v != "" {
		qq.Kind = StrPtr(v)
	}
	if v := q.Get("subsystem"); v != "" {
		qq.Subsystem = StrPtr(v)
	}
	r := c.O.Index.Query(qq)
	evs := []map[string]any{}
	for _, e := range r.Events {
		evs = append(evs, e.ToMap())
	}
	return Reply{200, map[string]any{"events": evs, "state": r.State}}
}

func (c *SpecConsole) Handler() http.Handler {
	rowsPath := "/" + c.Spec.Rows
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, req *http.Request) {
		path := req.URL.Path
		notFound := func() {
			if c.O.Extra != nil && c.O.Extra(w, req) {
				return
			}
			SendJSON(w, 404, map[string]any{"detail": "no such route", "error": "no such path"})
		}
		switch req.Method {
		case "GET":
			switch {
			case path == "/" || path == "/index.html":
				SendPage(w)
			case path == "/spec":
				SendJSON(w, 200, c.Describe())
			case path == rowsPath:
				SendJSON(w, 200, map[string]any{"rows": c.Ctl.ReadModel(c.O.LostAfter), "configured": c.Ctl.Units()})
			case strings.HasPrefix(path, "/where/"):
				uid := LastSegment(path)
				pl := c.Ctl.Placement(uid)
				body := map[string]any{"worker": nil, "reason": nil, "directory": nil, "scans": 0}
				if d := c.WhereScanned(uid); d != "" {
					body["directory"] = d
				}
				body["scans"] = c.Scans
				st := 404
				if pl != nil {
					body["worker"], body["reason"], st = pl.Worker, pl.Reason, 200
				}
				SendJSON(w, st, body)
			case path == "/servers":
				SendJSON(w, 200, c.Servers())
			case path == "/policy":
				body := map[string]any{"choices": PolicyChoices}
				for k, v := range c.Ctl.Policy() {
					body[k] = v
				}
				SendJSON(w, 200, body)
			case path == "/resources":
				now := c.O.Wall()
				out := map[string]any{}
				for s, hb := range ResourcesSeen(c.Ctl.Objects) {
					raw, _ := json.Marshal(hb)
					var m map[string]any
					json.Unmarshal(raw, &m)
					m["state"] = "silent"
					if now-hb.Ts <= c.O.LostAfter {
						m["state"] = "live"
					}
					out[s] = m
				}
				SendJSON(w, 200, out)
			case path == "/unplaceable":
				SendJSON(w, 200, c.Ctl.Unplaceable())
			case path == "/events":
				r := c.events(req)
				SendJSON(w, r.Status, r.Body)
			case path == "/metrics":
				SendText(w, 200, c.MetricsText())
			default:
				notFound()
			}
		case "POST":
			if path != rowsPath && path != "/marks" {
				notFound()
				return
			}
			key := req.Header.Get("Idempotency-Key")
			if key == "" {
				SendJSON(w, 400, map[string]any{"detail": "Idempotency-Key header is required", "error": "Idempotency-Key required"})
				return
			}
			if prior, err := c.Seen.Claim(key); err != nil {
				SendJSON(w, 400, map[string]any{"detail": err.Error(), "error": err.Error()})
				return
			} else if prior != nil {
				SendJSON(w, prior.Status, prior.Body)
				return
			}
			var r Reply
			if path == "/marks" {
				user := req.Header.Get("X-User")
				if user == "" {
					user = "operator"
				}
				r = c.Mark(ReadBody(req), user)
			} else {
				r = c.Create(ReadBody(req))
			}
			c.Seen.Store(key, r)
			SendJSON(w, r.Status, r.Body)
		case "PUT":
			if path == "/policy" { // the administrator's knobs: one row; a PUT is idempotent by itself
				changes := map[string]string{}
				for k, v := range ReadBody(req) {
					changes[k] = fmt.Sprint(v)
				}
				p, err := c.Ctl.SetPolicy(changes)
				if err != nil {
					st := 400
					if errors.Is(err, ErrForbidden) {
						st = 403
					}
					SendJSON(w, st, map[string]any{"detail": err.Error(), "error": err.Error()})
					return
				}
				SendJSON(w, 200, p)
				return
			}
			if !strings.HasPrefix(path, rowsPath+"/") {
				notFound()
				return
			}
			key := req.Header.Get("Idempotency-Key")
			if key != "" {
				if prior, err := c.Seen.Claim(key); err != nil {
					SendJSON(w, 400, map[string]any{"detail": err.Error(), "error": err.Error()})
					return
				} else if prior != nil {
					SendJSON(w, prior.Status, prior.Body)
					return
				}
			}
			r := c.Update(LastSegment(path), ReadBody(req))
			if key != "" {
				c.Seen.Store(key, r)
			}
			SendJSON(w, r.Status, r.Body)
		case "DELETE":
			if !strings.HasPrefix(path, rowsPath+"/") {
				notFound()
				return
			}
			r := c.Delete(LastSegment(path))
			SendJSON(w, r.Status, r.Body)
		default:
			SendJSON(w, 405, map[string]any{"detail": "method", "error": "method"})
		}
	})
	return mux
}

// ServeConsole on addr ("127.0.0.1:0" picks a port); the listener says which.
func (c *SpecConsole) Serve(addr string) (*http.Server, net.Listener, error) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, nil, err
	}
	srv := &http.Server{Handler: c.Handler()}
	go srv.Serve(ln)
	return srv, ln, nil
}

// Mount: one console process, several subsystems. The root console answers
// at `/` (the page, `/<rows>`, its extras); every other subsystem is a path:
// `/rec/spec`, `/rec/recordings`, `/rec/metrics` — the same class over that
// subsystem's spec with the console's token. `/mounts` names what the
// process fronts. A new subsystem is a YAML, a worker, and a path.
//
// Two routes live on the Mount itself and not on any subsystem, because both are questions about the
// whole process and not about one kind of unit: `/drain` — one machine is about to stop, is it safe yet —
// and `/schema` — the store's layout, and whether every live process is new enough to raise it.
type Mount struct {
	Root   *SpecConsole
	Mounts map[string]*SpecConsole
	names  []string
}

func NewMount(root *SpecConsole) *Mount {
	return &Mount{Root: root, Mounts: map[string]*SpecConsole{}}
}

func (m *Mount) Add(name string, c *SpecConsole) *Mount {
	if _, have := m.Mounts[name]; !have {
		m.names = append(m.names, name)
	}
	m.Mounts[name] = c
	return m
}

func (m *Mount) Describe() map[string]any {
	mounts := map[string]any{}
	for _, n := range m.names {
		mounts[n] = m.Mounts[n].Describe()
	}
	return map[string]any{"root": m.Root.Spec.Name, "mounts": mounts}
}

// SchemaRoute — `GET /schema`: what layout the store is in, what every live process understands, and
// whether the version can be raised; `PUT /schema?version=2`: raise it, once every machine is new.
//
// The two halves of an upgrade read side by side here: `/drain` is about one machine at a time, `/schema`
// is about the moment all of them are done. Raising early is the mistake the guard exists for — it would
// lock out whatever was not upgraded, which is exactly what a rolling upgrade is trying to avoid.
func (m *Mount) SchemaRoute(method string, q map[string]string) (int, map[string]any) {
	ctl := m.Root.Ctl
	switch method {
	case "PUT":
		v, err := strconv.Atoi(q["version"])
		if err != nil {
			return 400, map[string]any{"error": "a version is a number", "detail": "a version is a number"}
		}
		if err := ctl.SetSchema(v); err != nil {
			var tooNew *ErrSchemaTooNew
			if errors.As(err, &tooNew) {
				return 409, map[string]any{"error": err.Error(), "detail": err.Error()}
			}
			return 400, map[string]any{"error": err.Error(), "detail": err.Error()}
		}
	case "GET":
	default:
		return 404, map[string]any{}
	}
	running := Builds(ctl.Objects, ctl.Wall(), 45)
	canRaise, builds := Schema, map[string]bool{}
	procs := map[string]any{}
	for n, b := range running {
		procs[n] = map[string]any{"schema": b.Schema, "build": b.Build, "ts": b.Ts, "live": b.Live}
		if !b.Live {
			continue
		}
		builds[b.Build] = true
		if b.Schema < canRaise {
			canRaise = b.Schema
		}
	}
	names := make([]string, 0, len(builds))
	for b := range builds {
		names = append(names, b)
	}
	sort.Strings(names)
	return 200, map[string]any{"version": SchemaVersion(ctl.Vars), "understood": Schema,
		"builds": names, "can_raise_to": canRaise, "processes": procs}
}

// DrainRoute — `GET /drain`: is it safe to stop the machine yet; `POST /drain?server=srv-a`: say it is
// going to stop; `DELETE /drain`: it is back. The only route on the Mount itself rather than on a
// subsystem, because a server carries several and the answer is `safe` only when every one of them says so.
//
// An upgrade script is then three lines and no `sleep`: POST, poll until `safe`, stop the machine. And
// after the reboot, DELETE — and EnsureHome refills it one unit a pass, which is why the script should
// wait for the work to come back before draining the NEXT machine. Otherwise ten servers' worth of units
// drift onto whichever two were upgraded last.
func (m *Mount) DrainRoute(method string, q map[string]string) (int, map[string]any) {
	ctl := m.Root.Ctl
	switch method {
	case "POST":
		server := q["server"]
		if server == "" {
			return 400, map[string]any{"error": "a drain names a server", "detail": "a drain names a server"}
		}
		if err := ctl.Drain(server); err != nil {
			var refused *ErrDrainRefused
			if errors.As(err, &refused) {
				return 409, map[string]any{"error": err.Error(), "detail": err.Error()}
			}
			return 400, map[string]any{"error": err.Error(), "detail": err.Error()}
		}
	case "DELETE":
		if err := ctl.Undrain(); err != nil {
			return 400, map[string]any{"error": err.Error(), "detail": err.Error()}
		}
	case "GET":
	default:
		return 404, map[string]any{}
	}
	consoles := []*SpecConsole{m.Root}
	for _, n := range m.names {
		consoles = append(consoles, m.Mounts[n])
	}
	subsystems, strand := map[string]any{}, map[string]any{}
	safe := true
	for _, c := range consoles {
		part := c.DrainState("")
		subsystems[Str(part["subsystem"])] = part
		if ok, have := part["safe"].(bool); have && !ok {
			safe = false
		}
		if ws, have := part["would_strand"].([]string); have && len(ws) > 0 {
			strand[Str(part["subsystem"])] = ws
		}
	}
	server := ctl.Draining()
	if server == "" {
		return 200, map[string]any{"draining": "", "safe": true, "subsystems": subsystems}
	}
	return 200, map[string]any{"draining": server, "safe": safe, "would_strand": strand, "subsystems": subsystems}
}

func (m *Mount) Handler() http.Handler {
	root := m.Root.Handler()
	handlers := map[string]http.Handler{}
	for n, c := range m.Mounts {
		handlers[n] = c.Handler()
	}
	return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Path == "/mounts" {
			SendJSON(w, 200, m.Describe())
			return
		}
		if req.URL.Path == "/drain" || req.URL.Path == "/schema" {
			q := map[string]string{}
			for k, v := range req.URL.Query() {
				if len(v) > 0 {
					q[k] = v[0]
				}
			}
			route := m.DrainRoute
			if req.URL.Path == "/schema" {
				route = m.SchemaRoute
			}
			status, body := route(req.Method, q)
			SendJSON(w, status, body)
			return
		}
		parts := strings.SplitN(req.URL.Path, "/", 3)
		if len(parts) >= 2 {
			if h, ok := handlers[parts[1]]; ok {
				rest := "/"
				if len(parts) > 2 {
					rest += parts[2]
				}
				r2 := req.Clone(req.Context())
				r2.URL.Path = rest
				h.ServeHTTP(w, r2)
				return
			}
		}
		root.ServeHTTP(w, req)
	})
}

func (m *Mount) Serve(addr string) (*http.Server, net.Listener, error) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, nil, err
	}
	srv := &http.Server{Handler: m.Handler()}
	go srv.Serve(ln)
	return srv, ln, nil
}
