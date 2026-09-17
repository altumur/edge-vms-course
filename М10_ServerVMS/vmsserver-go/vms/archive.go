package vms

// The archive as a resource — server-bound, no controller, a policy. Two
// trees for one camera, two writers, two epochs:
//
//	<spool>/rec/<cam>/e<epoch>/<start>Z.mp4        the open segment, and closed ones not yet promoted — the RECORDER's
//	<archive>/rec/<cam>/e<epoch>/<start>Z.mp4      promoted: the resource's media, under the recorder's epoch
//	<archive>/rec/<cam>/manifest.jsonl             one line per media segment; media only
//	<archive>/vms/<cam>/e<epoch>/<start>Z.events.jsonl   the camera's EVENT BUCKETS — the WORKER's, under its epoch (the platform's event log)
//
// The manifest indexes media; the resource's event database indexes the
// buckets. The acknowledgement order is М9 Lesson 4's: a closed segment is
// PROMOTED (renamed into the archive, then a manifest line appended), and
// the spool copy is gone only after that.

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	p "vmsserver/w2cplatform"
)

const (
	Sub       = "rec" // footage: the recorder's tree and epoch
	EventsSub = "vms" // events: the worker's tree and epoch
)

var (
	segmentRe  = regexp.MustCompile(`^(\d{8}T\d{6}Z)\.mp4$`)
	epochDirRe = regexp.MustCompile(`^e(\d+)$`)
)

// SegmentPath: <root>/rec/<unit>/e<epoch>/<start>Z.mp4 — the same grammar in
// the spool and the archive.
//
// The middle segment is the UNIT, not the camera. Today they are the same
// string, because rec.subsystem.yaml says `id: cam` — a recording is named by
// the camera it records. The path code must not know that: the day a camera
// gets two recordings (two servers, two profiles) the unit is "7-main" and
// "7-backup", and this grammar keeps working unchanged.
func SegmentPath(root, unit string, epoch int, start time.Time) string {
	return filepath.Join(root, Sub, unit, "e"+strconv.Itoa(epoch), start.UTC().Format(p.Stamp)+".mp4")
}

// Parse a segment path under root -> (unit, epoch, start).
func Parse(path, root string) (unit string, epoch int, start time.Time, ok bool) {
	rel, err := filepath.Rel(root, path)
	if err != nil {
		return
	}
	parts := strings.Split(filepath.ToSlash(rel), "/")
	if len(parts) != 4 || parts[0] != Sub || parts[1] == "" || !epochDirRe.MatchString(parts[2]) {
		return
	}
	m := segmentRe.FindStringSubmatch(parts[3])
	if m == nil {
		return "", 0, time.Time{}, false
	}
	start, err = time.Parse(p.Stamp, m[1])
	if err != nil {
		return "", 0, time.Time{}, false
	}
	epoch, _ = strconv.Atoi(parts[2][1:])
	return parts[1], epoch, start, true
}

// EventLogFor is the camera's event log on this resource: what the worker
// holding the camera's epoch writes into, recorded or not — vms/<cam>/e<epoch>/.
func EventLogFor(root, cam string, epoch, bucketSeconds int) *p.EventLog {
	return p.NewEventLog(root, EventsSub, cam, epoch, bucketSeconds)
}

type Segment struct {
	// Unit: the recording this footage belongs to; `id: cam` makes it the camera's id today.
	Unit  string  `json:"unit"`
	Epoch int     `json:"epoch"`
	Start float64 `json:"start"`
	End   float64 `json:"end"`
	Path  string  `json:"path"` // relative to the archive root
	Bytes int64   `json:"bytes"`
	// Source: "live" — we recorded it off the fan-out as it happened — or
	// "edge": we fetched it later from the device's own archive. It lives in the
	// LINE and not in the path, so the footage on disk is the same footage
	// either way and nothing has to move when a gap is filled. The named cost:
	// Repair rebuilds a lost manifest from the files, and a file cannot say
	// where it came from, so a repaired line reads "live".
	Source string `json:"source"`
}

func (s Segment) Line() string {
	src := s.Source
	if src == "" {
		src = "live"
	}
	b, _ := json.Marshal(map[string]any{"kind": "media", "unit": s.Unit, "epoch": s.Epoch, "start": s.Start, "end": s.End,
		"path": s.Path, "bytes": s.Bytes, "source": src})
	return string(b)
}

func SegmentFromLine(line string) (Segment, error) {
	var d map[string]any
	if err := json.Unmarshal([]byte(line), &d); err != nil {
		return Segment{}, err
	}
	path, _ := d["path"].(string)
	src, _ := d["source"].(string)
	if src == "" {
		src = "live" // every line written before Lesson 15 is live footage
	}
	unit, _ := d["unit"].(string)
	if unit == "" { // `cam` is what a line written before the unit-keyed tree says
		unit = p.Str(d["cam"])
		if f, isNum := d["cam"].(float64); isNum {
			unit = strconv.Itoa(int(f))
		}
	}
	return Segment{unit, int(p.ToFloat(d["epoch"])), p.ToFloat(d["start"]), p.ToFloat(d["end"]), path, int64(p.ToFloat(d["bytes"])), src}, nil
}

// Span is one entry of a timeline: a media segment under the recorder's epoch.
type Span struct {
	Start, End float64
	Media      string
	Epoch      int
	Fenced     bool
}

func (s Span) ToMap() map[string]any {
	return map[string]any{"start": s.Start, "end": s.End, "media": s.Media, "epoch": s.Epoch, "fenced": s.Fenced}
}

// Manifest: per camera, append-only, beside the footage.
type Manifest struct{ Path string }

func NewManifest(archiveRoot, unit string) *Manifest {
	return &Manifest{filepath.Join(p.UnitDir(archiveRoot, Sub, unit), "manifest.jsonl")}
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

// Read: the media lines — what a player needs. The manifest holds nothing else.
func (m *Manifest) Read() []Segment {
	out := []Segment{}
	for _, l := range m.Lines() {
		if s, err := SegmentFromLine(l); err == nil && s.Path != "" {
			out = append(out, s)
		}
	}
	return out
}

func (m *Manifest) Rewrite(segs []Segment) error {
	es := append([]Segment{}, segs...)
	sort.SliceStable(es, func(i, j int) bool {
		if es[i].Start != es[j].Start {
			return es[i].Start < es[j].Start
		}
		return es[i].Epoch < es[j].Epoch
	})
	os.MkdirAll(filepath.Dir(m.Path), 0o755)
	f, err := os.Create(m.Path + ".tmp")
	if err != nil {
		return err
	}
	for _, e := range es {
		io.WriteString(f, e.Line()+"\n")
	}
	f.Close()
	return os.Rename(m.Path+".tmp", m.Path)
}

// Timeline: media spans overlapping [t0, t1), each marked fenced if its
// epoch is older than the recorder's current one (0 = unknown). Events are
// not here: the resource's event database has them, and the console draws
// them over these spans.
func (m *Manifest) Timeline(t0, t1 float64, currentEpoch int) []Span {
	var out []Span
	for _, s := range m.Read() {
		if s.End > t0 && s.Start < t1 {
			out = append(out, Span{s.Start, s.End, s.Path, s.Epoch, currentEpoch > 0 && s.Epoch < currentEpoch})
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
// Promote moves a closed spool file into the archive and then writes its line.
// source is "live" for footage off the fan-out and "edge" for footage fetched
// from a device's own archive; "" means live.
func (a *ArchiveResource) Promote(spoolPath string, end float64, source string) (Segment, error) {
	if source == "" {
		source = "live"
	}
	unit, epoch, start, ok := Parse(spoolPath, a.Spool)
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
	seg := Segment{unit, epoch, float64(start.Unix()), end, rel, st.Size(), source}
	return seg, NewManifest(a.Root, unit).Append(seg)
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

// Cameras: the cameras with a rec/ tree — recorded, now or once.
// Units: the units with a rec/<unit>/ directory. Strings, sorted so that
// numeric names — which is all of them while `id: cam` holds — come out in
// numeric order rather than "1, 10, 2".
func (a *ArchiveResource) Units() []string {
	ents, err := os.ReadDir(filepath.Join(a.Root, Sub))
	if err != nil {
		return nil
	}
	var out []string
	for _, e := range ents {
		if e.IsDir() {
			out = append(out, e.Name())
		}
	}
	sort.Slice(out, func(i, j int) bool {
		a, aerr := strconv.Atoi(out[i])
		b, berr := strconv.Atoi(out[j])
		if aerr == nil && berr == nil {
			return a < b
		}
		if aerr == nil != (berr == nil) {
			return aerr == nil
		}
		return out[i] < out[j]
	})
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
	for _, unit := range a.Units() {
		man := NewManifest(a.Root, unit)
		lines := map[string]Segment{}
		for _, s := range man.Read() {
			lines[s.Path] = s
		}
		present := map[string]bool{}
		filepath.WalkDir(p.UnitDir(a.Root, Sub, unit), func(pth string, d fs.DirEntry, err error) error {
			if err != nil || d.IsDir() {
				return nil
			}
			if u, epoch, start, ok := Parse(pth, a.Root); ok {
				rel, _ := filepath.Rel(a.Root, pth)
				rel = filepath.ToSlash(rel)
				present[rel] = true
				if _, have := lines[rel]; !have {
					st, _ := os.Stat(pth)
					// a file cannot say where it came from: a rebuilt line reads "live"
					lines[rel] = Segment{u, epoch, float64(start.Unix()), float64(st.ModTime().UnixNano()) / 1e9, rel, st.Size(), "live"}
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
		var segs []Segment
		for _, s := range lines {
			segs = append(segs, s)
		}
		man.Rewrite(segs)
	}
	return rep
}

// Retain: delete media older than days — the file first, then the line.
// The buckets are the platform's to retain, by the VMS row's events_retention_days.
func (a *ArchiveResource) Retain(unit string, days, now float64) int {
	cutoff := now - days*86400
	man := NewManifest(a.Root, unit)
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
		man.Rewrite(keep)
	}
	return removed
}

// Coverage: the camera's footage as runs of wall-clock time, gaps under
// `stitch` seconds closed over. It is what a timeline is drawn from and what
// backfill measures itself against — segment boundaries are an implementation
// detail of recording, not something an operator should have to see.
func (a *ArchiveResource) Coverage(unit string, stitch float64) [][2]float64 {
	if stitch == 0 {
		stitch = 2
	}
	segs := NewManifest(a.Root, unit).Read()
	sort.Slice(segs, func(i, j int) bool { return segs[i].Start < segs[j].Start })
	runs := [][2]float64{}
	for _, s := range segs {
		if n := len(runs); n > 0 && s.Start <= runs[n-1][1]+stitch {
			if s.End > runs[n-1][1] {
				runs[n-1][1] = s.End
			}
			continue
		}
		runs = append(runs, [2]float64{s.Start, s.End})
	}
	return runs
}

// Subtract: `want` minus every range in `have`. The one subtraction two things
// use — the console draws a device's coverage only where ours does not cover it
// (Lesson 15), and the recorder fetches only what it does not have (Lesson 16).
// One rule, two uses, so they cannot drift apart.
func Subtract(want [2]float64, have [][2]float64) [][2]float64 {
	out := [][2]float64{want}
	h := append([][2]float64(nil), have...)
	sort.Slice(h, func(i, j int) bool { return h[i][0] < h[j][0] })
	for _, r := range h {
		a, b := r[0], r[1]
		next := [][2]float64{}
		for _, seg := range out {
			x, y := seg[0], seg[1]
			if b <= x || a >= y {
				next = append(next, seg)
				continue
			}
			if a > x {
				next = append(next, [2]float64{x, math.Min(a, y)})
			}
			if b < y {
				next = append(next, [2]float64{math.Max(b, x), y})
			}
		}
		out = next
	}
	kept := [][2]float64{}
	for _, seg := range out {
		if seg[1] > seg[0] {
			kept = append(kept, seg)
		}
	}
	return kept
}

// Overlaps: does any range in `have` touch `span`?
func Overlaps(have [][2]float64, span [2]float64) bool {
	for _, r := range have {
		if r[0] < span[1] && span[0] < r[1] {
			return true
		}
	}
	return false
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

// ArchivePolicy is what the RECORDER registers with the platform's resource
// job (the "rec" hook): repair the manifests, retain media per camera from
// the recording rows (rec/recordings/<cam>, retention_days; 30 for a camera
// with no row).
type ArchivePolicy struct {
	Res  *ArchiveResource
	Vars p.Variables
}

func (ap *ArchivePolicy) Pass(now float64) map[string]any {
	rep := ap.Res.Repair()
	removed := 0
	for _, unit := range ap.Res.Units() {
		items, _, _ := ap.Vars.Get(Sub + "/recordings/" + unit) // the unit's own row: its retention, not the camera's
		days := 30
		if items != nil {
			days = atoiDef(items["retention_days"], 30)
		}
		removed += ap.Res.Retain(unit, float64(days), now)
	}
	return map[string]any{"added": rep.Added, "dropped": rep.Dropped, "media_removed": removed}
}
