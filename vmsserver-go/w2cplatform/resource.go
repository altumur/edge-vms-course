package w2cplatform

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
	"math"
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
	Schema  int                 `json:"schema"` // what this build understands
	Build   string              `json:"build"`
	Usage   int64               `json:"usage"`    // every FILE under the root, measured once a pass
	UsageAt float64             `json:"usage_at"` // …and when: a stale number must say so
	Space   DiskFree            `json:"space"`    // every volume summed: what a fleet view wants
	Volumes map[string]DiskFree `json:"volumes"`  // …and each disk on its own: what DECIDES anything
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

// Freer is the door a subsystem may add to its hook: the resource says how many bytes, the subsystem
// decides which. A subsystem that keeps only buckets does not implement it — retention by days is its
// whole policy — and is simply not asked.
type Freer interface {
	Free(need int64, now, minDays float64) map[string]any
}

// VolumeFreer is the same door on a box with more than one disk: the resource measured ONE volume, so the
// answer has to come off that volume — bytes freed on another close nothing, because the unit that cannot
// write is on this one. A subsystem whose files are all on one disk implements Freer and is asked the old
// way; nothing had to change on the day the second disk arrived.
type VolumeFreer interface {
	FreeOn(need int64, now, minDays float64, volume string) map[string]any
}

// Volume is one disk of this server, named. Ordered, because "the first" and "the emptiest" are both
// answers a caller gets and a map would give them differently on every run.
type Volume struct {
	Name string
	Path string
}

// Resource is one server's resource: its tree, its heartbeat, its policy pass.
type Resource struct {
	Root, Server, URL string // Root is the FIRST volume's path: what single-volume callers still mean
	Volumes           []Volume
	Vars              Variables
	Objects           ObjectStore
	BucketSeconds     int
	Wall              Clock
	Peers             PeerClient
	LostAfter         float64
	Hooks             map[string]Hook
	Database          *EventDatabase // the event database over this tree, if the job runs one: served as GET /events
	SpaceProbe        func(root string) (int64, int64)
	lastUsage         int64
	haveUsage         bool
	usageAt           float64
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
	mustSchema(vars) // a build older than the store does not run at all
	os.MkdirAll(root, 0o755)
	return &Resource{Root: root, Volumes: []Volume{{Name: "default", Path: root}},
		Server: server, URL: url, Vars: vars, Objects: objects,
		BucketSeconds: bucketSeconds, Wall: wall, Peers: peers, LostAfter: 45,
		Hooks: map[string]Hook{}, SpaceProbe: DiskSpace}
}

// NewResourceOn is the same resource on several disks. It is still ONE resource, because reachability is a
// property of a server and a volume has no address: the volumes are its internal structure. What a volume
// does have is its own bottom, which is why the watermark — the only thing here that ever measured a disk
// — becomes a loop over them.
func NewResourceOn(volumes []Volume, server, url string, vars Variables, objects ObjectStore, bucketSeconds int, wall Clock, peers PeerClient) *Resource {
	if len(volumes) == 0 {
		panic("a resource needs at least one volume")
	}
	r := NewResource(volumes[0].Path, server, url, vars, objects, bucketSeconds, wall, peers)
	r.Volumes = append([]Volume(nil), volumes...)
	for _, v := range r.Volumes {
		os.MkdirAll(v.Path, 0o755)
	}
	return r
}

// SpaceOf is one volume's disk. Space sums them, and the sum is the number that LIES: a box half full
// across two disks with one of them at 98% is a box that has stopped recording.
func (r *Resource) SpaceOf(name string) DiskFree {
	for _, v := range r.Volumes {
		if v.Name == name {
			total, free := r.SpaceProbe(v.Path)
			out := DiskFree{Total: total, Free: free, Used: total - free}
			if total > 0 {
				out.Full = float64(total-free) / float64(total)
			}
			return out
		}
	}
	return DiskFree{}
}

// Spaces is every volume by name: what the console shows and what Relieve walks.
func (r *Resource) Spaces() map[string]DiskFree {
	out := map[string]DiskFree{}
	for _, v := range r.Volumes {
		out[v.Name] = r.SpaceOf(v.Name)
	}
	return out
}

// VolumeOf answers which disk holds a unit — and it is not written down anywhere. The unit's directory IS
// the answer, exactly as SubsystemsUnder already derives what is on this resource at all. A map would be a
// second truth about the disks, and it would drift.
func (r *Resource) VolumeOf(sub, unit string) string {
	for _, v := range r.Volumes {
		if st, err := os.Stat(filepath.Join(v.Path, sub, unit)); err == nil && st.IsDir() {
			return v.Name
		}
	}
	return ""
}

// PlaceVolume is where a unit that is not here yet goes: the emptiest disk. After that VolumeOf answers,
// and a unit does not move between disks — that would be copying terabytes as a side effect of a pass.
func (r *Resource) PlaceVolume() string {
	best, most := r.Volumes[0].Name, int64(-1)
	for _, v := range r.Volumes {
		if _, free := r.SpaceProbe(v.Path); free > most {
			best, most = v.Name, free
		}
	}
	return best
}

// PathOf is the absolute path of something named relative to a volume: the volume that has it, else the
// emptiest.
func (r *Resource) PathOf(rel string) string {
	for _, v := range r.Volumes {
		full := filepath.Join(v.Path, rel)
		if _, err := os.Stat(full); err == nil {
			return full
		}
	}
	for _, v := range r.Volumes {
		if v.Name == r.PlaceVolume() {
			return filepath.Join(v.Path, rel)
		}
	}
	return filepath.Join(r.Root, rel)
}

// Space is the disk, and how full it is. Free is what a peer reads before sending anything here: an
// evacuation onto a disk that is itself tight only moves the problem.
func (r *Resource) Space() DiskFree {
	var total, free int64
	for _, v := range r.Volumes {
		t, f := r.SpaceProbe(v.Path)
		total, free = total+t, free+f
	}
	out := DiskFree{Total: total, Free: free, Used: total - free}
	if total > 0 {
		out.Full = float64(total-free) / float64(total)
	}
	return out
}

// UsageCached is the tree walk's answer, measured now if it never was: the first heartbeat of a process
// pays for it once, and Pass refreshes it after that. Walking a quarter of a million files every ten
// seconds does not merely cost a second — it touches every inode in the tree, so the cache holds the
// archive's metadata instead of the video the machine exists to serve.
func (r *Resource) UsageCached() int64 {
	if !r.haveUsage {
		r.lastUsage, r.usageAt, r.haveUsage = r.Usage(), r.Wall(), true
	}
	return r.lastUsage
}

// Relieve: over the high mark, ask each subsystem to free bytes down to the LOW one — freeing just enough
// to slip under high means being back over it in a minute. Retention by days is a PROMISE to the
// operator; this is what happens when the promise cannot be kept. Nothing here deletes a subsystem's file.
//
// Slowness resolves itself: a hook that can only start something (evacuating footage to the server that
// now writes it, say) returns what it managed and is asked again on the next pass — which is why there is
// no third, "critical" mark and no separate schedule.
func (r *Resource) Relieve() map[string]any {
	knob := GetSpaceSettings(r.Vars)
	if !knob.Enabled {
		return map[string]any{"space": "off"}
	}
	// Per VOLUME, and that is the whole difference from the single-disk case: space does not average. A
	// box that is 50% full across two disks, one of them at 98%, is a box that stops recording — and
	// freeing bytes on the empty one closes nothing, because the unit that cannot write is on the full
	// one. So the loop is over volumes, and the volume goes to the hook: only the subsystem knows which
	// of its files are where, only the resource knows which disk is short.
	out := map[string]any{}
	var over []map[string]any
	worst := 0.0
	for _, v := range r.Volumes {
		sp := r.SpaceOf(v.Name)
		if sp.Full > worst {
			worst = sp.Full
		}
		if sp.Total == 0 || float64(sp.Used) <= float64(sp.Total)*knob.High {
			continue
		}
		need := int64(float64(sp.Used) - float64(sp.Total)*knob.Low)
		var freed int64
		for _, sub := range hookNames(r.Hooks) {
			rep := askToFree(r.Hooks[sub], need-freed, r.Wall(), knob.MinDays, v.Name)
			if rep == nil {
				continue // a subsystem that keeps only buckets: retention by days is its whole policy
			}
			freed += int64(ToFloat(rep["freed"]))
			for k, val := range rep {
				if len(r.Volumes) == 1 {
					out[sub+"."+k] = val
				} else {
					out[sub+"."+v.Name+"."+k] = val
				}
			}
			if freed >= need {
				break
			}
		}
		short := need - freed
		if short < 0 {
			short = 0
		}
		over = append(over, map[string]any{"volume": v.Name, "full": round3(sp.Full),
			"need": need, "freed": freed, "short": short})
	}
	if len(over) == 0 {
		return map[string]any{"space": "ok", "full": round3(worst)}
	}
	first := over[0] // single-volume callers read these at the top level, as they always did
	out["space"], out["full"] = "over", first["full"]
	out["need"], out["freed"], out["short"] = first["need"], first["freed"], first["short"]
	out["volumes"] = over
	return out
}

// askToFree asks the richer door first: a hook that knows about volumes is told WHICH disk is short, and
// one written before there were volumes is asked the way it has always been asked. nil when the hook does
// not free at all.
func askToFree(h Hook, need int64, now, minDays float64, volume string) map[string]any {
	if vf, ok := h.(VolumeFreer); ok {
		return vf.FreeOn(need, now, minDays, volume)
	}
	if f, ok := h.(Freer); ok {
		return f.Free(need, now, minDays)
	}
	return nil
}

func round3(f float64) float64 { return math.Round(f*1000) / 1000 }

func hookNames(m map[string]Hook) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func (r *Resource) Register(subsystem string, h Hook) { r.Hooks[subsystem] = h }

func (r *Resource) Units() map[string][]string {
	out := map[string][]string{}
	for _, v := range r.Volumes {
		for sub, units := range SubsystemsUnder(v.Path) {
			for _, u := range units {
				if !contains(out[sub], u) {
					out[sub] = append(out[sub], u)
				}
			}
		}
	}
	for sub := range out {
		sort.Strings(out[sub])
	}
	return out
}


func (r *Resource) ClosedBuckets() []Bucket {
	var out []Bucket
	now := r.Wall()
	for sub, units := range r.Units() {
		for _, unit := range units {
			for _, v := range r.Volumes {
				for _, b := range BucketsUnder(v.Path, sub, unit, r.BucketSeconds) {
					if b.End <= now {
						out = append(out, b)
					}
				}
			}
		}
	}
	return out
}

func (r *Resource) Usage() int64 {
	var total int64
	for _, v := range r.Volumes {
		filepath.WalkDir(v.Path, func(p string, d fs.DirEntry, err error) error {
			if err == nil && !d.IsDir() {
				if info, e := d.Info(); e == nil {
					total += info.Size()
				}
			}
			return nil
		})
	}
	return total
}

func (r *Resource) Heartbeat() (ResourceHeartbeat, error) {
	mirrors := map[string]int{}
	for _, v := range r.Volumes {
		for _, s := range MirroredServers(v.Path) {
			mirrors[s] += len(MirroredBuckets(v.Path, s, r.BucketSeconds))
		}
	}
	hb := ResourceHeartbeat{Server: r.Server, Ts: r.Wall(), URL: r.URL, Schema: Schema, Build: Build,
		Usage: r.UsageCached(), UsageAt: r.usageAt, Space: r.Space(), Volumes: r.Spaces(),
		Units: r.Units(), Mirrors: mirrors}
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
			for _, v := range r.Volumes {
				for _, b := range BucketsUnder(v.Path, sub, unit, r.BucketSeconds) {
					if b.End < now-days*86400 {
						if os.Remove(filepath.Join(v.Path, b.Path)) == nil {
							removed = append(removed, b.Path)
						}
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
			data, err := os.ReadFile(r.PathOf(b.Path))
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
			dest := r.PathOf(p) // back onto the volume that held it, or the emptiest
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
	r.lastUsage, r.usageAt, r.haveUsage = r.Usage(), r.Wall(), true // the one walk of the pass, not one per heartbeat
	out["usage"] = r.lastUsage
	for k, v := range r.Relieve() { // the promise first, the watermark only for what the promise left behind
		out[k] = v
	}
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
