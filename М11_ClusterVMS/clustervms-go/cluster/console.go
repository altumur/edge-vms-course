package cluster

// The cluster console, standard library. М10's console with three more reads
// and no more writes:
//
//	GET /                  М10's page, unchanged: the camera list, a timeline merged across resources, playback
//	GET /segment/<path>?server=<s>   the bytes of one segment, fetched from THAT server's resource job (Range passed through)
//	GET /cameras           the read model from every worker's heartbeat, with server and age
//	GET /where/<id>        the stored placement (why), and the directory's answer (where, one scan)
//	GET /timeline/<id>     merged across the resources that hold the camera; unreachable ones named
//	GET /resources         which archive resources exist, their usage, which are silent
//	GET /unplaceable       cameras nothing live can reach, with the labels that say why
//	GET /events?from&to&cam&kind&subsystem&unit   from the eventindex — a cache over the resources
//	GET /metrics           vms_workers_live, vms_worker_headroom, vms_worker_load, vms_epoch_conflicts,
//	                       vms_failover_seconds{kind="worst"}, vms_resources_live, vms_cameras_recording
//	POST /cameras, PUT /cameras/<id>     through the controller; Idempotency-Key
//	POST /marks {cam, note}              an operator's observation: the console's own bucket on THIS server's resource

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	"vmsserver/vms"
	p "vmsserver/vmsplatform"
)

func MetricsText(ctl *ClusterController, worstFailover float64) string {
	hbs := Heartbeats(ctl.Objects, "vms/")
	now := ctl.Wall()
	var all, live []string
	for w, hb := range hbs {
		all = append(all, w)
		if now-hb.Ts <= 45 {
			live = append(live, w)
		}
	}
	sort.Strings(all)
	sort.Strings(live)
	res := p.ResourcesSeen(ctl.Objects)
	var b strings.Builder
	fmt.Fprintf(&b, "# TYPE vms_workers_live gauge\nvms_workers_live %d\n# TYPE vms_worker_headroom gauge\n", len(live))
	total, recording, resLive := 0, 0, 0
	for _, w := range live {
		fmt.Fprintf(&b, "vms_worker_headroom{worker=\"%s\",server=\"%s\"} %d\n", w, hbs[w].ExtraString("server", "?"), hbs[w].ExtraInt("headroom", 0))
		total += hbs[w].ExtraInt("headroom", 0)
	}
	fmt.Fprintf(&b, "vms_headroom %d\n# TYPE vms_worker_load gauge\n", total)
	for _, w := range live {
		fmt.Fprintf(&b, "vms_worker_load{worker=\"%s\"} %.3f\n", w, vms.WorkerLoad(hbs[w]))
	}
	b.WriteString("# TYPE vms_epoch_conflicts counter\n")
	for _, w := range all {
		fmt.Fprintf(&b, "vms_epoch_conflicts{worker=\"%s\"} %d\n", w, hbs[w].ExtraInt("conflicts", 0))
	}
	fmt.Fprintf(&b, "# TYPE vms_failover_seconds gauge\nvms_failover_seconds{kind=\"worst\"} %s\n", strconv.FormatFloat(worstFailover, 'f', 1, 64))
	for _, hb := range res {
		if now-hb.Ts <= 45 {
			resLive++
		}
	}
	for _, w := range live {
		for _, s := range hbs[w].Status {
			if s["phase"] == "running" {
				recording++
			}
		}
	}
	fmt.Fprintf(&b, "# TYPE vms_resources_live gauge\nvms_resources_live %d\n# TYPE vms_cameras_recording gauge\nvms_cameras_recording %d\n", resLive, recording)
	return b.String()
}

// ConsoleOptions: the pieces a console may have beside its controller.
type ConsoleOptions struct {
	Reader        ManifestReader
	WorstFailover float64
	Index         *p.EventIndex
	ArchiveRoot   string
}

func NewHandler(ctl *ClusterController, o ConsoleOptions) http.Handler {
	reader := o.Reader
	if reader == nil {
		reader = NewHTTPManifestReader()
	}
	directory := NewDirectory(ctl.Vars, 5, nil)
	seen := vms.NewIdempotent()
	var marks *vms.Marks
	if o.ArchiveRoot != "" {
		marks = vms.NewMarks(o.ArchiveRoot)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, req *http.Request) {
		path := req.URL.Path
		q := req.URL.Query()
		switch req.Method {
		case "GET":
			switch {
			case path == "/" || path == "/index.html":
				vms.SendPage(w)
			case strings.HasPrefix(path, "/segment/"):
				rel := strings.TrimPrefix(path, "/segment/")
				res, ok := p.ResourcesSeen(ctl.Objects)[q.Get("server")]
				if strings.Contains(rel, "..") || !ok {
					vms.SendJSON(w, 404, map[string]any{"error": "no such resource"})
					return
				}
				up, _ := http.NewRequest("GET", res.URL+"/segment/"+rel, nil)
				if rng := req.Header.Get("Range"); rng != "" {
					up.Header.Set("Range", rng)
				}
				resp, err := (&http.Client{Timeout: 10 * time.Second}).Do(up)
				if err != nil {
					vms.SendJSON(w, 503, map[string]any{"error": "the resource on " + q.Get("server") + " is not answering — unavailable, not lost"})
					return
				}
				defer resp.Body.Close()
				if resp.StatusCode >= 400 {
					vms.SendJSON(w, resp.StatusCode, map[string]any{"error": fmt.Sprintf("the resource on %s said %d", q.Get("server"), resp.StatusCode)})
					return
				}
				data, _ := io.ReadAll(resp.Body)
				w.Header().Set("Content-Type", "video/mp4")
				w.Header().Set("Accept-Ranges", "bytes")
				w.Header().Set("Content-Length", strconv.Itoa(len(data)))
				if cr := resp.Header.Get("Content-Range"); cr != "" {
					w.Header().Set("Content-Range", cr)
				}
				w.WriteHeader(resp.StatusCode)
				w.Write(data)
			case path == "/cameras":
				cams := []map[string]any{}
				for _, c := range ctl.Cameras() {
					cams = append(cams, c.ToMap())
				}
				vms.SendJSON(w, 200, map[string]any{"rows": ctl.ReadModel(45), "configured": cams})
			case strings.HasPrefix(path, "/where/"):
				cid, _ := vms.LastSegmentInt(path)
				pl := ctl.Placement(cid)
				body := map[string]any{"worker": nil, "reason": nil, "directory": nil, "scans": 0}
				if d := directory.Where(cid); d != "" {
					body["directory"] = d
				}
				body["scans"] = directory.Scans
				st := 404
				if pl != nil {
					body["worker"], body["reason"], st = pl.Worker, pl.Reason, 200
				}
				vms.SendJSON(w, st, body)
			case strings.HasPrefix(path, "/timeline/"):
				cid, _ := vms.LastSegmentInt(path)
				cur, _ := p.CurrentEpoch(ctl.Vars, ctl.Sub.EpochKey(strconv.Itoa(cid)))
				from, _ := strconv.ParseFloat(q.Get("from"), 64)
				to := 1e12
				if q.Get("to") != "" {
					to, _ = strconv.ParseFloat(q.Get("to"), 64)
				}
				vms.SendJSON(w, 200, MergedTimeline(p.ResourcesSeen(ctl.Objects), reader, cid, from, to, cur, ctl.Wall(), 45).ToMap())
			case path == "/resources":
				now := ctl.Wall()
				out := map[string]any{}
				for s, hb := range p.ResourcesSeen(ctl.Objects) {
					raw, _ := json.Marshal(hb)
					var m map[string]any
					json.Unmarshal(raw, &m)
					m["state"] = "silent"
					if now-hb.Ts <= 45 {
						m["state"] = "live"
					}
					out[s] = m
				}
				vms.SendJSON(w, 200, out)
			case path == "/unplaceable":
				vms.SendJSON(w, 200, ctl.Unplaceable())
			case path == "/events":
				if o.Index == nil {
					vms.SendJSON(w, 503, map[string]any{"error": "no eventindex in this cluster"})
					return
				}
				cur := map[[2]string]int{}
				paths, _ := ctl.Vars.List(ctl.Sub.Name + "/epoch/")
				for _, pth := range paths {
					e, _ := p.CurrentEpoch(ctl.Vars, pth)
					cur[[2]string{"vms", pth[strings.LastIndex(pth, "/")+1:]}] = e
				}
				qq := p.Query{CurrentEpochs: cur, T1: 1e12}
				qq.T0, _ = strconv.ParseFloat(q.Get("from"), 64)
				if q.Get("to") != "" {
					qq.T1, _ = strconv.ParseFloat(q.Get("to"), 64)
				}
				if q.Get("cam") != "" {
					n, _ := strconv.Atoi(q.Get("cam"))
					qq.Cam = &n
				}
				for _, k := range []struct {
					name string
					dst  **string
				}{{"kind", &qq.Kind}, {"subsystem", &qq.Subsystem}, {"unit", &qq.Unit}} {
					if v := q.Get(k.name); v != "" {
						*k.dst = p.StrPtr(v)
					}
				}
				r := o.Index.Query(qq)
				evs := []map[string]any{}
				for _, e := range r.Events {
					evs = append(evs, e.ToMap())
				}
				vms.SendJSON(w, 200, map[string]any{"events": evs, "state": r.State})
			case path == "/metrics":
				vms.SendText(w, 200, MetricsText(ctl, o.WorstFailover))
			default:
				vms.SendJSON(w, 404, map[string]any{"error": "no such path"})
			}
		case "POST":
			if path != "/cameras" && path != "/marks" {
				vms.SendJSON(w, 404, map[string]any{})
				return
			}
			key := req.Header.Get("Idempotency-Key")
			if key == "" {
				vms.SendJSON(w, 400, map[string]any{"error": "Idempotency-Key required"})
				return
			}
			if r, ok := seen.Get(key); ok {
				vms.SendJSON(w, r.Status, r.Body)
				return
			}
			var r vms.Reply
			if path == "/marks" {
				if marks == nil {
					r = vms.Reply{Status: 503, Body: map[string]any{"error": "no resource on this server to write marks into"}}
				} else {
					user := req.Header.Get("X-User")
					if user == "" {
						user = "operator"
					}
					r = marks.Handle(vms.ReadBody(req), user, ctl.Wall())
				}
			} else {
				r = vms.CreateReply(ctl, ctl, vms.ReadBody(req))
			}
			seen.Set(key, r)
			vms.SendJSON(w, r.Status, r.Body)
		case "PUT":
			if !strings.HasPrefix(path, "/cameras/") {
				vms.SendJSON(w, 404, map[string]any{})
				return
			}
			cid, _ := vms.LastSegmentInt(path)
			r := vms.UpdateReply(ctl, cid, vms.ReadBody(req))
			vms.SendJSON(w, r.Status, r.Body)
		default:
			vms.SendJSON(w, 405, map[string]any{"error": "method"})
		}
	})
	return mux
}

func Serve(ctl *ClusterController, addr string, o ConsoleOptions) (*http.Server, net.Listener, error) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, nil, err
	}
	srv := &http.Server{Handler: NewHandler(ctl, o)}
	go srv.Serve(ln)
	return srv, ln, nil
}
