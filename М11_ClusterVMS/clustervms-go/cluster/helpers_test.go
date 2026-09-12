package cluster_test

// Fakes: a Variables raft in memory, an object store on disk, servers with
// archive directories, a clock. No Nomad, no MinIO, no GStreamer.

import (
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"clustervms/cluster"
	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/vmsplatform"
)

const bucketSeconds = 600

// Server is a box in the cluster: a name, labels, an archive resource on its disks.
type Server struct {
	Name, Labels   string
	Spool, Archive string
	Resource       *vms.ArchiveResource
	Down           bool
}

// Cluster: three servers, one raft, one object store — and a clock the tests own.
type Cluster struct {
	Root    string
	Vars    *cluster.FakeVariables
	Objects *p.FsObjectStore
	Clock   *testbox.Clock
	Wall    *testbox.Clock
	Servers map[string]*Server
	allocs  int
}

func newCluster() *Cluster {
	return newClusterWith([][2]string{{"srv-a", "vlan:cctv-a"}, {"srv-b", "vlan:cctv-a,vlan:cctv-b"}, {"srv-c", "vlan:cctv-b"}})
}

func newClusterWith(servers [][2]string) *Cluster {
	root, _ := os.MkdirTemp("", "clustervms-")
	objects, _ := p.NewFsObjectStore(filepath.Join(root, "objects"))
	c := &Cluster{Root: root, Vars: cluster.NewFakeVariables(), Objects: objects,
		Clock: testbox.NewClock(1000), Wall: testbox.NewClock(1_757_500_000), Servers: map[string]*Server{}}
	for _, s := range servers {
		spool, archive := filepath.Join(root, s[0], "spool"), filepath.Join(root, s[0], "archive")
		c.Servers[s[0]] = &Server{Name: s[0], Labels: s[1], Spool: spool, Archive: archive,
			Resource: vms.NewArchiveResource(spool, archive, bucketSeconds, c.Wall.Now)}
	}
	return c
}

// env is what Nomad puts in an allocation's environment.
func (c *Cluster) env(index int, server, alloc string) cluster.Env {
	c.allocs++
	if alloc == "" {
		alloc = fmt.Sprintf("alloc-%04d", c.allocs)
	}
	return cluster.Env{"NOMAD_ALLOC_INDEX": strconv.Itoa(index), "NOMAD_NODE_NAME": server,
		"NOMAD_META_labels": c.Servers[server].Labels, "NOMAD_ALLOC_ID": alloc}
}

func (c *Cluster) worker(t testing.TB, index int, server string, capacity int, act *vms.FakeActuator) *cluster.ClusterWorker {
	if t != nil {
		t.Helper()
	}
	if capacity == 0 {
		capacity = 50
	}
	if act == nil {
		act = vms.NewFakeActuator()
	}
	w, err := cluster.NewClusterWorker(c.Vars.AsWriter("vmsworker", "vms/epoch/*", "vms/slots/*"), c.Objects, act, c.env(index, server, ""),
		vms.VmsWorkerOptions{WorkerOptions: p.WorkerOptions{Clock: c.Clock.Now, Wall: c.Wall.Now}, Capacity: capacity})
	if err != nil {
		if t == nil {
			panic(err)
		}
		t.Fatal(err)
	}
	return w
}

func (c *Cluster) controller(capacity int, name string) *cluster.ClusterController {
	return cluster.NewClusterController(c.Vars, c.Objects, capacity, c.Wall.Now, name)
}

func (c *Cluster) create(t testing.TB, ctl *cluster.ClusterController, fields map[string]any) vms.Camera {
	t.Helper()
	r, err := ctl.CreateCamera(fields)
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func src(i any) map[string]any {
	return map[string]any{"source": fmt.Sprintf("driverpack://file/%v.mp4", i)}
}

func srcLabels(i any, labels ...string) map[string]any {
	m := src(i)
	m["labels"] = labels
	return m
}

func eq(t *testing.T, got, want any) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v, want %#v", got, want)
	}
}

func actions(a ...string) []vms.Action {
	out := []vms.Action{}
	for _, s := range a {
		verb, id, _ := strings.Cut(s, " ")
		n, _ := strconv.Atoi(id)
		out = append(out, vms.Action{Verb: verb, ID: n})
	}
	return out
}

func sortedKeys[V any](m map[string]V) []string {
	var out []string
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func touch(pth string, mtime float64) {
	tm := time.Unix(0, int64(mtime*1e9))
	os.Chtimes(pth, tm, tm)
}

// dirReader is the console's manifest reader, the index's reader and the
// resources' peer client, against directories instead of HTTP.
type dirReader struct{ c *Cluster }

func (d dirReader) srv(url string) (*Server, error) {
	s := d.c.Servers[url[strings.LastIndex(url, "/")+1:]]
	if s.Down {
		return nil, fmt.Errorf("%s: connection refused", s.Name)
	}
	return s, nil
}

func (d dirReader) Read(url string, cam int) ([]vms.Segment, error) {
	s, err := d.srv(url)
	if err != nil {
		return nil, err
	}
	return vms.NewManifest(s.Archive, cam).Read(), nil
}

func (d dirReader) Buckets(url, sub, unit string) ([]p.Bucket, error) {
	s, err := d.srv(url)
	if err != nil {
		return nil, err
	}
	return p.BucketsUnder(s.Archive, sub, unit, bucketSeconds), nil
}

func (d dirReader) Events(url string, b p.Bucket) ([]p.Event, error) {
	s, err := d.srv(url)
	if err != nil {
		return nil, err
	}
	return p.ReadBucket(filepath.Join(s.Archive, b.Path)), nil
}

func (d dirReader) Mirrored(url, server string) ([]p.Bucket, error) {
	s, err := d.srv(url)
	if err != nil {
		return nil, err
	}
	return p.MirroredBuckets(s.Archive, server, bucketSeconds), nil
}

func (d dirReader) MirroredEvents(url, server string, b p.Bucket) ([]p.Event, error) {
	s, err := d.srv(url)
	if err != nil {
		return nil, err
	}
	return p.ReadBucket(filepath.Join(s.Archive, p.MirrorDir, server, b.Path)), nil
}

func (d dirReader) Put(url, server, path string, data []byte) error {
	s, err := d.srv(url)
	if err != nil {
		return err
	}
	dest := filepath.Join(s.Archive, p.MirrorDir, server, path)
	os.MkdirAll(filepath.Dir(dest), 0o755)
	return os.WriteFile(dest, data, 0o644)
}

func (d dirReader) Get(url, server, path string) ([]byte, error) {
	s, err := d.srv(url)
	if err != nil {
		return nil, err
	}
	return os.ReadFile(filepath.Join(s.Archive, p.MirrorDir, server, path))
}

// segment writes a promoted segment with its manifest line straight into a server's archive.
func segment(t *testing.T, s *Server, cam, epoch int, start float64, seconds float64, size int) {
	t.Helper()
	pth := vms.SegmentPath(s.Archive, cam, epoch, time.Unix(int64(start), 0).UTC())
	os.MkdirAll(filepath.Dir(pth), 0o755)
	os.WriteFile(pth, []byte(strings.Repeat("x", size)), 0o644)
	rel, _ := filepath.Rel(s.Archive, pth)
	vms.NewManifest(s.Archive, cam).Append(vms.Segment{Cam: cam, Epoch: epoch, Start: start, End: start + seconds, Path: filepath.ToSlash(rel), Bytes: int64(size)})
}

// observe: a worker of sub holding unit's epoch on server observed something.
func observe(c *Cluster, server, sub, unit string, epoch int, tt float64, kind string, fields map[string]any) string {
	pth, _ := p.NewEventLog(c.Servers[server].Archive, sub, unit, epoch, bucketSeconds).Append(tt, kind, fields)
	return pth
}

func (c *Cluster) resources(peers p.PeerClient) map[string]*p.Resource {
	out := map[string]*p.Resource{}
	for name, s := range c.Servers {
		r := cluster.ClusterResource(s.Resource, name, "http://"+name, c.Vars, c.Objects, c.Wall.Now, peers)
		r.Heartbeat()
		out[name] = r
	}
	return out
}
