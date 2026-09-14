package vms

// The one-box console, standard library. Reads never touch a worker;
// writes go through the controller, the only writer.
//
//	GET  /                        the page: the camera list, a camera's timeline, playback of a span (console.html)
//	GET  /segment/<path>          the bytes of one promoted segment from this box's archive, Range honoured
//	GET  /cameras                 the read model: every camera from the workers' heartbeats, with age
//	GET  /where/<id>              which worker — from the stored placement
//	GET  /timeline/<id>?from&to   segments from the archive resource's manifest, fenced ones marked
//	POST /cameras                 create (Idempotency-Key required)
//	POST /marks                   an operator's observation {cam, note} — the CONSOLE's event, into console/<instance>/…
//	PUT  /cameras/<id>            update — refuses placement and controller-owned fields
//	DELETE /cameras/<id>          the row is marked deleted; its assignment goes; footage stays until retention
//	GET  /metrics                 vms_epoch_conflicts, vms_workers_live, vms_worker_headroom, vms_worker_load, vms_cameras_recording

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

	p "vmsserver/vmsplatform"
)

//go:embed console.html
var Page []byte

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

func SendPage(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Content-Length", strconv.Itoa(len(Page)))
	w.Write(Page)
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

func LastSegmentInt(path string) (int, error) {
	return strconv.Atoi(path[strings.LastIndex(path, "/")+1:])
}

// Marks is the console's own event log: one writer, so epoch 1.
type Marks struct {
	Log      *p.EventLog
	Instance string
	Root     string
}

func NewMarks(archiveRoot string) *Marks {
	host, _ := os.Hostname()
	inst := fmt.Sprintf("%s:%d", host, os.Getpid())
	return &Marks{p.NewEventLog(archiveRoot, "console", inst, 1, 600), inst, archiveRoot}
}

func (m *Marks) Handle(body map[string]any, user string, now float64) Reply {
	cam, ok := body["cam"]
	if !ok {
		return Reply{400, map[string]any{"detail": "a mark names a camera", "error": "a mark names a camera"}}
	}
	note, _ := body["note"].(string)
	path, err := m.Log.Append(now, "mark", map[string]any{"cam": int(p.ToFloat(cam)), "user": user, "note": note})
	if err != nil {
		return Reply{500, map[string]any{"detail": err.Error()}}
	}
	rel, _ := filepath.Rel(m.Root, path)
	return Reply{201, map[string]any{"subsystem": "console", "unit": m.Instance, "bucket": filepath.ToSlash(rel)}}
}

func CreateReply(ctl *VmsController, placer Placer, body map[string]any) Reply {
	r, err := ctl.CreateCamera(body)
	var refused *Refused
	if errors.As(err, &refused) {
		return Reply{400, map[string]any{"detail": refused.Msg, "error": refused.Msg}}
	}
	if err != nil {
		return Reply{500, map[string]any{"detail": err.Error()}}
	}
	pl, _ := placer.Place(r.ID, nil)
	out := r.ToMap()
	if pl != nil {
		out["worker"] = pl.Worker
	} else {
		out["worker"] = nil
	}
	return Reply{201, out}
}

func UpdateReply(ctl *VmsController, cid int, body map[string]any) Reply {
	r, err := ctl.UpdateCamera(cid, body)
	var refused *Refused
	switch {
	case errors.As(err, &refused):
		return Reply{400, map[string]any{"detail": refused.Msg, "error": refused.Msg}}
	case errors.Is(err, ErrNoSuchCamera):
		return Reply{404, map[string]any{"detail": "no such camera", "error": "no such camera"}}
	case err != nil:
		return Reply{500, map[string]any{"detail": err.Error()}}
	}
	return Reply{200, r.ToMap()}
}

func camerasMaps(cams []Camera) []map[string]any {
	out := []map[string]any{}
	for _, c := range cams {
		out = append(out, c.ToMap())
	}
	return out
}

func MetricsOneBox(ctl *VmsController) string {
	hbs := ctl.WorkersSeen(45)
	var ws []string
	for w := range hbs {
		ws = append(ws, w)
	}
	sort.Strings(ws)
	var b strings.Builder
	b.WriteString("# TYPE vms_epoch_conflicts counter\n")
	for _, w := range ws {
		fmt.Fprintf(&b, "vms_epoch_conflicts{worker=\"%s\"} %d\n", w, hbs[w].ExtraInt("conflicts", 0))
	}
	fmt.Fprintf(&b, "# TYPE vms_workers_live gauge\nvms_workers_live %d\n# TYPE vms_worker_headroom gauge\n", len(hbs))
	total, recording := 0, 0
	for _, w := range ws {
		fmt.Fprintf(&b, "vms_worker_headroom{worker=\"%s\"} %d\n", w, hbs[w].ExtraInt("headroom", 0))
		total += hbs[w].ExtraInt("headroom", 0)
	}
	fmt.Fprintf(&b, "vms_headroom %d\n# TYPE vms_worker_load gauge\n", total)
	for _, w := range ws {
		fmt.Fprintf(&b, "vms_worker_load{worker=\"%s\"} %.3f\n", w, WorkerLoad(hbs[w]))
	}
	for _, hb := range hbs {
		for _, s := range hb.Status {
			if s["phase"] == "running" {
				recording++
			}
		}
	}
	fmt.Fprintf(&b, "# TYPE vms_cameras_recording gauge\nvms_cameras_recording %d\n", recording)
	return b.String()
}

// WorkerLoad = 1 − headroom/capacity: what a target-value policy scales on.
func WorkerLoad(hb p.Heartbeat) float64 {
	cap := hb.ExtraInt("capacity", 1)
	if cap < 1 {
		cap = 1
	}
	return 1 - float64(hb.ExtraInt("headroom", 0))/float64(cap)
}

func NewHandler(ctl *VmsController, archive *ArchiveResource, wall p.Clock) http.Handler {
	seen := NewIdempotent()
	var marks *Marks
	if archive != nil {
		marks = NewMarks(archive.Root)
	}
	if wall == nil {
		wall = ctl.Wall
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, req *http.Request) {
		path := req.URL.Path
		q := req.URL.Query()
		switch req.Method {
		case "GET":
			switch {
			case path == "/" || path == "/index.html":
				SendPage(w)
			case strings.HasPrefix(path, "/segment/") && archive != nil:
				rel := strings.TrimPrefix(path, "/segment/")
				if strings.Contains(rel, "..") {
					SendJSON(w, 404, map[string]any{"detail": "no such segment"})
					return
				}
				SendFile(w, req, filepath.Join(archive.Root, rel), "video/mp4")
			case path == "/cameras":
				SendJSON(w, 200, map[string]any{"rows": ctl.ReadModel(45), "configured": camerasMaps(ctl.Cameras())})
			case strings.HasPrefix(path, "/where/"):
				cid, _ := LastSegmentInt(path)
				wk := ctl.Where(cid)
				var v any
				if wk != "" {
					v = wk
				}
				st := 200
				if wk == "" {
					st = 404
				}
				SendJSON(w, st, map[string]any{"worker": v})
			case strings.HasPrefix(path, "/timeline/") && archive != nil:
				cid, _ := LastSegmentInt(path)
				from, _ := strconv.ParseFloat(q.Get("from"), 64)
				to := 1e12
				if q.Get("to") != "" {
					to, _ = strconv.ParseFloat(q.Get("to"), 64)
				}
				spans := []map[string]any{}
				for _, s := range NewManifest(archive.Root, cid).Timeline(from, to, 0) {
					spans = append(spans, s.ToMap())
				}
				SendJSON(w, 200, spans)
			case path == "/metrics":
				SendText(w, 200, MetricsOneBox(ctl))
			default:
				SendJSON(w, 404, map[string]any{"detail": "no such route"})
			}
		case "POST":
			if path != "/cameras" && path != "/marks" {
				SendJSON(w, 404, map[string]any{"detail": "no such route"})
				return
			}
			key := req.Header.Get("Idempotency-Key")
			if key == "" {
				SendJSON(w, 400, map[string]any{"detail": "Idempotency-Key header is required"})
				return
			}
			if r, ok := seen.Get(key); ok {
				SendJSON(w, r.Status, r.Body)
				return
			}
			var r Reply
			if path == "/marks" {
				if marks == nil {
					r = Reply{503, map[string]any{"detail": "no resource on this box to write marks into"}}
				} else {
					user := req.Header.Get("X-User")
					if user == "" {
						user = "operator"
					}
					r = marks.Handle(ReadBody(req), user, wall())
				}
			} else {
				r = CreateReply(ctl, ctl, ReadBody(req))
			}
			seen.Set(key, r)
			SendJSON(w, r.Status, r.Body)
		case "PUT":
			if !strings.HasPrefix(path, "/cameras/") {
				SendJSON(w, 404, map[string]any{"detail": "no such route"})
				return
			}
			key := req.Header.Get("Idempotency-Key")
			if key == "" {
				SendJSON(w, 400, map[string]any{"detail": "Idempotency-Key header is required"})
				return
			}
			if r, ok := seen.Get(key); ok {
				SendJSON(w, r.Status, r.Body)
				return
			}
			cid, _ := LastSegmentInt(path)
			r := UpdateReply(ctl, cid, ReadBody(req))
			seen.Set(key, r)
			SendJSON(w, r.Status, r.Body)
		case "DELETE":
			if !strings.HasPrefix(path, "/cameras/") {
				SendJSON(w, 404, map[string]any{"detail": "no such route"})
				return
			}
			cid, _ := LastSegmentInt(path)
			if ctl.Camera(cid) == nil {
				SendJSON(w, 404, map[string]any{"detail": "no such camera", "error": "no such camera"})
				return
			}
			ctl.DeleteCamera(cid)
			SendJSON(w, 200, map[string]any{"deleted": cid})
		default:
			SendJSON(w, 405, map[string]any{"detail": "method"})
		}
	})
	return mux
}

// Serve on addr ("127.0.0.1:0" picks a port); the listener says which.
func Serve(ctl *VmsController, archive *ArchiveResource, addr string, wall p.Clock) (*http.Server, net.Listener, error) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, nil, err
	}
	srv := &http.Server{Handler: NewHandler(ctl, archive, wall)}
	go srv.Serve(ln)
	return srv, ln, nil
}
