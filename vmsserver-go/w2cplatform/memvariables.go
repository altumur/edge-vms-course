package w2cplatform

// The config store in memory, with the same contract as the file one — and therefore right exactly when
// the contract suite is green against it:
//
//	CONTRACT_URL=memory:// go test ./w2cplatform -run Contract
//
// Lesson 3 said the store is a URL and nothing in the platform may know which backend answers it. That is
// a claim about design, and a design claim with one implementation is a hope. This is the second
// implementation: the same seven clauses, in memory, in a hundred lines.
//
// It is not a mock and it is not test scaffolding. It is registered on the same seam file:// is, chosen
// the same way (CONFIG_URL=memory://), and held to the same standard. What it is FOR: a dev box where
// nothing should survive a restart; a test that wants the real store and not a fake of it; and М11, where
// FakeVariables is this same thing with raft's answers — which is the point. A fake that implements a
// contract is a backend.
//
// A URL names a store, so two opens of one URL must be one store, as two opens of file://<dir> are:
//
//	memory://<name>   one store per name, per process. Three identities in one process — a controller, a
//	                  console and a worker in one dev binary — open the same name and see each other's
//	                  writes: sharing an address space is not sharing a writer, and the ACL still holds.
//	memory://         no name, so nothing to share by: a fresh private store on every open. That is what
//	                  the contract suite needs, since every clause starts from an empty store.
//
// Named stores live for the life of the process and are never collected. For a dev box and a test that is
// the whole of the requirement; anything that needs them collected wanted file://.

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
	"sync"
)

var (
	namedMu  sync.Mutex
	namedMem = map[string]*memState{}
)

func init() {
	// The seam takes a URL and an identity, never a class name.
	RegisterScheme("memory", func(url, writer string, acl map[string][]string) (Variables, error) {
		v := NewMemVariables()
		// `memory://<name>?max_bytes=<n>`: the ceiling is part of WHICH STORE THIS IS, so it belongs in
		// the URL beside the name, exactly as the backend itself does (Lesson 20).
		rest := strings.TrimPrefix(url, "memory://")
		name, query, _ := strings.Cut(rest, "?")
		for _, part := range strings.Split(query, "&") {
			if k, val, ok := strings.Cut(part, "="); ok && k == "max_bytes" {
				if n, err := strconv.Atoi(val); err == nil {
					v.Max = n
				}
			}
		}
		if name != "" {
			namedMu.Lock()
			if st, ok := namedMem[name]; ok {
				v.s = st
			} else {
				namedMem[name] = v.s
			}
			namedMu.Unlock()
		}
		if writer == "" {
			return v, nil
		}
		return v.AsWriter(writer, acl[writer]...), nil
	})
}

// MemVariables is one identity over a shared state, exactly as FileVariables is one identity over a
// directory.
type MemVariables struct {
	s      *memState
	Writer string // "who am I" for the ACL check; "" bypasses it
	Max    int    // a map in memory has no ceiling — but `memory://x?max_bytes=512` gives the contract
	//               suite a store that DOES, which is how clause 8 is exercised against something
	//               other than prose
}

func (m *MemVariables) MaxBytes() int { return m.Max }

type memEntry struct {
	items Items
	index int64
}

type memState struct {
	mu    sync.Mutex
	index int64
	items map[string]memEntry
	acl   map[string][]string
}

// NewMemVariables: an empty store. The index starts at 1000 and only goes up, as the file backend's
// counter does — so a CAS from before a restart still conflicts instead of quietly matching.
func NewMemVariables() *MemVariables {
	return &MemVariables{s: &memState{index: 1000, items: map[string]memEntry{}, acl: map[string][]string{}}}
}

// AsWriter is the same store seen through another identity, allowed only these prefixes.
func (m *MemVariables) AsWriter(writer string, allowed ...string) *MemVariables {
	m.s.mu.Lock()
	if allowed != nil {
		m.s.acl[writer] = allowed
	}
	m.s.mu.Unlock()
	return &MemVariables{s: m.s, Writer: writer, Max: m.Max}
}

func (m *MemVariables) refuse(path string) error {
	if m.Writer == "" || len(m.s.acl) == 0 {
		return nil
	}
	if !Allowed(path, m.s.acl[m.Writer]) {
		return fmt.Errorf("%w: %s may not write %s", ErrForbidden, m.Writer, path)
	}
	return nil
}

// indexOf, held under the lock: Absent for a path that is not there.
func (m *memState) indexOf(path string) Index {
	e, ok := m.items[path]
	if !ok {
		return Absent
	}
	return Idx(strconv.FormatInt(e.index, 10))
}

// Get returns a COPY of the row — contract clause 7. A caller that mutated what it read would otherwise
// have edited the store with no index: no CAS, no conflict, no version, and the next reader sees a change
// nobody can point at. The file backend gets this for free by parsing JSON every time; here it is a
// decision, and the suite is what keeps it made.
func (m *MemVariables) Get(path string) (Items, Index, error) {
	if _, err := safe(path); err != nil {
		return nil, Absent, err
	}
	m.s.mu.Lock()
	defer m.s.mu.Unlock()
	e, ok := m.s.items[path]
	if !ok {
		return nil, Absent, nil
	}
	out := make(Items, len(e.items))
	for k, v := range e.items {
		out[k] = v
	}
	return out, Idx(strconv.FormatInt(e.index, 10)), nil
}

func (m *MemVariables) Put(path string, items Items, cas Index) (Index, error) {
	if _, err := safe(path); err != nil {
		return Absent, err
	}
	if err := Check(path, ItemsBytes(items), m.Max); err != nil {
		return Absent, err
	}
	m.s.mu.Lock()
	defer m.s.mu.Unlock()
	if err := m.refuse(path); err != nil {
		return Absent, err
	}
	current := m.s.indexOf(path)
	if cas.Conditional() && cas != current {
		return Absent, fmt.Errorf("%w: %s: cas=%s but the store is at %s", ErrConflict, path, cas, current)
	}
	m.s.index++
	cp := make(Items, len(items)) // the other half of clause 7: put is given a row and must not go on sharing it
	for k, v := range items {
		cp[k] = v
	}
	m.s.items[path] = memEntry{cp, m.s.index}
	return Idx(strconv.FormatInt(m.s.index, 10)), nil
}

func (m *MemVariables) List(prefix string) ([]string, error) {
	m.s.mu.Lock()
	defer m.s.mu.Unlock()
	out := []string{}
	for p := range m.s.items {
		if strings.HasPrefix(p, prefix) {
			out = append(out, p)
		}
	}
	sort.Strings(out)
	return out, nil
}

// Delete: the same CAS check as Put and — as on the file backend — no ACL check. A delete burns an index
// too, for the same reason a write does.
func (m *MemVariables) Delete(path string, cas Index) error {
	if _, err := safe(path); err != nil {
		return err
	}
	m.s.mu.Lock()
	defer m.s.mu.Unlock()
	current := m.s.indexOf(path)
	if cas.Conditional() && cas != current {
		return fmt.Errorf("%w: %s: cas=%s but the store is at %s", ErrConflict, path, cas, current)
	}
	delete(m.s.items, path)
	m.s.index++
	return nil
}
