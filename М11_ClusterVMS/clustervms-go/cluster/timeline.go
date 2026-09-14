package cluster

// One camera's timeline across the resources it recorded into.
//
// A camera that failed over has footage on two servers: the dead one's until
// the failure, the new one's after. The console asks every resource that
// reports the camera for its manifest and merges. A resource whose heartbeat
// is stale is unreachable: the answer says so by server. *Unavailable* is a
// state with a name in it; *lost* is a word this module never prints for
// footage on a disk.

import (
	"fmt"
	"io"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	p "vmsserver/psimplatform"
	"vmsserver/vms"
)

// ManifestReader: how the console gets a manifest from a resource — HTTP, or a fake for tests.
type ManifestReader interface {
	Read(url string, cam int) ([]vms.Segment, error)
}

type HTTPManifestReader struct{ Client *http.Client }

func NewHTTPManifestReader() *HTTPManifestReader {
	return &HTTPManifestReader{&http.Client{Timeout: 3 * time.Second}}
}

func (r *HTTPManifestReader) Read(url string, cam int) ([]vms.Segment, error) {
	resp, err := r.Client.Get(fmt.Sprintf("%s/manifest/%d", url, cam))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out []vms.Segment
	for _, l := range strings.Split(string(raw), "\n") {
		if strings.TrimSpace(l) == "" {
			continue
		}
		if s, err := vms.SegmentFromLine(l); err == nil {
			out = append(out, s)
		}
	}
	return out, nil
}

type TimelineSegment struct {
	Start, End float64
	Path       string
	Epoch      int
	Server     string
	Fenced     bool
}

func (s TimelineSegment) ToMap() map[string]any {
	return map[string]any{"start": s.Start, "end": s.End, "path": s.Path, "epoch": s.Epoch, "server": s.Server, "fenced": s.Fenced}
}

type Timeline struct {
	Segments    []TimelineSegment
	Unreachable []string
	Note        string
}

func (t Timeline) ToMap() map[string]any {
	segs := []map[string]any{}
	for _, s := range t.Segments {
		segs = append(segs, s.ToMap())
	}
	return map[string]any{"segments": segs, "unreachable": t.Unreachable, "note": t.Note}
}

// MergedTimeline over ResourcesSeen(); currentEpoch 0 = unknown.
func MergedTimeline(resources map[string]p.ResourceHeartbeat, reader ManifestReader, cam int, t0, t1 float64, currentEpoch int, now float64, lostAfter float64) Timeline {
	if lostAfter == 0 {
		lostAfter = 45
	}
	out := Timeline{Segments: []TimelineSegment{}, Unreachable: []string{}}
	var servers []string
	for s := range resources {
		servers = append(servers, s)
	}
	sort.Strings(servers)
	unit := strconv.Itoa(cam)
	for _, server := range servers {
		hb := resources[server]
		has := false
		for _, u := range hb.Units["vms"] { // the platform's heartbeat: units per subsystem
			if u == unit {
				has = true
			}
		}
		if !has {
			continue
		}
		if now-hb.Ts > lostAfter {
			out.Unreachable = append(out.Unreachable, server)
			continue
		}
		rows, err := reader.Read(hb.URL, cam)
		if err != nil { // the heartbeat is fresh but the server is not answering
			out.Unreachable = append(out.Unreachable, server)
			continue
		}
		for _, s := range rows {
			if s.End > t0 && s.Start < t1 {
				out.Segments = append(out.Segments, TimelineSegment{s.Start, s.End, s.Path, s.Epoch, server, currentEpoch > 0 && s.Epoch < currentEpoch})
			}
		}
	}
	sort.SliceStable(out.Segments, func(i, j int) bool {
		if out.Segments[i].Start != out.Segments[j].Start {
			return out.Segments[i].Start < out.Segments[j].Start
		}
		return out.Segments[i].Epoch < out.Segments[j].Epoch
	})
	if len(out.Unreachable) > 0 {
		out.Note = fmt.Sprintf("ranges on %s are unavailable until the server returns — not lost", strings.Join(out.Unreachable, ", "))
	}
	return out
}
