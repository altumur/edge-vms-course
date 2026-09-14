package vms

// The archive as a resource — server-bound, no controller, a policy.
//
//	<spool>/vms/<cam>/e<epoch>/<start>Z.mp4        the open segment, and closed ones not yet promoted
//	<archive>/vms/<cam>/e<epoch>/<start>Z.mp4      promoted: the resource's media
//	<archive>/vms/<cam>/e<epoch>/<start>Z.events.jsonl   the camera's EVENT BUCKETS (the platform's event log)
//	<archive>/vms/<cam>/manifest.jsonl             one line per media segment and one per closed event bucket
//
// The archive's unit is a TIME SPAN under an epoch, not a media file. The
// acknowledgement order is М9 Lesson 4's: a closed segment is PROMOTED
// (renamed into the archive, then a manifest line appended), and the spool
// copy is gone only after that.

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	p "vmsserver/psimplatform"
)

const Sub = "vms"

var (
	segmentRe  = regexp.MustCompile(`^(\d{8}T\d{6}Z)\.mp4$`)
	epochDirRe = regexp.MustCompile(`^e(\d+)$`)
)

func SegmentPath(root string, cam, epoch int, start time.Time) string {
	return filepath.Join(root, Sub, strconv.Itoa(cam), "e"+strconv.Itoa(epoch), start.UTC().Format(p.Stamp)+".mp4")
}

// Parse a segment path under root -> (cam, epoch, start).
func Parse(path, root string) (cam, epoch int, start time.Time, ok bool) {
	rel, err := filepath.Rel(root, path)
	if err != nil {
		return
	}
	parts := strings.Split(filepath.ToSlash(rel), "/")
	if len(parts) != 4 || parts[0] != Sub || !epochDirRe.MatchString(parts[2]) {
		return
	}
	if cam, err = strconv.Atoi(parts[1]); err != nil {
		return 0, 0, time.Time{}, false
	}
	m := segmentRe.FindStringSubmatch(parts[3])
	if m == nil {
		return 0, 0, time.Time{}, false
	}
	start, err = time.Parse(p.Stamp, m[1])
	if err != nil {
		return 0, 0, time.Time{}, false
	}
	epoch, _ = strconv.Atoi(parts[2][1:])
	return cam, epoch, start, true
}

// EventLogFor is the camera's event log on this resource: what the worker
// holding the camera's epoch writes into, recording or not.
func EventLogFor(root string, cam, epoch, bucketSeconds int) *p.EventLog {
	return p.NewEventLog(root, Sub, strconv.Itoa(cam), epoch, bucketSeconds)
}

type Segment struct {
	Cam   int     `json:"cam"`
	Epoch int     `json:"epoch"`
	Start float64 `json:"start"`
	End   float64 `json:"end"`
	Path  string  `json:"path"` // relative to the archive root
	Bytes int64   `json:"bytes"`
}

func (s Segment) Line() string {
	b, _ := json.Marshal(map[string]any{"kind": "media", "cam": s.Cam, "epoch": s.Epoch, "start": s.Start, "end": s.End, "path": s.Path, "bytes": s.Bytes})
	return string(b)
}

func SegmentFromLine(line string) (Segment, error) {
	var d map[string]any
	if err := json.Unmarshal([]byte(line), &d); err != nil {
		return Segment{}, err
	}
	path, _ := d["path"].(string)
	return Segment{int(p.ToFloat(d["cam"])), int(p.ToFloat(d["epoch"])), p.ToFloat(d["start"]), p.ToFloat(d["end"]), path, int64(p.ToFloat(d["bytes"]))}, nil
}

// Span is one entry of a timeline: a media segment, or an event bucket with no media.
type Span struct {
	Start, End float64
	Media      string // "" for a watched, not recorded span
	Epoch      int
	Events     int
	Fenced     bool
}

func (s Span) ToMap() map[string]any {
	var media any
	if s.Media != "" {
		media = s.Media
	}
	return map[string]any{"start": s.Start, "end": s.End, "media": media, "epoch": s.Epoch, "events": s.Events, "fenced": s.Fenced}
}

// Manifest: per camera, append-only, beside the footage.
type Manifest struct{ Path string }

func NewManifest(archiveRoot string, cam int) *Manifest {
	return &Manifest{filepath.Join(p.UnitDir(archiveRoot, Sub, strconv.Itoa(cam)), "manifest.jsonl")}
}

type liner interface{ Line() string }

func (m *Manifest) Append(entry liner) error {
	if err := os.MkdirAll(filepath.Dir(m.Path), 0o755); err != nil {
		return err
	}
	f, err := os.OpenFile(m.Path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = io.WriteString(f, entry.Line()+"\n")
	return err
}

func (m *Manifest) Lines() []string {
	f, err := os.Open(m.Path)
	if err != nil {
		return nil
	}
	defer f.Close()
	var out []string
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<24)
	for sc.Scan() {
		if strings.TrimSpace(sc.Text()) != "" {
			out = append(out, sc.Text())
		}
	}
	return out
}

func kindOf(line string) string {
	var d struct {
		Kind string `json:"kind"`
	}
	json.Unmarshal([]byte(line), &d)
	if d.Kind == "" {
		return "media"
	}
	return d.Kind
}

// Read: the media lines — what a player needs.
func (m *Manifest) Read() []Segment {
	out := []Segment{}
	for _, l := range m.Lines() {
		if kindOf(l) == "media" {
			if s, err := SegmentFromLine(l); err == nil {
				out = append(out, s)
			}
		}
	}
	return out
}

// Buckets: the closed event buckets — what an index needs.
func (m *Manifest) Buckets() []p.Bucket {
	out := []p.Bucket{}
	for _, l := range m.Lines() {
		if kindOf(l) == "events" {
			if b, err := p.BucketFromLine(l); err == nil {
				out = append(out, b)
			}
		}
	}
	return out
}

type entry struct {
	start float64
	epoch int
	line  string
}

func (m *Manifest) Rewrite(segs []Segment, buckets []p.Bucket) error {
	if buckets == nil {
		buckets = m.Buckets()
	}
	var es []entry
	for _, s := range segs {
		es = append(es, entry{s.Start, s.Epoch, s.Line()})
	}
	for _, b := range buckets {
		es = append(es, entry{b.Start, b.Epoch, b.Line()})
	}
	sort.SliceStable(es, func(i, j int) bool {
		if es[i].start != es[j].start {
			return es[i].start < es[j].start
		}
		return es[i].epoch < es[j].epoch
	})
	os.MkdirAll(filepath.Dir(m.Path), 0o755)
	f, err := os.Create(m.Path + ".tmp")
	if err != nil {
		return err
	}
	for _, e := range es {
		io.WriteString(f, e.line+"\n")
	}
	f.Close()
	return os.Rename(m.Path+".tmp", m.Path)
}

// Timeline: spans overlapping [t0, t1) — media segments, and event buckets
// with no media (the camera was watched, not recorded). Each marked fenced
// if its epoch is older than the current (0 = unknown).
func (m *Manifest) Timeline(t0, t1 float64, currentEpoch int) []Span {
	var out []Span
	for _, s := range m.Read() {
		if s.End > t0 && s.Start < t1 {
			out = append(out, Span{s.Start, s.End, s.Path, s.Epoch, 0, currentEpoch > 0 && s.Epoch < currentEpoch})
		}
	}
	for _, b := range m.Buckets() {
		if !(b.End > t0 && b.Start < t1) {
			continue
		}
		hit := -1
		for i, o := range out {
			if o.Media != "" && o.Epoch == b.Epoch && o.Start < b.End && b.Start < o.End {
				hit = i
				break
			}
		}
		if hit >= 0 {
			out[hit].Events += b.Events // events during a recorded span: count them on it
		} else {
			out = append(out, Span{b.Start, b.End, "", b.Epoch, b.Events, currentEpoch > 0 && b.Epoch < currentEpoch})
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Start != out[j].Start {
			return out[i].Start < out[j].Start
		}
		return out[i].Epoch < out[j].Epoch
	})
	if out == nil {
		out = []Span{}
	}
	return out
}

// ArchiveResource is one server's archive. Promote is what archivesink
// calls on fragment-closed; Repair is what М11 called the re-index sweep.
type ArchiveResource struct {
	Spool, Root   string
	BucketSeconds int
	Wall          p.Clock
}

func NewArchiveResource(spool, root string, bucketSeconds int, wall p.Clock) *ArchiveResource {
	if bucketSeconds == 0 {
		bucketSeconds = 600
	}
	if wall == nil {
		wall = p.Wall()
	}
	os.MkdirAll(spool, 0o755)
	os.MkdirAll(root, 0o755)
	return &ArchiveResource{spool, root, bucketSeconds, wall}
}

func mtime(path string) float64 {
	st, err := os.Stat(path)
	if err != nil {
		return 0
	}
	return float64(st.ModTime().UnixNano()) / 1e9
}

// Promote: 1. into the archive, atomically; 2. then the manifest line; 3. the spool copy last.
func (a *ArchiveResource) Promote(spoolPath string, end float64) (Segment, error) {
	cam, epoch, start, ok := Parse(spoolPath, a.Spool)
	if !ok {
		return Segment{}, fmt.Errorf("not a segment path: %s", spoolPath)
	}
	st, err := os.Stat(spoolPath)
	if err != nil {
		return Segment{}, err
	}
	if end == 0 {
		end = float64(st.ModTime().UnixNano()) / 1e9
	}
	rel, _ := filepath.Rel(a.Spool, spoolPath)
	rel = filepath.ToSlash(rel)
	dest := filepath.Join(a.Root, rel)
	os.MkdirAll(filepath.Dir(dest), 0o755)
	if err := move(spoolPath, dest); err != nil {
		return Segment{}, err
	}
	seg := Segment{cam, epoch, float64(start.Unix()), end, rel, st.Size()}
	return seg, NewManifest(a.Root, cam).Append(seg)
}

func move(src, dest string) error {
	if err := os.Rename(src, dest); err == nil {
		return nil
	}
	data, err := os.ReadFile(src) // different filesystem: copy, then appear whole
	if err != nil {
		return err
	}
	if err := os.WriteFile(dest+".tmp", data, 0o644); err != nil {
		return err
	}
	if err := os.Rename(dest+".tmp", dest); err != nil {
		return err
	}
	return os.Remove(src)
}

// CloseBuckets: event buckets whose span is over and that nobody has
// written to for the grace get their manifest line.
func (a *ArchiveResource) CloseBuckets(now, graceSeconds float64) []p.Bucket {
	closed := []p.Bucket{}
	for _, cam := range a.Cameras() {
		man := NewManifest(a.Root, cam)
		known := map[string]bool{}
		for _, b := range man.Buckets() {
			known[b.Path] = true
		}
		for _, b := range p.BucketsUnder(a.Root, Sub, strconv.Itoa(cam), a.BucketSeconds) {
			if known[b.Path] || b.End > now || now-mtime(filepath.Join(a.Root, b.Path)) < graceSeconds {
				continue
			}
			man.Append(b)
			closed = append(closed, b)
		}
	}
	return closed
}

func (a *ArchiveResource) Cameras() []int {
	ents, err := os.ReadDir(filepath.Join(a.Root, Sub))
	if err != nil {
		return nil
	}
	var out []int
	for _, e := range ents {
		if n, err := strconv.Atoi(e.Name()); err == nil {
			out = append(out, n)
		}
	}
	sort.Ints(out)
	return out
}

// ClosedInSpool: segments in the spool older than the grace — closed, not
// yet promoted (a worker died between close and promote).
func (a *ArchiveResource) ClosedInSpool(graceSeconds, now float64) []string {
	var out []string
	filepath.WalkDir(a.Spool, func(pth string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return nil
		}
		if _, _, _, ok := Parse(pth, a.Spool); ok && now-mtime(pth) >= graceSeconds {
			out = append(out, pth)
		}
		return nil
	})
	sort.Strings(out)
	return out
}

type RepairReport struct{ Added, Dropped int }

// Repair: make the manifests agree with the files. Idempotent.
func (a *ArchiveResource) Repair() RepairReport {
	var rep RepairReport
	for _, cam := range a.Cameras() {
		man := NewManifest(a.Root, cam)
		lines := map[string]Segment{}
		for _, s := range man.Read() {
			lines[s.Path] = s
		}
		present := map[string]bool{}
		filepath.WalkDir(p.UnitDir(a.Root, Sub, strconv.Itoa(cam)), func(pth string, d fs.DirEntry, err error) error {
			if err != nil || d.IsDir() {
				return nil
			}
			if c, epoch, start, ok := Parse(pth, a.Root); ok {
				rel, _ := filepath.Rel(a.Root, pth)
				rel = filepath.ToSlash(rel)
				present[rel] = true
				if _, have := lines[rel]; !have {
					st, _ := os.Stat(pth)
					lines[rel] = Segment{c, epoch, float64(start.Unix()), float64(st.ModTime().UnixNano()) / 1e9, rel, st.Size()}
					rep.Added++
				}
			}
			return nil
		})
		for rel := range lines {
			if !present[rel] {
				delete(lines, rel)
				rep.Dropped++
			}
		}
		// event buckets: every CLOSED bucket on disk is a line; a line whose file is gone is dropped
		known := map[string]bool{}
		for _, b := range man.Buckets() {
			known[b.Path] = true
		}
		onDisk := map[string]bool{}
		var closed []p.Bucket
		for _, b := range p.BucketsUnder(a.Root, Sub, strconv.Itoa(cam), a.BucketSeconds) {
			if b.End <= a.Wall() {
				closed = append(closed, b)
				onDisk[b.Path] = true
				if !known[b.Path] {
					rep.Added++
				}
			}
		}
		for pth := range known {
			if !onDisk[pth] {
				rep.Dropped++
			}
		}
		var segs []Segment
		for _, s := range lines {
			segs = append(segs, s)
		}
		if closed == nil {
			closed = []p.Bucket{}
		}
		man.Rewrite(segs, closed)
	}
	return rep
}

// Retain: delete media older than days — the file first, then the line.
// The buckets are the platform's to retain.
func (a *ArchiveResource) Retain(cam int, days, now float64) int {
	cutoff := now - days*86400
	man := NewManifest(a.Root, cam)
	var keep []Segment
	removed := 0
	for _, s := range man.Read() {
		if s.End < cutoff {
			os.Remove(filepath.Join(a.Root, s.Path))
			removed++
		} else {
			keep = append(keep, s)
		}
	}
	if removed > 0 {
		man.Rewrite(keep, nil)
	}
	return removed
}

func (a *ArchiveResource) Usage() int64 {
	var total int64
	filepath.WalkDir(a.Root, func(pth string, d fs.DirEntry, err error) error {
		if err == nil && !d.IsDir() {
			if _, _, _, ok := Parse(pth, a.Root); ok {
				if st, e := d.Info(); e == nil {
					total += st.Size()
				}
			}
		}
		return nil
	})
	return total
}

// ArchivePolicy is what the VMS registers with the platform's resource job:
// repair the manifests, close the buckets into them, retain media per camera
// from the camera rows.
type ArchivePolicy struct {
	Res  *ArchiveResource
	Vars p.Variables
}

func (ap *ArchivePolicy) Pass(now float64) map[string]any {
	rep := ap.Res.Repair()
	closed := len(ap.Res.CloseBuckets(now, 30))
	removed := 0
	for _, cam := range ap.Res.Cameras() {
		items, _, _ := ap.Vars.Get(Sub + "/cameras/" + strconv.Itoa(cam))
		days := 30
		if items != nil {
			days = atoiDef(items["retention_days"], 30)
		}
		removed += ap.Res.Retain(cam, float64(days), now)
	}
	return map[string]any{"added": rep.Added, "dropped": rep.Dropped, "closed": closed, "media_removed": removed}
}
