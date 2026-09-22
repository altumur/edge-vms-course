package vms

// The resource process — the platform's resource job (w2cplatform.Resource)
// with the VMS registered on it. One per server, pinned there for as long as
// the server exists; on a box it is `vms resource`, in М11 the `resource`
// system job (clustervms-go re-exports NewVmsResource as ClusterResource).
// It has no controller: it has a policy pass on a timer, a heartbeat, its
// HTTP, and the event database over its own tree. What the VMS adds:
//
//	ArchivePolicy    registered as the "rec" hook: the recorder's — repair the manifests, retain media by rec/recordings/<unit>
//	ResourceRoutes   GET /manifest/<unit>  the manifest's lines;  GET /segment/<path>  the bytes, Range honoured
//
// The platform's part — platform/resources/<server>/heartbeat, GET /events
// from the EventDatabase, /buckets, /mirrored, PUT /mirror — is not the
// VMS's, and the names say so.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	p "vmsserver/w2cplatform"
)

// ResourceRoutes: the VMS's reads on the resource, plugged into the platform's server.
func ResourceRoutes(archive *ArchiveResource) p.Extra {
	return ResourceRoutesWith(archive, nil, "")
}

// ResourceRoutesWith adds the two answers that need to know whose server this is: PUT /segment — a
// segment another resource is giving up — and GET /space, which says what is here, how deep it goes, and
// what of it another server writes now.
func ResourceRoutesWith(archive *ArchiveResource, objects p.ObjectStore, server string) p.Extra {
	root := archive.Root
	return func(w http.ResponseWriter, req *http.Request) bool {
		pth := req.URL.Path
		if req.Method == "PUT" && strings.HasPrefix(pth, "/segment/") {
			return putSegment(w, req, root)
		}
		switch {
		case pth == "/space":
			// What the archive holds and what of it is not ours — the state the watermark acts on,
			// readable whether or not it is acting. `accounted` is what the manifests name; the
			// heartbeat's `usage` is every FILE under the root, and the gap between them is whatever
			// nobody indexes. Worth seeing side by side.
			now := archive.Wall()
			away := map[string]string{}
			if objects != nil && server != "" {
				away = Foreign(archive, objects, server, now, 45)
			}
			units, accounted := map[string]map[string]any{}, int64(0)
			for _, u := range archive.Units() {
				b := UnitBytes(archive, u)
				accounted += b
				row := map[string]any{"bytes": b, "days": math.Round(DepthDays(archive, u, now)*100) / 100}
				if s, ok := away[u]; ok {
					row["written_on"] = s
				}
				units[u] = row
			}
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(map[string]any{"server": server, "units": units,
				"foreign": away, "accounted": accounted})
			return true
		case strings.HasPrefix(pth, "/manifest/"):
			unit := pth[strings.LastIndex(pth, "/")+1:] // a UNIT, verbatim: "7" today, "7-backup" the day the spec says so
			if unit == "" {
				w.WriteHeader(404)
				return true
			}
			for _, l := range NewManifest(root, unit).Lines() {
				io.WriteString(w, l+"\n")
			}
			return true
		case strings.HasPrefix(pth, "/segment/"):
			rel := strings.TrimPrefix(pth, "/segment/")
			full := filepath.Join(root, rel)
			st, err := os.Stat(full)
			if strings.Contains(rel, "..") || err != nil || st.IsDir() {
				w.WriteHeader(404)
				return true
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
			f, err := os.Open(full)
			if err != nil {
				w.WriteHeader(404)
				return true
			}
			defer f.Close()
			f.Seek(start, 0)
			data := make([]byte, end-start+1)
			n, _ := io.ReadFull(f, data)
			if rng != "" {
				w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, end, st.Size()))
				w.WriteHeader(206)
			}
			w.Write(data[:n])
			return true
		}
		return false
	}
}

// NewVmsResource: the platform's resource for this server with the VMS
// registered on it, and the event database over its tree (created; the
// process calls Database.Start() after Restore()).
// putSegment: the VMS's one write on the resource, a segment another resource is giving up.
//
// It arrives as bytes plus its manifest line in X-Segment, and lands at the SAME relative path —
// rec/<unit>/e<epoch>/<stamp>.mp4 says nothing about a server, which is why footage can change hands at
// all. The epoch travels with it: it says who WROTE the segment, never where it lies.
//
// Idempotent on purpose: a path already in our manifest is accepted again and the line is not doubled, so
// the sender may retry a batch it is unsure of. Written tmp + rename, the file first and the line after —
// the order Promote uses, for the same reason.
func putSegment(w http.ResponseWriter, req *http.Request, root string) bool {
	rel := strings.TrimPrefix(req.URL.Path, "/segment/")
	if strings.Contains(rel, "..") || !strings.HasPrefix(rel, Sub+"/") || !strings.HasSuffix(rel, ".mp4") {
		w.WriteHeader(400)
		return true
	}
	seg, err := SegmentFromLine(req.Header.Get("X-Segment"))
	if err != nil || seg.Path != rel {
		w.WriteHeader(400)
		return true
	}
	dest := filepath.Join(root, rel)
	os.MkdirAll(filepath.Dir(dest), 0o755)
	data, err := io.ReadAll(req.Body)
	if err != nil {
		w.WriteHeader(400)
		return true
	}
	if os.WriteFile(dest+".tmp", data, 0o644) != nil || os.Rename(dest+".tmp", dest) != nil {
		w.WriteHeader(500)
		return true
	}
	man := NewManifest(root, seg.Unit)
	for _, s := range man.Read() {
		if s.Path == rel { // a retried segment does not get a second line
			w.WriteHeader(204)
			return true
		}
	}
	man.Append(seg)
	w.WriteHeader(204)
	return true
}

func NewVmsResource(archive *ArchiveResource, server, url string, vars p.Variables, objects p.ObjectStore, wall p.Clock, peers p.PeerClient) *p.Resource {
	if wall == nil {
		wall = archive.Wall
	}
	r := p.NewResource(archive.Root, server, url, vars, objects, archive.BucketSeconds, wall, peers)
	r.Register("rec", &ArchivePolicy{Res: archive, Vars: vars, Objects: objects,
		Peers: NewHTTPSegmentPeer(), Server: server}) // footage is the recorder's: rec/<cam>/…, rec/recordings/<cam>
	r.Database = p.NewEventDatabase(archive.Root, server, wall, archive.BucketSeconds)
	return r
}

// NewVmsResourceOn is the same on a box with several disks: one archive tree per volume, one recorder per
// volume (place_by: volume). The resource is still ONE — reachability is a property of a server — but its
// watermark is a loop over the volumes, and the volume goes to the hook.
func NewVmsResourceOn(archives map[string]*ArchiveResource, server, url string, vars p.Variables, objects p.ObjectStore, wall p.Clock, peers p.PeerClient) *p.Resource {
	names := sortedVolumeNames(archives)
	if len(names) == 0 {
		panic("a resource needs at least one volume")
	}
	first := archives[names[0]]
	if wall == nil {
		wall = first.Wall
	}
	vols := make([]p.Volume, 0, len(names))
	for _, n := range names {
		vols = append(vols, p.Volume{Name: n, Path: archives[n].Root})
	}
	r := p.NewResourceOn(vols, server, url, vars, objects, first.BucketSeconds, wall, peers)
	r.Register("rec", &ArchivePolicy{Res: first, Vars: vars, Objects: objects,
		Peers: NewHTTPSegmentPeer(), Server: server, Volumes: archives})
	r.Database = p.NewEventDatabase(first.Root, server, wall, first.BucketSeconds)
	return r
}

// HTTPSegmentPeer is how one archive hands a segment to another over the routes above.
type HTTPSegmentPeer struct{ Client *http.Client }

func NewHTTPSegmentPeer() *HTTPSegmentPeer {
	return &HTTPSegmentPeer{&http.Client{Timeout: 30 * time.Second}}
}

func (c *HTTPSegmentPeer) PutSegment(url, rel string, data []byte, line string) error {
	req, _ := http.NewRequest("PUT", url+"/segment/"+rel, bytes.NewReader(data))
	req.Header.Set("X-Segment", line)
	resp, err := c.Client.Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	if resp.StatusCode != 200 && resp.StatusCode != 201 && resp.StatusCode != 204 {
		return fmt.Errorf("PUT segment %s: %d", rel, resp.StatusCode)
	}
	return nil
}

func (c *HTTPSegmentPeer) Manifest(url, unit string) ([]byte, error) {
	resp, err := c.Client.Get(url + "/manifest/" + unit)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("GET manifest %s: %d", unit, resp.StatusCode)
	}
	return io.ReadAll(resp.Body)
}
