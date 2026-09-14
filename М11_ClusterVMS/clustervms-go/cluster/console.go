package cluster

// The cluster console, standard library — its own job (count ≥ 2, anywhere),
// its own token (the operator's rows, never placement). The platform's
// SpecConsole run from the VMS spec, exactly as М10 runs it; what a CLUSTER
// adds is where the bytes are — two routes, registered, not subclassed:
//
//	GET /timeline/<id>               merged across the resources that hold the camera; unreachable ones named
//	GET /segment/<path>?server=<s>   the bytes of one segment, fetched from THAT server's resource job (Range passed through)
//
// The rest — the page, /spec, /cameras, /where (one scan of the assignments),
// /resources, /unplaceable, /events, /metrics, /marks, POST/PUT/DELETE — is
// vmsplatform.SpecConsole reading vms.subsystem.yaml. A console for the
// counter subsystem is the same type with a different YAML and no extra.

import (
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"

	p "vmsserver/vmsplatform"
)

// ConsoleOptions: the pieces a cluster console may have beside its controller.
type ConsoleOptions struct {
	Reader        ManifestReader
	WorstFailover float64
	Index         *p.EventIndex
	ArchiveRoot   string
}

// ClusterRoutes: the cluster's media routes — the timeline is merged, the segment is proxied.
func ClusterRoutes(ctl *ClusterController, reader ManifestReader) p.Extra {
	if reader == nil {
		reader = NewHTTPManifestReader()
	}
	return func(w http.ResponseWriter, req *http.Request) bool {
		path, q := req.URL.Path, req.URL.Query()
		if req.Method != "GET" {
			return false
		}
		switch {
		case strings.HasPrefix(path, "/timeline/"):
			cid, _ := p.LastSegmentInt(path)
			cur, _ := p.CurrentEpoch(ctl.Vars, ctl.Sub.EpochKey(strconv.Itoa(cid)))
			from, to := p.QueryRange(req)
			p.SendJSON(w, 200, MergedTimeline(p.ResourcesSeen(ctl.Objects), reader, cid, from, to, cur, ctl.Wall(), 45).ToMap())
			return true
		case strings.HasPrefix(path, "/segment/"):
			rel := strings.TrimPrefix(path, "/segment/")
			res, ok := p.ResourcesSeen(ctl.Objects)[q.Get("server")]
			if strings.Contains(rel, "..") || !ok {
				p.SendJSON(w, 404, map[string]any{"error": "no such resource", "detail": "no such resource"})
				return true
			}
			up, _ := http.NewRequest("GET", res.URL+"/segment/"+rel, nil)
			if rng := req.Header.Get("Range"); rng != "" {
				up.Header.Set("Range", rng)
			}
			resp, err := (&http.Client{Timeout: 10 * time.Second}).Do(up)
			if err != nil {
				p.SendJSON(w, 503, map[string]any{"error": "the resource on " + q.Get("server") + " is not answering — unavailable, not lost"})
				return true
			}
			defer resp.Body.Close()
			if resp.StatusCode >= 400 {
				p.SendJSON(w, resp.StatusCode, map[string]any{"error": fmt.Sprintf("the resource on %s said %d", q.Get("server"), resp.StatusCode)})
				return true
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
			return true
		}
		return false
	}
}

func NewConsole(ctl *ClusterController, o ConsoleOptions) *p.SpecConsole {
	return p.NewSpecConsole(ctl.SpecController, p.ConsoleOptions{MarksRoot: o.ArchiveRoot, Index: o.Index, WorstFailover: o.WorstFailover,
		Extra: ClusterRoutes(ctl, o.Reader), Media: true})
}

func NewHandler(ctl *ClusterController, o ConsoleOptions) http.Handler {
	return NewConsole(ctl, o).Handler()
}

func MetricsText(ctl *ClusterController, worstFailover float64) string {
	return p.NewSpecConsole(ctl.SpecController, p.ConsoleOptions{WorstFailover: worstFailover}).MetricsText()
}

func Serve(ctl *ClusterController, addr string, o ConsoleOptions) (*http.Server, net.Listener, error) {
	return NewConsole(ctl, o).Serve(addr)
}
