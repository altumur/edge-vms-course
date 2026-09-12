// Package cluster is ClusterVMS — М11 — in Go: what a recorder needs to outlive
// its server. Identity from a Nomad Variable, the epoch by check-and-set,
// configuration published upward object-first, the restore, a lease that
// fences the zombie, the cluster directory, placement by measured capacity,
// and the sweep that turns a fenced instance's files back into rows.
//
// Standard library only, like the Python version: the Variables client is
// net/http against Nomad's API (github.com/hashicorp/nomad/api would do the
// same in more code and an MPL-2.0 dependency), the object store speaks
// SigV4 by hand, and Postgres is behind an interface.
package cluster

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

// Items is one Variable's payload. Nomad stores strings; so do we.
type Items map[string]string

// NoCAS asks for an unconditional write. Every write that matters passes
// the ModifyIndex it read instead.
const NoCAS int64 = -1

// ErrConflict is HTTP 409: the cas index did not match the current ModifyIndex.
var ErrConflict = errors.New("cas conflict: the ModifyIndex moved")

// ErrForbidden is HTTP 403: this token may not write that path — one writer per key.
var ErrForbidden = errors.New("forbidden: this writer may not write that path")

// Variables is the cluster's small, consistent store. Get returns (nil, 0,
// nil) for a path that does not exist — absence is a value, not an error.
type Variables interface {
	Get(path string) (Items, int64, error)
	Put(path string, items Items, cas int64) (int64, error)
	List(prefix string) ([]string, error)
}

// NomadVariables speaks the HTTP API with the task's own workload-identity
// token (NOMAD_TOKEN).
type NomadVariables struct {
	Addr      string
	Token     string
	Namespace string
	Client    *http.Client
}

func NewNomadVariables() *NomadVariables {
	addr := os.Getenv("NOMAD_ADDR")
	if addr == "" {
		addr = "http://127.0.0.1:4646"
	}
	return &NomadVariables{Addr: strings.TrimRight(addr, "/"), Token: os.Getenv("NOMAD_TOKEN"),
		Namespace: "default", Client: &http.Client{Timeout: 5 * time.Second}}
}

type nomadVar struct {
	Path        string            `json:"Path"`
	Items       map[string]string `json:"Items"`
	ModifyIndex int64             `json:"ModifyIndex"`
}

func (n *NomadVariables) do(method, u string, body any) (int, []byte, error) {
	var rd io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, u, rd)
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("X-Nomad-Token", n.Token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := n.Client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	switch resp.StatusCode {
	case 409:
		return 409, raw, ErrConflict
	case 403:
		return 403, raw, ErrForbidden
	case 404:
		return 404, nil, nil
	}
	if resp.StatusCode >= 300 {
		return resp.StatusCode, raw, fmt.Errorf("%s %s: HTTP %d: %.200s", method, u, resp.StatusCode, raw)
	}
	return resp.StatusCode, raw, nil
}

func (n *NomadVariables) Get(path string) (Items, int64, error) {
	status, raw, err := n.do("GET", fmt.Sprintf("%s/v1/var/%s?namespace=%s", n.Addr, path, n.Namespace), nil)
	if err != nil || status == 404 || len(raw) == 0 {
		return nil, 0, err
	}
	var v nomadVar
	if err := json.Unmarshal(raw, &v); err != nil {
		return nil, 0, err
	}
	return Items(v.Items), v.ModifyIndex, nil
}

func (n *NomadVariables) Put(path string, items Items, cas int64) (int64, error) {
	q := "namespace=" + n.Namespace
	if cas != NoCAS {
		q += fmt.Sprintf("&cas=%d", cas)
	}
	_, raw, err := n.do("PUT", fmt.Sprintf("%s/v1/var/%s?%s", n.Addr, path, q), map[string]any{"Items": items})
	if err != nil {
		return 0, err
	}
	var v nomadVar
	if err := json.Unmarshal(raw, &v); err != nil {
		return 0, err
	}
	return v.ModifyIndex, nil
}

func (n *NomadVariables) List(prefix string) ([]string, error) {
	_, raw, err := n.do("GET", fmt.Sprintf("%s/v1/vars?prefix=%s&namespace=%s", n.Addr, url.QueryEscape(prefix), n.Namespace), nil)
	if err != nil {
		return nil, err
	}
	var vs []nomadVar
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &vs); err != nil {
			return nil, err
		}
	}
	out := make([]string, 0, len(vs))
	for _, v := range vs {
		out = append(out, v.Path)
	}
	return out, nil
}

// FakeVariables is one raft log for the whole cluster, in memory, with the
// semantics the docs promise: a raft-assigned ModifyIndex, PUT ?cas=<index>
// succeeding only if the index still matches, a conflict otherwise. Optional
// ACL: a writer may only put under the prefixes it was granted. Nothing in
// the fake is Nomad; everything in it is what Nomad promises, and the tests
// run against it in microseconds.
type FakeVariables struct {
	s      *fakeState
	Writer string // "who am I" for the ACL check; "" bypasses it
}

type fakeState struct {
	mu        sync.Mutex
	raftIndex int64
	items     map[string]fakeEntry
	acl       map[string][]string
}

type fakeEntry struct {
	items Items
	index int64
}

func NewFakeVariables() *FakeVariables {
	return &FakeVariables{s: &fakeState{raftIndex: 1000, items: map[string]fakeEntry{}, acl: map[string][]string{}}}
}

// SetACL grants a writer id its prefixes ("nodes/node-3", "nodes/node-3/*").
func (f *FakeVariables) SetACL(writer string, allowed ...string) { f.s.acl[writer] = allowed }

// AsWriter is the same raft seen through another token.
func (f *FakeVariables) AsWriter(writer string) *FakeVariables {
	return &FakeVariables{s: f.s, Writer: writer}
}

func (f *FakeVariables) Get(path string) (Items, int64, error) {
	f.s.mu.Lock()
	defer f.s.mu.Unlock()
	e, ok := f.s.items[path]
	if !ok {
		return nil, 0, nil
	}
	out := make(Items, len(e.items))
	for k, v := range e.items {
		out[k] = v
	}
	return out, e.index, nil
}

func (f *FakeVariables) Put(path string, items Items, cas int64) (int64, error) {
	if f.Writer != "" && len(f.s.acl) > 0 {
		ok := false
		for _, p := range f.s.acl[f.Writer] {
			if path == p || (strings.HasSuffix(p, "*") && strings.HasPrefix(path, strings.TrimSuffix(p, "*"))) {
				ok = true
			}
		}
		if !ok {
			return 0, fmt.Errorf("%w: %s may not write %s", ErrForbidden, f.Writer, path)
		}
	}
	f.s.mu.Lock()
	defer f.s.mu.Unlock()
	current := f.s.items[path].index
	if cas != NoCAS && cas != current {
		return 0, fmt.Errorf("%w: cas=%d but ModifyIndex=%d", ErrConflict, cas, current)
	}
	f.s.raftIndex++
	cp := make(Items, len(items))
	for k, v := range items {
		cp[k] = v
	}
	f.s.items[path] = fakeEntry{cp, f.s.raftIndex}
	return f.s.raftIndex, nil
}

func (f *FakeVariables) List(prefix string) ([]string, error) {
	f.s.mu.Lock()
	defer f.s.mu.Unlock()
	var out []string
	for p := range f.s.items {
		if strings.HasPrefix(p, prefix) {
			out = append(out, p)
		}
	}
	sort.Strings(out)
	return out, nil
}
