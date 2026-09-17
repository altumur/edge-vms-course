package vms

// The one-box console, standard library — the platform's SpecConsole run
// from the VMS spec, plus the two routes only a VMS has (the bytes):
//
//	GET  /timeline/<id>?from&to   segments from the archive resource's manifest, fenced ones marked,
//	                              PLUS what the device still has and we do not (Lesson 15)
//	GET  /segment/<path>          the bytes of one promoted segment from this box's archive, Range honoured
//	GET  /segment?cam&from&to     the DEVICE's own footage: the holder's playback door, named not proxied
//	POST /backfill                an operator asking for a range: the recorder fetches it next pass
//
// Everything else — the page, /spec, /cameras, /where, /marks, /metrics, the
// POST/PUT/DELETE of a camera — is w2cplatform.SpecConsole reading
// vms.subsystem.yaml; nothing here knows what a camera's fields are. The
// recorder is mounted under its name — /rec/spec, /rec/recordings (the
// page's Record toggle), /rec/metrics — the same class over rec.subsystem.yaml.
// Its own process (`vms console`), with its own token: the operator's rows —
// cameras, recordings, next_id, retention, the policy knobs — and never placement.

import (
	"encoding/json"
	"net"
	"net/http"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	p "vmsserver/w2cplatform"
)

// DevicePlayback: the door of whoever holds this camera right now, or "". No
// phase is asked for — a channel held only for its archive answers too.
func DevicePlayback(objects p.ObjectStore, cam int, now float64) string {
	h, ok := p.HolderOf(objects, "vms/", strconv.Itoa(cam), now, p.HolderQuery{Field: "playback_url"})
	if !ok {
		return ""
	}
	return p.Str(h.Status["playback_url"])
}

// DeviceSpans: what the DEVICE has and we do not — drawn only where our own
// footage does not cover it. The same subtraction the recorder fetches by
// (Lesson 16): one rule, two uses, so the picture and the work cannot disagree.
// A span like this is the one that will disappear — our archive keeps thirty
// days, a card keeps three — which is why the page offers to pin it.
func DeviceSpans(objects p.ObjectStore, cam int, ours []map[string]any, t0, t1, now float64) []map[string]any {
	h, ok := p.HolderOf(objects, "vms/", strconv.Itoa(cam), now, p.HolderQuery{Field: "coverage"})
	if !ok {
		return []map[string]any{}
	}
	cov, isMap := h.Status["coverage"].(map[string]any)
	if !isMap {
		return []map[string]any{}
	}
	from, to := p.ToFloat(cov["from"]), p.ToFloat(cov["to"])
	if from < t0 {
		from = t0
	}
	if to > t1 {
		to = t1
	}
	if to <= from {
		return []map[string]any{}
	}
	have := make([][2]float64, 0, len(ours))
	for _, s := range ours {
		have = append(have, [2]float64{p.ToFloat(s["start"]), p.ToFloat(s["end"])})
	}
	out := []map[string]any{}
	for _, g := range Subtract([2]float64{from, to}, have) {
		out = append(out, map[string]any{"start": g[0], "end": g[1], "media": nil, "epoch": 0,
			"source": "device", "fenced": false, "device": true})
	}
	return out
}

// VmsRoutes: what the VMS adds to the generic console — the media, from the
// archive we wrote and from the one we did not. Returns false when a route is
// not ours, so the console answers 404.
func VmsRoutes(archive *ArchiveResource, ctl *VmsController, wall p.Clock) p.Extra {
	if wall == nil {
		wall = func() float64 { return float64(time.Now().UnixNano()) / 1e9 }
	}
	return func(w http.ResponseWriter, req *http.Request) bool {
		path := req.URL.Path
		if req.Method == "POST" && path == "/backfill" && archive != nil {
			var body map[string]any
			json.NewDecoder(req.Body).Decode(&body)
			p.SendJSON(w, 202, map[string]any{
				"queued": map[string]any{"cam": body["cam"], "from": body["from"], "to": body["to"]},
				"detail": "the recorder fetches it on its next pass — outside the budget and the window, " +
					"because a person asked for it"})
			return true
		}
		if req.Method != "GET" || archive == nil {
			return false
		}
		switch {
		case (path == "/segment" || path == "/segment/") && ctl != nil:
			// The device's own footage. The console NAMES the holder's door rather
			// than proxying the bytes: the holder is the only process with the
			// session, and a proxy would put the console on the recording path.
			cam, _ := strconv.Atoi(req.URL.Query().Get("cam"))
			url := DevicePlayback(ctl.Objects, cam, wall())
			if url == "" {
				p.SendJSON(w, 503, map[string]any{"detail": "nobody holds this camera right now", "error": "unheld"})
				return true
			}
			q := req.URL.Query()
			from, to := q.Get("from"), q.Get("to")
			if from == "" {
				from = "0"
			}
			if to == "" {
				to = "1e12"
			}
			p.SendJSON(w, 200, map[string]any{"playback": url + "?from=" + from + "&to=" + to})
			return true
		case strings.HasPrefix(path, "/segment/"):
			rel := strings.TrimPrefix(path, "/segment/")
			if strings.Contains(rel, "..") {
				p.SendJSON(w, 404, map[string]any{"detail": "no such segment", "error": "no such segment"})
				return true
			}
			p.SendFile(w, req, filepath.Join(archive.Root, rel), "video/mp4")
			return true
		case strings.HasPrefix(path, "/timeline/"):
			cid, _ := p.LastSegmentInt(path)
			from, to := p.QueryRange(req)
			spans := []map[string]any{}
			for _, s := range NewManifest(archive.Root, strconv.Itoa(cid)).Timeline(from, to, 0) {
				spans = append(spans, s.ToMap())
			}
			if ctl != nil { // ours first, then the device's in the holes: one answer, sorted
				spans = append(spans, DeviceSpans(ctl.Objects, cid, spans, from, to, wall())...)
			}
			sort.SliceStable(spans, func(i, j int) bool {
				if a, b := p.ToFloat(spans[i]["start"]), p.ToFloat(spans[j]["start"]); a != b {
					return a < b
				}
				return p.ToFloat(spans[i]["epoch"]) < p.ToFloat(spans[j]["epoch"])
			})
			p.SendJSON(w, 200, spans)
			return true
		}
		return false
	}
}

// NewConsole: the VMS at `/`, and the recorder at `/rec/…` when a rec controller is given (the console's
// token over RecSpec). Both answer /events from the same merge over the resources' databases.
func NewConsole(ctl *VmsController, archive *ArchiveResource, wall p.Clock, recCtl *p.SpecController) *p.Mount {
	index := p.NewMergedIndex(ctl.Objects, nil, wall) // no database here: the resource process's, asked over HTTP
	o := p.ConsoleOptions{Wall: wall, Extra: VmsRoutes(archive, ctl, wall), Media: archive != nil, Index: index}
	if archive != nil {
		o.MarksRoot = archive.Root
	}
	m := p.NewMount(p.NewSpecConsole(ctl.SpecController, o))
	if recCtl != nil {
		m.Add("rec", p.NewSpecConsole(recCtl, p.ConsoleOptions{Wall: wall, Index: index}))
	}
	return m
}

func NewHandler(ctl *VmsController, archive *ArchiveResource, wall p.Clock, recCtl *p.SpecController) http.Handler {
	return NewConsole(ctl, archive, wall, recCtl).Handler()
}

// Serve on addr ("127.0.0.1:0" picks a port); the listener says which.
func Serve(ctl *VmsController, archive *ArchiveResource, addr string, wall p.Clock, recCtl *p.SpecController) (*http.Server, net.Listener, error) {
	return NewConsole(ctl, archive, wall, recCtl).Serve(addr)
}
