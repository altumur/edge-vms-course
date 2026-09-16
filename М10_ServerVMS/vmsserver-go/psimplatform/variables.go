// Package psimplatform is the platform, on one box — in Go. Everything here
// would host any fleet of stateless shards writing bulk data; nothing here
// knows what the units of work are.
//
//	variables.go   a small, consistent config store with ModifyIndex and check-and-set (file-backed)
//	objects.go     an object store (a directory)
//	epoch.go       the fencing-token issuer and the lease, generic
//	contract.go    what a subsystem gives the platform: a controller and its workers
//	events.go      the event log: buckets per unit per epoch, written by the epoch's holder
//	resource.go    the resource job: one per server; heartbeat, retention, the peer mirror
//	eventdatabase.go  the event "database", which is a cache each resource keeps over its own tree; the console merges
//
// М11 replaces variables.go with Nomad Variables behind the same interface
// and changes nothing above this line.
package psimplatform

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
)

// Items is one Variable's payload. Nomad stores strings; so do we.
type Items map[string]string

// Index is a version of a path in the store, and it is OPAQUE: compare it
// for equality, hand it back on the next write, and do nothing else with it.
// Nomad's ModifyIndex is a number; Kubernetes' resourceVersion is a string
// its API conventions forbid a client to interpret. So the platform never
// orders indexes, never subtracts them and never counts with them — which is
// the one clause that decides whether a second backend is possible at all.
// (Python says this as `Index = str | int`; Go says it as a comparable
// struct, so the compiler refuses the arithmetic outright.)
type Index struct {
	v   string
	set bool
}

// NoCAS is the zero Index: write unconditionally, whatever the path holds.
// Every write that matters passes the Index it read instead.
var NoCAS = Index{}

// Absent is what Get returns for a path that does not exist — and, passed as
// cas, "this path must not exist yet": create-only, the first claimant wins.
var Absent = Index{set: true}

// Idx wraps a backend's own version token. Only a backend calls it.
func Idx(v string) Index { return Index{v: v, set: true} }

// String is for logs and error messages, never for a decision.
func (i Index) String() string {
	if !i.set {
		return "(no cas)"
	}
	if i.v == "" {
		return "(absent)"
	}
	return i.v
}

// Exists: this Index names a version, so the path was there when it was read.
func (i Index) Exists() bool { return i.set && i.v != "" }

// Conditional: this Index is a condition on the write, rather than NoCAS.
func (i Index) Conditional() bool { return i.set }

// ErrConflict: the cas index did not match the current ModifyIndex.
var ErrConflict = errors.New("cas conflict: the ModifyIndex moved")

// ErrForbidden: this writer may not write that path — one writer per prefix.
var ErrForbidden = errors.New("forbidden: this writer may not write that path")

// Variables is the config store. Get returns (nil, 0, nil) for a path that
// does not exist — absence is a value, not an error.
type Variables interface {
	Get(path string) (Items, Index, error)
	Put(path string, items Items, cas Index) (Index, error)
	List(prefix string) ([]string, error)
	Delete(path string, cas Index) error
}

// Str stringifies an item value the way Python's str() did on put.
func Str(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case bool:
		if x {
			return "true"
		}
		return "false"
	case float64:
		return strconv.FormatFloat(x, 'f', -1, 64)
	case int:
		return strconv.Itoa(x)
	case int64:
		return strconv.FormatInt(x, 10)
	}
	return fmt.Sprint(v)
}

// Allowed is the ACL test: path == p, or p ends in "*" and is a prefix.
func Allowed(path string, allowed []string) bool {
	for _, p := range allowed {
		if path == p || (strings.HasSuffix(p, "*") && strings.HasPrefix(path, strings.TrimSuffix(p, "*"))) {
			return true
		}
	}
	return false
}

func safe(path string) (string, error) {
	if strings.Contains(path, "..") || strings.HasPrefix(path, "/") {
		return "", fmt.Errorf("bad path %q", path)
	}
	return path, nil
}

// -- the seam: which store is behind the contract, said as a URL ------------
// A process is told CONFIG_URL and nothing else. `file://` is in-process — on
// a box there is no daemon, no hop and no second quorum, which is the whole
// reason this is a factory and not a service. Every other scheme is registered
// by the package that implements it (М11's cluster package registers
// `nomad://` at init), so the platform names no vendor and adding Kubernetes
// adds a file rather than a branch here.
type VarsFactory func(url, writer string, acl map[string][]string) (Variables, error)

var schemes = map[string]VarsFactory{}

// RegisterScheme: a backend registers itself from its own package's init.
func RegisterScheme(scheme string, factory VarsFactory) { schemes[scheme] = factory }

// OpenVars: `file:///data/platform/config` · `nomad://127.0.0.1:4646` ·
// whatever else registered. A bare path is read as `file://` so the box keeps
// working with no URL at all.
func OpenVars(url, writer string, acl map[string][]string) (Variables, error) {
	scheme, rest, found := strings.Cut(url, "://")
	if !found {
		return openFileVars(url, writer, acl)
	}
	if scheme == "file" {
		if rest == "" {
			rest = "/"
		}
		return openFileVars(rest, writer, acl)
	}
	factory, ok := schemes[scheme]
	if !ok {
		known := []string{"file"}
		for s := range schemes {
			known = append(known, s)
		}
		sort.Strings(known)
		return nil, fmt.Errorf("no Variables backend for %s://  (have: %s)", scheme, strings.Join(known, ", "))
	}
	return factory(url, writer, acl)
}

func openFileVars(root, writer string, acl map[string][]string) (Variables, error) {
	v, err := NewFileVariables(root)
	if err != nil {
		return nil, err
	}
	if writer != "" {
		v.Writer = writer
		for k, a := range acl {
			v.ACL[k] = append([]string(nil), a...)
		}
	}
	return v, nil
}

// FileVariables is a config store with the semantics Nomad Variables promise
// — a raft-assigned ModifyIndex, PUT with cas=<index> succeeding only if the
// index still matches, a conflict otherwise — on one box, as files. One JSON
// file per path under <root>/vars/, one counter file for the index, one lock.
type FileVariables struct {
	Root   string
	dir    string
	Writer string
	ACL    map[string][]string
}

func NewFileVariables(root string) (*FileVariables, error) {
	v := &FileVariables{Root: root, dir: filepath.Join(root, "vars"), ACL: map[string][]string{}}
	return v, os.MkdirAll(v.dir, 0o755)
}

// AsWriter is the same store seen through another identity, allowed only
// these prefixes ("vms/*", "vms/epoch/*") — what a Nomad ACL policy does.
func (v *FileVariables) AsWriter(writer string, allowed ...string) *FileVariables {
	acl := map[string][]string{}
	for k, a := range v.ACL {
		acl[k] = a
	}
	acl[writer] = allowed
	return &FileVariables{Root: v.Root, dir: v.dir, Writer: writer, ACL: acl}
}

func (v *FileVariables) file(path string) (string, error) {
	p, err := safe(path)
	if err != nil {
		return "", err
	}
	return filepath.Join(v.dir, strings.ReplaceAll(p, "/", "%2F")+".json"), nil
}

func (v *FileVariables) lock() (*os.File, error) {
	f, err := os.OpenFile(filepath.Join(v.Root, "lock"), os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		f.Close()
		return nil, err
	}
	return f, nil
}

func (v *FileVariables) nextIndex() (int64, error) {
	idx := filepath.Join(v.Root, "index")
	n := int64(1000)
	if b, err := os.ReadFile(idx); err == nil && len(strings.TrimSpace(string(b))) > 0 {
		n, _ = strconv.ParseInt(strings.TrimSpace(string(b)), 10, 64)
	}
	n++
	if err := os.WriteFile(idx+".tmp", []byte(strconv.FormatInt(n, 10)), 0o644); err != nil {
		return 0, err
	}
	return n, os.Rename(idx+".tmp", idx)
}

type fileVar struct {
	Items map[string]string `json:"items"`
	Index int64             `json:"index"`
}

func (v *FileVariables) Get(path string) (Items, Index, error) {
	p, err := v.file(path)
	if err != nil {
		return nil, Absent, err
	}
	b, err := os.ReadFile(p)
	if errors.Is(err, os.ErrNotExist) {
		return nil, Absent, nil
	}
	if err != nil {
		return nil, Absent, err
	}
	var d fileVar
	if err := json.Unmarshal(b, &d); err != nil {
		return nil, Absent, err
	}
	return Items(d.Items), Idx(strconv.FormatInt(d.Index, 10)), nil
}

func (v *FileVariables) Put(path string, items Items, cas Index) (Index, error) {
	if v.Writer != "" && len(v.ACL) > 0 && !Allowed(path, v.ACL[v.Writer]) {
		return Absent, fmt.Errorf("%w: %s may not write %s", ErrForbidden, v.Writer, path)
	}
	p, err := v.file(path)
	if err != nil {
		return Absent, err
	}
	l, err := v.lock()
	if err != nil {
		return Absent, err
	}
	defer l.Close()
	_, current, err := v.Get(path)
	if err != nil {
		return Absent, err
	}
	if cas.Conditional() && cas != current {
		return Absent, fmt.Errorf("%w: %s: cas=%s but the store is at %s", ErrConflict, path, cas, current)
	}
	n, err := v.nextIndex()
	if err != nil {
		return Absent, err
	}
	if items == nil {
		items = Items{}
	}
	b, _ := json.Marshal(fileVar{Items: items, Index: n})
	if err := os.WriteFile(p+".tmp", b, 0o644); err != nil {
		return Absent, err
	}
	return Idx(strconv.FormatInt(n, 10)), os.Rename(p+".tmp", p)
}

func (v *FileVariables) Delete(path string, cas Index) error {
	p, err := v.file(path)
	if err != nil {
		return err
	}
	l, err := v.lock()
	if err != nil {
		return err
	}
	defer l.Close()
	_, current, err := v.Get(path)
	if err != nil {
		return err
	}
	if cas.Conditional() && cas != current {
		return fmt.Errorf("%w: %s: cas=%s but the store is at %s", ErrConflict, path, cas, current)
	}
	if err := os.Remove(p); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	_, err = v.nextIndex()
	return err
}

func (v *FileVariables) List(prefix string) ([]string, error) {
	ents, err := os.ReadDir(v.dir)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, e := range ents {
		if strings.HasSuffix(e.Name(), ".json") {
			p := strings.ReplaceAll(strings.TrimSuffix(e.Name(), ".json"), "%2F", "/")
			if strings.HasPrefix(p, prefix) {
				out = append(out, p)
			}
		}
	}
	sort.Strings(out)
	return out, nil
}
