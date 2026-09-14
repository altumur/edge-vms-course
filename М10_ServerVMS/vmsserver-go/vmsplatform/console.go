package vmsplatform

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
//	GET  /events?from&to&unit&kind&subsystem   from the eventindex, if this console runs one
//	GET  /metrics                <name>_workers_live · _worker_headroom{worker,server} · _worker_load · _epoch_conflicts ·
//	                             _failover_seconds{kind="worst"} · _resources_live · <name>_<running> (the spec names the gauge)
//	POST /<rows>  (Idempotency-Key)   the row only — the controller places it on its next pass
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

// Idempotent remembers each key's first reply.
type Idempotent struct {
	mu   sync.Mutex
	seen map[string]Reply
}

func NewIdempotent() *Idempotent { return &Idempotent{seen: map[string]Reply{}} }

func (i *Idempotent) Get(key string) (Reply, bool) {
	i.mu.Lock()
	defer i.mu.Unlock()
	r, ok := i.seen[key]
	return r, ok
}

func (i *Idempotent) Set(key string, r Reply) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.seen[key] = r
}

// Heartbeats: every worker's last heartbeat under prefix, whatever its age — the read model's source.
func Heartbeats(objects ObjectStore, prefix string) map[string]Heartbeat {
	out := map[string]Heartbeat{}
	keys, _ := objects.List(prefix)
	for _, k := range keys {
		if strings.HasSuffix(k, "/heartbeat") {
			raw, _ := objects.Get(k)
			if raw != nil {
				if hb, err := HeartbeatFromBytes(raw); err == nil {
					out[hb.Worker] = hb
				}
			}
		}
	}
	return out
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
	MarksRoot     string // this server's resource, for the console's own event log; "" = no marks
	Index         *EventIndex
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
	Seen     *Idempotent

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
	c := &SpecConsole{Ctl: ctl, Spec: ctl.Spec, O: o, Instance: fmt.Sprintf("%s:%d", host, os.Getpid()), Seen: NewIdempotent()}
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
		return Reply{503, map[string]any{"error": "no eventindex behind this console"}}
	}
	q := req.URL.Query()
	cur := map[[2]string]int{}
	paths, _ := c.Ctl.Vars.List(c.Spec.Name + "/epoch/")
	for _, pth := range paths {
		e, _ := CurrentEpoch(c.Ctl.Vars, pth)
		cur[[2]string{c.Spec.Name, LastSegment(pth)}] = e
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
			if r, ok := c.Seen.Get(key); ok {
				SendJSON(w, r.Status, r.Body)
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
			c.Seen.Set(key, r)
			SendJSON(w, r.Status, r.Body)
		case "PUT":
			if !strings.HasPrefix(path, rowsPath+"/") {
				notFound()
				return
			}
			key := req.Header.Get("Idempotency-Key")
			if key != "" {
				if r, ok := c.Seen.Get(key); ok {
					SendJSON(w, r.Status, r.Body)
					return
				}
			}
			r := c.Update(LastSegment(path), ReadBody(req))
			if key != "" {
				c.Seen.Set(key, r)
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
