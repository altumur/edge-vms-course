package vms

// The one-box console, standard library — the platform's SpecConsole run
// from the VMS spec, plus the two routes only a VMS has (the bytes):
//
//	GET  /timeline/<id>?from&to   segments from the archive resource's manifest, fenced ones marked
//	GET  /segment/<path>          the bytes of one promoted segment from this box's archive, Range honoured
//
// Everything else — the page, /spec, /cameras, /where, /marks, /metrics, the
// POST/PUT/DELETE of a camera — is psimplatform.SpecConsole reading
// vms.subsystem.yaml; nothing here knows what a camera's fields are. The
// recorder is mounted under its name — /rec/spec, /rec/recordings (the
// page's Record toggle), /rec/metrics — the same class over rec.subsystem.yaml.
// Its own process (`vms console`), with its own token: the operator's rows —
// cameras, recordings, next_id, retention, the policy knobs — and never placement.

import (
	"net"
	"net/http"
	"path/filepath"
	"strings"

	p "vmsserver/psimplatform"
)

// VmsRoutes: what the VMS adds to the generic console — the media. Returns
// false when a route is not ours, so the console answers 404.
func VmsRoutes(archive *ArchiveResource) p.Extra {
	return func(w http.ResponseWriter, req *http.Request) bool {
		path := req.URL.Path
		if req.Method != "GET" || archive == nil {
			return false
		}
		switch {
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
			for _, s := range NewManifest(archive.Root, cid).Timeline(from, to, 0) {
				spans = append(spans, s.ToMap())
			}
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
	o := p.ConsoleOptions{Wall: wall, Extra: VmsRoutes(archive), Media: archive != nil, Index: index}
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
