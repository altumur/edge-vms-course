package psimplatform

// The event log — a platform piece. What the platform knows about events:
//
//	a bucket    <resource>/<subsystem>/<unit>/e<epoch>/<start>Z.events.jsonl
//	            JSON lines {t, kind, ...}, for a span of bucketSeconds starting at <start>
//	its writer  the worker that holds that unit's epoch — one writer per file, by construction
//	its fence   the epoch in the path: a stale instance writes into its own bucket, marked afterwards
//	its index   the manifest beside the unit's buckets, and (М11) a cluster-wide cache over every resource
//
// The word "event" means only: something a worker observed at a time,
// about a unit it holds.

import (
	"bufio"
	"encoding/json"
	"io/fs"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

var (
	eventsRe   = regexp.MustCompile(`^(\d{8}T\d{6}Z)\.events\.jsonl$`)
	epochDirRe = regexp.MustCompile(`^e(\d+)$`)
)

const Stamp = "20060102T150405Z"

func BucketStart(t float64, bucketSeconds int) float64 {
	return math.Floor(t/float64(bucketSeconds)) * float64(bucketSeconds)
}

func StampOf(t float64) string {
	return time.Unix(int64(math.Floor(t)), 0).UTC().Format(Stamp)
}

func ParseStamp(s string) (float64, bool) {
	t, err := time.Parse(Stamp, s)
	if err != nil {
		return 0, false
	}
	return float64(t.Unix()), true
}

func UnitDir(root, subsystem, unit string) string { return filepath.Join(root, subsystem, unit) }

func BucketPath(root, subsystem, unit string, epoch int, start float64) string {
	return filepath.Join(UnitDir(root, subsystem, unit), "e"+strconv.Itoa(epoch), StampOf(start)+".events.jsonl")
}

// ParseBucket -> (subsystem, unit, epoch, start) for a bucket path under root.
func ParseBucket(path, root string) (sub, unit string, epoch int, start float64, ok bool) {
	rel, err := filepath.Rel(root, path)
	if err != nil {
		return
	}
	parts := strings.Split(filepath.ToSlash(rel), "/")
	if len(parts) != 4 || !epochDirRe.MatchString(parts[2]) {
		return
	}
	m := eventsRe.FindStringSubmatch(parts[3])
	if m == nil {
		return
	}
	start, ok = ParseStamp(m[1])
	if !ok {
		return
	}
	epoch, _ = strconv.Atoi(parts[2][1:])
	return parts[0], parts[1], epoch, start, true
}

type Bucket struct {
	Subsystem string  `json:"subsystem"`
	Unit      string  `json:"unit"`
	Epoch     int     `json:"epoch"`
	Start     float64 `json:"start"`
	End       float64 `json:"end"`
	Path      string  `json:"path"` // relative to the resource root
	Events    int     `json:"events"`
}

func (b Bucket) Line() string {
	m := map[string]any{"kind": "events", "subsystem": b.Subsystem, "unit": b.Unit, "epoch": b.Epoch,
		"start": b.Start, "end": b.End, "path": b.Path, "events": b.Events}
	out, _ := json.Marshal(m)
	return string(out)
}

func BucketFromLine(line string) (Bucket, error) {
	var d map[string]any
	if err := json.Unmarshal([]byte(line), &d); err != nil {
		return Bucket{}, err
	}
	p, _ := d["path"].(string)
	return Bucket{Str(d["subsystem"]), Str(d["unit"]), int(ToFloat(d["epoch"])), ToFloat(d["start"]), ToFloat(d["end"]), p, int(ToFloat(d["events"]))}, nil
}

// Event is one JSON line: {t, kind, ...fields}.
type Event map[string]any

func (e Event) T() float64   { return ToFloat(e["t"]) }
func (e Event) Kind() string { k, _ := e["kind"].(string); return k }

// EventLog is what a worker holds per unit it has an epoch for. Append
// writes one line, flushed, into the bucket for t; buckets roll by the clock.
type EventLog struct {
	Root, Subsystem, Unit string
	Epoch, BucketSeconds  int
}

func NewEventLog(root, subsystem, unit string, epoch int, bucketSeconds int) *EventLog {
	if bucketSeconds == 0 {
		bucketSeconds = 600
	}
	return &EventLog{root, subsystem, unit, epoch, bucketSeconds}
}

func (l *EventLog) PathFor(t float64) string {
	return BucketPath(l.Root, l.Subsystem, l.Unit, l.Epoch, BucketStart(t, l.BucketSeconds))
}

func (l *EventLog) Append(t float64, kind string, fields map[string]any) (string, error) {
	p := l.PathFor(t)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		return "", err
	}
	m := map[string]any{"t": t, "kind": kind}
	for k, v := range fields {
		m[k] = v
	}
	line, _ := json.Marshal(m)
	f, err := os.OpenFile(p, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return "", err
	}
	defer f.Close()
	_, err = f.Write(append(line, '\n'))
	return p, err
}

func ReadBucket(path string) []Event {
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	var out []Event
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<24)
	for sc.Scan() {
		if strings.TrimSpace(sc.Text()) == "" {
			continue
		}
		var e Event
		if json.Unmarshal(sc.Bytes(), &e) == nil {
			out = append(out, e)
		}
	}
	return out
}

func countLines(path string) int {
	return len(ReadBucket(path))
}

// BucketsUnder: every bucket file for a unit, from the files alone.
func BucketsUnder(root, subsystem, unit string, bucketSeconds int) []Bucket {
	var out []Bucket
	base := UnitDir(root, subsystem, unit)
	filepath.WalkDir(base, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return nil
		}
		if sub, u, epoch, start, ok := ParseBucket(p, root); ok {
			rel, _ := filepath.Rel(root, p)
			out = append(out, Bucket{sub, u, epoch, start, start + float64(bucketSeconds), filepath.ToSlash(rel), countLines(p)})
		}
		return nil
	})
	sortBuckets(out)
	return out
}

func sortBuckets(bs []Bucket) {
	sort.Slice(bs, func(i, j int) bool {
		if bs[i].Start != bs[j].Start {
			return bs[i].Start < bs[j].Start
		}
		return bs[i].Epoch < bs[j].Epoch
	})
}

// SubsystemsUnder: {subsystem: [unit, ...]} present on a resource — the
// index's discovery, no registry.
func SubsystemsUnder(root string) map[string][]string {
	out := map[string][]string{}
	ents, err := os.ReadDir(root)
	if err != nil {
		return out
	}
	for _, e := range ents {
		if !e.IsDir() || strings.HasPrefix(e.Name(), ".") {
			continue
		}
		units := []string{}
		us, _ := os.ReadDir(filepath.Join(root, e.Name()))
		for _, u := range us {
			if u.IsDir() {
				units = append(units, u.Name())
			}
		}
		sort.Strings(units)
		out[e.Name()] = units
	}
	return out
}
