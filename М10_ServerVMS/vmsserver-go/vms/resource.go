package vms

// The resource process — the platform's resource job (psimplatform.Resource)
// with the VMS registered on it. One per server, pinned there for as long as
// the server exists; on a box it is `vms resource`, in М11 the `resource`
// system job (clustervms-go re-exports NewVmsResource as ClusterResource).
// It has no controller: it has a policy pass on a timer, a heartbeat, its
// HTTP, and the event database over its own tree. What the VMS adds:
//
//	ArchivePolicy    registered as the "rec" hook: the recorder's — repair the manifests, retain media by rec/recordings/<cam>
//	ResourceRoutes   GET /manifest/<cam>  the manifest's lines;  GET /segment/<path>  the bytes, Range honoured
//
// The platform's part — platform/resources/<server>/heartbeat, GET /events
// from the EventDatabase, /buckets, /mirrored, PUT /mirror — is not the
// VMS's, and the names say so.

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	p "vmsserver/psimplatform"
)

// ResourceRoutes: the VMS's reads on the resource, plugged into the platform's server.
func ResourceRoutes(archive *ArchiveResource) p.Extra {
	root := archive.Root
	return func(w http.ResponseWriter, req *http.Request) bool {
		pth := req.URL.Path
		switch {
		case strings.HasPrefix(pth, "/manifest/"):
			cam, err := strconv.Atoi(pth[strings.LastIndex(pth, "/")+1:])
			if err != nil {
				w.WriteHeader(404)
				return true
			}
			for _, l := range NewManifest(root, cam).Lines() {
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
func NewVmsResource(archive *ArchiveResource, server, url string, vars p.Variables, objects p.ObjectStore, wall p.Clock, peers p.PeerClient) *p.Resource {
	if wall == nil {
		wall = archive.Wall
	}
	r := p.NewResource(archive.Root, server, url, vars, objects, archive.BucketSeconds, wall, peers)
	r.Register("rec", &ArchivePolicy{Res: archive, Vars: vars}) // footage is the recorder's: rec/<cam>/…, rec/recordings/<cam>
	r.Database = p.NewEventDatabase(archive.Root, server, wall, archive.BucketSeconds)
	return r
}
