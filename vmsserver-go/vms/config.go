// Package vms is the VMS — the first subsystem the platform hosts.
//
//	vms.subsystem.yaml   the spec: what the controller does for this subsystem — rows, fields, placement, snapshot
//	config.go            Camera, the typed view of a spec row, for the worker and the tests
//	reconciler.go        М9 Lesson 6's loop, unchanged: desired persisted, actual derived
//	archive.go           the archive as a resource: two trees — rec/<cam>/ media (spool → promote → manifest), vms/<cam>/ events; retention as a policy
//	worker.go            vmsworker — DriverPack as the worker: holds N cameras against an assignment, one fan-out each, events, no footage
//	recorder.go          vmsrecorder — the fourth subsystem's worker: subscribes to the worker's tee, writes rec/<cam>/e<epoch>/ on its server's archive
//	rec.subsystem.yaml   the recorder's spec: requires: resource, servers: distinct, near: vms
//	controller.go        vmscontroller — the platform's SpecController run from the spec, in the VMS's words
//	console.go           the one-box console: the read model from heartbeats; writes go to the controller
//
// Nothing here imports from w2cplatform except through its public
// interfaces, and nothing in w2cplatform imports from here.
package vms

import (
	_ "embed"
	"net/url"
	"strconv"
	"strings"

	p "vmsserver/w2cplatform"
)

//go:embed vms.subsystem.yaml
var specYAML string

//go:embed rec.subsystem.yaml
var recSpecYAML string

// Spec is the VMS as the platform sees it; everything the controller does is here.
var Spec = mustSpec(specYAML)

// RecSpec is the recorder — the fourth subsystem — as the platform sees it.
var RecSpec = mustSpec(recSpecYAML)

// VMS is the subsystem's name, which is all a worker needs of the spec above: the keys its slot, epoch,
// assignment and heartbeat live under.
var VMS = Spec.Sub()

const (
	LivePortBase = 20000      // a camera's RTP port on its worker's loopback: the RTSP fan-out's one subscriber
	RTSPPort     = 8554       // the worker's RTSP server: rtsp://<server>:8554/<cam>
	PlaybackPort = 8083       // the holder's playback surface: HTTP, because a browser must be able to seek it
	ShmDir       = "/run/vms" // the tee's shared-memory branch: <ShmDir>/<cam>.shm — a subscriber on the SAME server reads it
)

// LiveURL is the camera's RTSP fan-out on its worker's server — what a subscriber on any server reads.
func LiveURL(server, cid string) string {
	return "rtsp://" + server + ":" + strconv.Itoa(RTSPPort) + "/" + cid
}

// LiveShm is the camera's shared-memory socket on its worker's server: the local fast path (shm:// scheme).
func LiveShm(cid, shmDir string) string {
	return "shm://" + shmDir + "/" + cid + ".shm"
}

// LivePort: the camera's RTP port on its worker's loopback. The one place a unit id
// still has to be a number — a port is a number — so a non-numeric one gets the base
// port and the fan-out simply has none to publish.
func LivePort(cid string) int { return LivePortBase + atoiDef(cid, 0) }

// DeviceOf: the thing DriverPack connects to. Cameras sharing it share one
// session — `driverpack://acme/10.0.0.50/ch/17` and `…/ch/18` are two channels
// of one NVR; a camera with an SD card is a device with one channel. Pure
// parsing: the vendor's own addressing stays opaque, only the grouping is ours.
func DeviceOf(source string) string {
	u, err := url.Parse(source)
	if err != nil || u.Scheme != "driverpack" {
		return source
	}
	parts := pathParts(u.Path)
	if u.Host == "file" {
		if len(parts) > 0 {
			return "file/" + parts[0]
		}
		return "file"
	}
	if len(parts) > 0 {
		return u.Host + "/" + parts[0]
	}
	return u.Host
}

// ChannelOf: `driverpack://<vendor>/<host>/ch/<n>` -> "<n>"; "" when the device
// has one channel.
func ChannelOf(source string) string {
	u, err := url.Parse(source)
	if err != nil {
		return ""
	}
	parts := pathParts(u.Path)
	if u.Host != "file" && len(parts) >= 3 && parts[1] == "ch" {
		return parts[2]
	}
	return ""
}

func pathParts(path string) []string {
	out := []string{}
	for _, seg := range strings.Split(path, "/") {
		if seg != "" {
			out = append(out, seg)
		}
	}
	return out
}

// PlaybackURL: where a camera's OWN archive is served from — the holder's
// playback door. HTTP, not the RTSP fan-out: a browser has to seek inside it,
// and the recorder fetches ranges from the same door.
func PlaybackURL(server, cid string) string {
	return "http://" + server + ":" + strconv.Itoa(PlaybackPort) + "/playback/" + cid
}

func mustSpec(text string) *p.SubsystemSpec {
	v, err := p.ParseYAML(text)
	if err != nil {
		panic(err)
	}
	s, err := p.SpecFromMap(v.(map[string]any))
	if err != nil {
		panic(err)
	}
	return s
}

var (
	OperatorFields  = Spec.FieldOrder
	ForbiddenFields = p.PlatformFields
)

// Camera is a row, typed — the worker's unit (a camera) or the recorder's (a
// recording, ID = the camera). Epoch is not a column: the worker's gate sets
// it on the copy it hands the actuator; so are the fields after it, which
// Enrich adds for the pipeline — the worker's fan-out, the recorder's source.
type Camera struct {
	// ID is the UNIT's id, and it is a string because a subsystem names its units
	// however its spec says: `id: numeric` gives "7", `id: <field>` gives whatever
	// the operator typed. The VMS's are numbers written as strings; a recording's is
	// "7" today and could be "7-main" the day a camera has two of them, with no
	// change here. Cam below is the camera a recording subscribes to — a different
	// identity that `id: cam` happens to make equal.
	ID                  string
	Cam                 string // the recorder's row: WHOSE fan-out this recording records
	Name, Source        string
	Enabled             bool
	EventsRetentionDays int
	RetentionDays       int // the recorder's row: media retention
	Priority            int
	Revision            int
	Labels              []string
	Ref                 string
	Epoch               int
	LiveURL, LiveShm    string // the worker: what it gives out
	LivePort            int
	SourceServer, Via   string // the recorder: where the stream comes from, and how (shm | rtsp)
	// "always" (the default) or "on-demand". A channel of an NVR kept only for its
	// archive needs no live pipeline: the worker still HOLDS the device — its
	// session, its playback, its coverage — and reports the camera as `held`.
	// Thirty-two channels imported for their footage would otherwise be
	// thirty-two streams nobody watches.
	Live           string
	Spool, Archive string
}

func CameraOf(r p.Row) Camera {
	return Camera{ID: r.String("id"), Name: r.String("name"), Source: r.String("source"), Enabled: r.Bool("enabled"),
		EventsRetentionDays: r.Int("events_retention_days"), Priority: r.Int("priority"),
		Revision: r.Int("revision"), Labels: r.List("labels"), Ref: r.String("ref"), Live: r.String("live")}
}

func RowOf(c Camera) p.Row {
	labels := c.Labels
	if labels == nil {
		labels = []string{}
	}
	return p.Row{"id": c.ID, "name": c.Name, "source": c.Source, "enabled": c.Enabled,
		"events_retention_days": c.EventsRetentionDays, "priority": c.Priority, "revision": c.Revision, "labels": labels, "ref": c.Ref}
}

func Row(items p.Items) Camera { return CameraOf(Spec.RowOf(items)) }

// RecRow: a recording row as the recorder's unit. ID is left exactly as the spec
// made it — it used to read `atoiDef(r.String("cam"), 0)`, and that one expression
// was the whole of "a recording is named by its camera", hidden in a parser rather
// than declared in the YAML. Cam is the camera it subscribes to; `id: cam` still
// makes them the same string today, and now that is the spec's statement alone.
func RecRow(items p.Items) Camera {
	r := RecSpec.RowOf(items)
	return Camera{ID: r.String("id"), Cam: r.String("cam"), Name: r.String("cam"), Enabled: r.Bool("enabled"),
		RetentionDays: r.Int("retention_days"), Revision: r.Int("revision"), Labels: r.List("labels")}
}
func ItemsOf(c Camera) p.Items { return Spec.ItemsOf(RowOf(c)) }
func atoiDef(s string, def int) int {
	if n, err := strconv.Atoi(s); err == nil {
		return n
	}
	return def
}

// ToMap renders a row as the console's JSON.
func (c Camera) ToMap() map[string]any {
	return map[string]any(RowOf(c))
}
