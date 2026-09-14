package psimplatform

// The resource — a platform job, one per server, pinned there for as long
// as the server exists. It knows the shape of what every subsystem leaves
// on a server's disks and nothing about what it means:
//
//	<root>/<subsystem>/<unit>/e<epoch>/...          each subsystem's tree
//	<root>/.mirror/<server>/<subsystem>/<unit>/...  copies of another server's closed buckets (the knob)
//
//	platform/resources/<server>/heartbeat   {server, ts, url, usage, units: {sub: [unit]}, mirrors: {server: n}}
//	platform/mirror                         the knob: {enabled, copies}
//	<sub>/retention, <sub>/retention/<unit> {days}: each subsystem's policy, written by ITS controller
//
//	GET  <url>/buckets/<sub>/<unit>    closed buckets, from the files
//	GET  <url>/events/<path>           one bucket (also .mirror/<server>/<path>)
//	GET  <url>/mirrored/<server>       which of <server>'s buckets this server holds copies of
//	PUT  <url>/mirror/<server>/<path>  another resource leaves a copy of one of ITS closed buckets here

import (
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	MirrorDir = ".mirror"
	MirrorKey = "platform/mirror"
	Resources = "platform/resources"
)

// ResourceHeartbeat is what a resource publishes about itself.
type ResourceHeartbeat struct {
	Server  string              `json:"server"`
	Ts      float64             `json:"ts"`
	URL     string              `json:"url"`
	Usage   int64               `json:"usage"`
	Units   map[string][]string `json:"units"`
	Mirrors map[string]int      `json:"mirrors"`
}

type MirrorSettings struct {
	Enabled bool
	Copies  int
}

func GetMirrorSettings(vars Variables) MirrorSettings {
	items, _, _ := vars.Get(MirrorKey)
	copies := 1
	if items != nil {
		if n, err := strconv.Atoi(items["copies"]); err == nil {
			copies = n
		}
	}
	return MirrorSettings{items != nil && items["enabled"] == "true", copies}
}

// RetentionDays: the unit's days if its subsystem set them, else the
// subsystem's, else a year.
func RetentionDays(vars Variables, subsystem, unit string) float64 {
	for _, p := range []string{subsystem + "/retention/" + unit, subsystem + "/retention"} {
		items, _, _ := vars.Get(p)
		if items != nil {
			if d, ok := items["days"]; ok {
				f, _ := strconv.ParseFloat(d, 64)
				return f
			}
		}
	}
	return 365
}

// PeersOf is the rule that replaces a map: the next `copies` live
// resources after mine, in sorted order.
func PeersOf(server string, live []string, copies int) []string {
	var others []string
	for _, s := range live {
		if s != server {
			others = append(others, s)
		}
	}
	sort.Strings(others)
	if len(others) == 0 {
		return []string{}
	}
	var after []string
	for _, s := range others {
		if s > server {
			after = append(after, s)
		}
	}
	for _, s := range others {
		if s < server {
			after = append(after, s)
		}
	}
	if len(after) > copies {
		after = after[:copies]
	}
	return after
}

func ResourcesSeen(objects ObjectStore) map[string]ResourceHeartbeat {
	out := map[string]ResourceHeartbeat{}
	keys, _ := objects.List(Resources + "/")
	for _, k := range keys {
		if !strings.HasSuffix(k, "/heartbeat") {
			continue
		}
		raw, _ := objects.Get(k)
		var hb ResourceHeartbeat
		if len(raw) > 0 && json.Unmarshal(raw, &hb) == nil {
			out[hb.Server] = hb
		}
	}
	return out
}

func MirroredServers(root string) []string {
	ents, err := os.ReadDir(filepath.Join(root, MirrorDir))
	if err != nil {
		return nil
	}
	var out []string
	for _, e := range ents {
		if e.IsDir() {
			out = append(out, e.Name())
		}
	}
	sort.Strings(out)
	return out
}

// MirroredBuckets: copies this resource holds of server's buckets; Path is
// the ORIGINAL path on server.
func MirroredBuckets(root, server string, bucketSeconds int) []Bucket {
	base := filepath.Join(root, MirrorDir, server)
	var out []Bucket
	filepath.WalkDir(base, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return nil
		}
		if sub, unit, epoch, start, ok := ParseBucket(p, base); ok {
			rel, _ := filepath.Rel(base, p)
			out = append(out, Bucket{sub, unit, epoch, start, start + float64(bucketSeconds), filepath.ToSlash(rel), countLines(p)})
		}
		return nil
	})
	sortBuckets(out)
	if out == nil {
		out = []Bucket{}
	}
	return out
}

// PeerClient is how one resource talks to another; tests substitute an in-process client.
type PeerClient interface {
	Mirrored(url, server string) ([]Bucket, error)
	Put(url, server, path string, data []byte) error
	Get(url, server, path string) ([]byte, error)
}

type HTTPPeerClient struct{ Client *http.Client }

func NewHTTPPeerClient() *HTTPPeerClient {
	return &HTTPPeerClient{&http.Client{Timeout: 5 * time.Second}}
}

func readLines(r io.Reader) []string {
	b, _ := io.ReadAll(r)
	var out []string
	for _, l := range strings.Split(string(b), "\n") {
		if strings.TrimSpace(l) != "" {
			out = append(out, l)
		}
	}
	return out
}

func ParseBucketLines(r io.Reader) ([]Bucket, error) {
	out := []Bucket{}
	for _, l := range readLines(r) {
		b, err := BucketFromLine(l)
		if err != nil {
			return nil, err
		}
		out = append(out, b)
	}
	return out, nil
}

func (c *HTTPPeerClient) Mirrored(url, server string) ([]Bucket, error) {
	resp, err := c.Client.Get(url + "/mirrored/" + server)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return ParseBucketLines(resp.Body)
}

func (c *HTTPPeerClient) Put(url, server, path string, data []byte) error {
	req, _ := http.NewRequest("PUT", url+"/mirror/"+server+"/"+path, strings.NewReader(string(data)))
	resp, err := c.Client.Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	if resp.StatusCode != 200 && resp.StatusCode != 201 && resp.StatusCode != 204 {
		return fmt.Errorf("PUT mirror %s: %d", path, resp.StatusCode)
	}
	return nil
}

func (c *HTTPPeerClient) Get(url, server, path string) ([]byte, error) {
	resp, err := c.Client.Get(url + "/events/" + MirrorDir + "/" + server + "/" + path)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return io.ReadAll(resp.Body)
}

// Hook is a subsystem's own policy on ITS part of the tree.
type Hook interface {
	Pass(now float64) map[string]any
}

// Resource is one server's resource: its tree, its heartbeat, its policy pass.
type Resource struct {
	Root, Server, URL string
	Vars              Variables
	Objects           ObjectStore
	BucketSeconds     int
	Wall              Clock
	Peers             PeerClient
	LostAfter         float64
	Hooks             map[string]Hook
	Database          *EventDatabase // the event database over this tree, if the job runs one: served as GET /events
}

func NewResource(root, server, url string, vars Variables, objects ObjectStore, bucketSeconds int, wall Clock, peers PeerClient) *Resource {
	if bucketSeconds == 0 {
		bucketSeconds = 600
	}
	if wall == nil {
		wall = Wall()
	}
	if peers == nil {
		peers = NewHTTPPeerClient()
	}
	os.MkdirAll(root, 0o755)
	return &Resource{root, server, url, vars, objects, bucketSeconds, wall, peers, 45, map[string]Hook{}, nil}
}

func (r *Resource) Register(subsystem string, h Hook) { r.Hooks[subsystem] = h }

func (r *Resource) Units() map[string][]string { return SubsystemsUnder(r.Root) }

func (r *Resource) ClosedBuckets() []Bucket {
	var out []Bucket
	now := r.Wall()
	for sub, units := range r.Units() {
		for _, unit := range units {
			for _, b := range BucketsUnder(r.Root, sub, unit, r.BucketSeconds) {
				if b.End <= now {
					out = append(out, b)
				}
			}
		}
	}
	return out
}

func (r *Resource) Usage() int64 {
	var total int64
	filepath.WalkDir(r.Root, func(p string, d fs.DirEntry, err error) error {
		if err == nil && !d.IsDir() {
			if info, e := d.Info(); e == nil {
				total += info.Size()
			}
		}
		return nil
	})
	return total
}

func (r *Resource) Heartbeat() (ResourceHeartbeat, error) {
	mirrors := map[string]int{}
	for _, s := range MirroredServers(r.Root) {
		mirrors[s] = len(MirroredBuckets(r.Root, s, r.BucketSeconds))
	}
	hb := ResourceHeartbeat{r.Server, r.Wall(), r.URL, r.Usage(), r.Units(), mirrors}
	raw, _ := json.Marshal(hb)
	return hb, r.Objects.Put(Resources+"/"+r.Server+"/heartbeat", raw)
}

func (r *Resource) LiveResources() map[string]ResourceHeartbeat {
	now := r.Wall()
	out := map[string]ResourceHeartbeat{}
	for s, hb := range ResourcesSeen(r.Objects) {
		if now-hb.Ts <= r.LostAfter {
			out[s] = hb
		}
	}
	return out
}

// Retain: each subsystem's buckets by its own days. Files only.
func (r *Resource) Retain() int {
	var removed []string
	now := r.Wall()
	for sub, units := range r.Units() {
		for _, unit := range units {
			days := RetentionDays(r.Vars, sub, unit)
			for _, b := range BucketsUnder(r.Root, sub, unit, r.BucketSeconds) {
				if b.End < now-days*86400 {
					if os.Remove(filepath.Join(r.Root, b.Path)) == nil {
						removed = append(removed, b.Path)
					}
				}
			}
		}
	}
	if len(removed) > 0 && r.Database != nil {
		r.Database.Forget(r.Server, removed) // the rows go with the file
	}
	return len(removed)
}

// MirrorReport is what one mirror pass did.
type MirrorReport struct {
	Enabled  bool
	Mirrored int
	Peers    []string
}

// Mirror: the knob. Every CLOSED bucket on this server — any subsystem — is
// copied to the next live resource(s) after it, exactly once each.
func (r *Resource) Mirror() MirrorReport {
	knob := GetMirrorSettings(r.Vars)
	if !knob.Enabled {
		return MirrorReport{false, 0, []string{}}
	}
	live := r.LiveResources()
	var names []string
	for s := range live {
		names = append(names, s)
	}
	peers := PeersOf(r.Server, names, knob.Copies)
	n := 0
	for _, peer := range peers {
		have := map[string]bool{}
		bs, err := r.Peers.Mirrored(live[peer].URL, r.Server)
		if err != nil {
			continue
		}
		for _, b := range bs {
			have[b.Path] = true
		}
		for _, b := range r.ClosedBuckets() {
			if have[b.Path] {
				continue
			}
			data, err := os.ReadFile(filepath.Join(r.Root, b.Path))
			if err != nil {
				continue
			}
			if r.Peers.Put(live[peer].URL, r.Server, b.Path, data) == nil {
				n++
			}
		}
	}
	return MirrorReport{true, n, peers}
}

// Restore: the reverse, run by the owner — pull my buckets from whoever
// holds copies, then let each subsystem's hook re-index what came back.
// The result carries "pulled" and each hook's report as "<sub>.<key>".
func (r *Resource) Restore() map[string]any {
	pulled := 0
	for peer, hb := range r.LiveResources() {
		if peer == r.Server {
			continue
		}
		if _, ok := hb.Mirrors[r.Server]; !ok {
			continue
		}
		bs, err := r.Peers.Mirrored(hb.URL, r.Server)
		if err != nil {
			continue
		}
		var paths []string
		for _, b := range bs {
			paths = append(paths, b.Path)
		}
		sort.Strings(paths)
		for _, p := range paths {
			dest := filepath.Join(r.Root, p)
			if _, err := os.Stat(dest); err == nil {
				continue
			}
			data, err := r.Peers.Get(hb.URL, r.Server, p)
			if err != nil {
				continue
			}
			os.MkdirAll(filepath.Dir(dest), 0o755)
			if os.WriteFile(dest+".tmp", data, 0o644) == nil && os.Rename(dest+".tmp", dest) == nil {
				pulled++
			}
		}
	}
	out := map[string]any{"pulled": pulled}
	if pulled > 0 {
		for sub, h := range r.Hooks {
			for k, v := range h.Pass(r.Wall()) {
				out[sub+"."+k] = v
			}
		}
	}
	return out
}

// Pass is the policy pass: each subsystem's own pass first, then retention,
// then the mirror. Keys: "<sub>.<key>", "removed", "enabled", "mirrored", "peers".
func (r *Resource) Pass() map[string]any {
	out := map[string]any{}
	for sub, h := range r.Hooks {
		for k, v := range h.Pass(r.Wall()) {
			out[sub+"."+k] = v
		}
	}
	out["removed"] = r.Retain()
	m := r.Mirror()
	out["enabled"], out["mirrored"], out["peers"] = m.Enabled, m.Mirrored, m.Peers
	return out
}

// Extra lets a subsystem add its own reads to the resource's server (the
// VMS: manifests and footage). It returns handled=false to fall through.
type Extra func(w http.ResponseWriter, req *http.Request) bool

// Serve the resource over HTTP on addr ("127.0.0.1:0" for a free port).
func Serve(r *Resource, addr string, extra Extra) (*http.Server, net.Listener, error) {
	root := r.Root
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, req *http.Request) {
		p := req.URL.Path
		switch {
		case req.Method == "GET" && strings.HasPrefix(p, "/buckets/"):
			parts := strings.SplitN(strings.TrimPrefix(p, "/buckets/"), "/", 2)
			if len(parts) != 2 {
				w.WriteHeader(404)
				return
			}
			for _, b := range BucketsUnder(root, parts[0], parts[1], r.BucketSeconds) {
				io.WriteString(w, b.Line()+"\n")
			}
		case req.Method == "GET" && strings.HasPrefix(p, "/mirrored/"):
			for _, b := range MirroredBuckets(root, strings.TrimPrefix(p, "/mirrored/"), r.BucketSeconds) {
				io.WriteString(w, b.Line()+"\n")
			}
		case req.Method == "GET" && p == "/events":
			if r.Database == nil {
				SendJSON(w, 503, map[string]any{"error": "this resource runs no event database"})
				return
			}
			SendJSON(w, 200, r.Database.Query(QueryFromValues(req.URL.Query())).ToMap())
		case req.Method == "GET" && strings.HasPrefix(p, "/events/"):
			rel := strings.TrimPrefix(p, "/events/")
			if strings.Contains(rel, "..") || !strings.HasSuffix(rel, ".events.jsonl") {
				w.WriteHeader(404)
				return
			}
			b, err := os.ReadFile(filepath.Join(root, rel))
			if err != nil {
				w.WriteHeader(404)
				return
			}
			w.Write(b)
		case req.Method == "PUT" && strings.HasPrefix(p, "/mirror/"):
			rel := strings.TrimPrefix(p, "/mirror/")
			server, path, ok := strings.Cut(rel, "/")
			if !ok || strings.Contains(rel, "..") || server == "" || !strings.HasSuffix(path, ".events.jsonl") {
				w.WriteHeader(400)
				return
			}
			dest := filepath.Join(root, MirrorDir, server, path)
			os.MkdirAll(filepath.Dir(dest), 0o755)
			data, _ := io.ReadAll(req.Body)
			if os.WriteFile(dest+".tmp", data, 0o644) != nil || os.Rename(dest+".tmp", dest) != nil {
				w.WriteHeader(500)
				return
			}
			w.WriteHeader(204) // a copy appears whole or not at all
		default:
			if extra != nil && extra(w, req) {
				return
			}
			w.WriteHeader(404)
		}
	})
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, nil, err
	}
	srv := &http.Server{Handler: mux}
	go srv.Serve(ln)
	return srv, ln, nil
}
