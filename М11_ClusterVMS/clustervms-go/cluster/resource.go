package cluster

// The resource job on a cluster is the platform's (vmsplatform.Resource): a
// system job on every server with meta.archive, serving buckets, taking
// mirrors from its peers, retaining every subsystem's buckets by that
// subsystem's policy. What this module adds is the VMS's part of it:
//
//	ArchivePolicy   registered as the "vms" hook: repair the manifests, close buckets into them, retain media
//	VmsRoutes       GET /manifest/<cam>  the manifest's lines;  GET /segment/<path>  the bytes, Range honoured
//
// Nothing about mirrors, heartbeats or peers is the VMS's, and the names say
// so: platform/resources/<server>/heartbeat, platform/mirror, job "resource".

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"vmsserver/vms"
	p "vmsserver/vmsplatform"
)

// VmsRoutes: the VMS's reads on the resource, plugged into the platform's server.
func VmsRoutes(archive *vms.ArchiveResource) p.Extra {
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
			for _, l := range vms.NewManifest(root, cam).Lines() {
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

// ClusterResource: the platform's resource for this server with the VMS registered on it.
func ClusterResource(archive *vms.ArchiveResource, server, url string, vars Variables, objects ObjectStore, wall p.Clock, peers p.PeerClient) *p.Resource {
	if wall == nil {
		wall = archive.Wall
	}
	r := p.NewResource(archive.Root, server, url, vars, objects, archive.BucketSeconds, wall, peers)
	r.Register("vms", &vms.ArchivePolicy{Res: archive, Vars: vars})
	return r
}
