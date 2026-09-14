// Package cluster is ClusterVMS — М11 — in Go: М10's platform shape across
// several servers. Built ON М10's vmsserver module (imported, not copied):
// the same psimplatform contract and the same vms controller, worker and
// archive resource. This package supplies what a cluster adds and nothing else:
//
//	variables.go    Nomad Variables over HTTP (ModifyIndex, cas) — and the fake with the promised semantics
//	objectstore.go  objects as Variables (this cluster's choice), HTTP, a directory, S3 (s3.go)
//	worker.go       the worker as an allocation: a slot from NOMAD_ALLOC_INDEX, labels from the server
//	controller.go   the controller as a job: placement under label constraints; the snapshot for М12
//	resource.go     the archive resource as a system job: the VMS's routes on the platform's server
//	timeline.go     one camera across two resources; *unavailable*, never *lost*
//	directory.go    where is camera 7 — one scan of vms/workers/*
//	console.go      the cluster console, standard library
//
// Standard library only: the Variables client is net/http against Nomad's
// API (github.com/hashicorp/nomad/api would do the same in more code and an
// MPL-2.0 dependency).
package cluster

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	p "vmsserver/psimplatform"
)

// The platform's names, re-exported so a cluster program imports one package.
type (
	Items     = p.Items
	Variables = p.Variables
)

var (
	ErrConflict  = p.ErrConflict
	ErrForbidden = p.ErrForbidden
)

const NoCAS = p.NoCAS

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
		return 409, raw, fmt.Errorf("%w: %.200s", ErrConflict, raw)
	case 403:
		return 403, raw, fmt.Errorf("%w: %s", ErrForbidden, u)
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

func (n *NomadVariables) query(cas int64) string {
	q := "namespace=" + n.Namespace
	if cas != NoCAS {
		q += fmt.Sprintf("&cas=%d", cas)
	}
	return q
}

func (n *NomadVariables) Put(path string, items Items, cas int64) (int64, error) {
	_, raw, err := n.do("PUT", fmt.Sprintf("%s/v1/var/%s?%s", n.Addr, path, n.query(cas)), map[string]any{"Items": items})
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
	sort.Strings(out)
	return out, nil
}

func (n *NomadVariables) Delete(path string, cas int64) error {
	_, _, err := n.do("DELETE", fmt.Sprintf("%s/v1/var/%s?%s", n.Addr, path, n.query(cas)), nil)
	return err
}

// FakeVariables is one raft log for the whole cluster, in memory, with the
// semantics the docs promise: a raft-assigned ModifyIndex, PUT ?cas=<index>
// succeeding only if the index still matches, a conflict otherwise. Optional
// ACL: a writer may only put under the prefixes it was granted. Nothing in
// the fake is Nomad; everything in it is what Nomad promises.
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

// AsWriter is the same raft seen through one identity — what a task's
// workload identity token is under a Nomad ACL policy. With prefixes given,
// they become that writer's grant.
func (f *FakeVariables) AsWriter(writer string, allowed ...string) *FakeVariables {
	f.s.mu.Lock()
	if allowed != nil {
		f.s.acl[writer] = allowed
	}
	f.s.mu.Unlock()
	return &FakeVariables{s: f.s, Writer: writer}
}

func (f *FakeVariables) acl(path string) error {
	if f.Writer == "" || len(f.s.acl) == 0 {
		return nil
	}
	if !p.Allowed(path, f.s.acl[f.Writer]) {
		return fmt.Errorf("%w: %s may not write %s", ErrForbidden, f.Writer, path)
	}
	return nil
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
	f.s.mu.Lock()
	defer f.s.mu.Unlock()
	if err := f.acl(path); err != nil {
		return 0, err
	}
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
	out := []string{}
	for pth := range f.s.items {
		if strings.HasPrefix(pth, prefix) {
			out = append(out, pth)
		}
	}
	sort.Strings(out)
	return out, nil
}

func (f *FakeVariables) Delete(path string, cas int64) error {
	f.s.mu.Lock()
	defer f.s.mu.Unlock()
	if err := f.acl(path); err != nil {
		return err
	}
	current := f.s.items[path].index
	if cas != NoCAS && cas != current {
		return fmt.Errorf("%w: cas=%d but ModifyIndex=%d", ErrConflict, cas, current)
	}
	delete(f.s.items, path)
	f.s.raftIndex++
	return nil
}
