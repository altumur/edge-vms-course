package cluster

// The cluster's object store — М10's vmsplatform.ObjectStore contract, which
// holds three small things: worker heartbeats, resource heartbeats, and the
// snapshot the domain's read model is built from.
//
// On a cluster of this size the implementation is VariablesObjectStore:
// objects as Nomad Variables under objects/…. A heartbeat is ~10 KB every
// ten seconds from a dozen workers and three resources — a couple of raft
// writes a second — and it removes a whole store (MinIO, its quorum, its
// credentials) from the cluster. When a cluster grows to where its
// heartbeats are a raft load, OpenStore("s3+http://…") is the same three
// calls against MinIO or S3 (s3.go). Footage never goes to any of these.

import (
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"time"

	p "vmsserver/vmsplatform"
)

type ObjectStore = p.ObjectStore

// FsObjectStore is М10's: a directory — the tests, and a bench with a shared mount.
func NewFsObjectStore(root string) (*p.FsObjectStore, error) { return p.NewFsObjectStore(root) }

// HttpObjectStore PUTs and GETs against any endpoint with plain HTTP object
// semantics (MinIO with a bucket policy, nginx with dav). Plain HTTP has no
// listing.
type HttpObjectStore struct {
	Base   string
	Client *http.Client
}

func NewHttpObjectStore(base string) *HttpObjectStore {
	return &HttpObjectStore{Base: strings.TrimRight(base, "/"), Client: &http.Client{Timeout: 10 * time.Second}}
}

func (h *HttpObjectStore) Put(key string, data []byte) error {
	req, err := http.NewRequest("PUT", h.Base+"/"+key, strings.NewReader(string(data)))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/octet-stream")
	resp, err := h.Client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 && resp.StatusCode != 201 && resp.StatusCode != 204 {
		return fmt.Errorf("PUT %s: HTTP %d", key, resp.StatusCode)
	}
	return nil
}

func (h *HttpObjectStore) Get(key string) ([]byte, error) {
	resp, err := h.Client.Get(h.Base + "/" + key)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == 404 {
		return nil, nil
	}
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("GET %s: HTTP %d", key, resp.StatusCode)
	}
	return io.ReadAll(resp.Body)
}

func (h *HttpObjectStore) List(prefix string) ([]string, error) {
	return nil, fmt.Errorf("plain HTTP has no listing; use s3+http:// or variables:// for the heartbeat prefix")
}

// VariablesObjectStore: objects as Variables: <prefix>/<key> -> {data: <utf-8 text>}.
// The store is whatever Variables the caller holds, with the ACL that comes
// with its token.
type VariablesObjectStore struct {
	Vars   Variables
	Prefix string
}

func NewVariablesObjectStore(vars Variables, prefix string) *VariablesObjectStore {
	if prefix == "" {
		prefix = "objects"
	}
	return &VariablesObjectStore{vars, strings.Trim(prefix, "/")}
}

func (v *VariablesObjectStore) path(key string) (string, error) {
	if strings.Contains(key, "..") || strings.HasPrefix(key, "/") {
		return "", fmt.Errorf("bad key %q", key)
	}
	return v.Prefix + "/" + key, nil
}

func (v *VariablesObjectStore) Put(key string, data []byte) error {
	pth, err := v.path(key)
	if err != nil {
		return err
	}
	_, err = v.Vars.Put(pth, Items{"data": string(data)}, NoCAS) // no cas: the last heartbeat wins, as it should
	return err
}

func (v *VariablesObjectStore) Get(key string) ([]byte, error) {
	pth, err := v.path(key)
	if err != nil {
		return nil, err
	}
	items, _, err := v.Vars.Get(pth)
	if err != nil || items == nil {
		return nil, err
	}
	d, ok := items["data"]
	if !ok {
		return nil, nil
	}
	return []byte(d), nil
}

func (v *VariablesObjectStore) List(prefix string) ([]string, error) {
	base := v.Prefix + "/"
	paths, err := v.Vars.List(base + prefix)
	if err != nil {
		return nil, err
	}
	out := []string{}
	for _, pth := range paths {
		out = append(out, strings.TrimPrefix(pth, base))
	}
	sort.Strings(out)
	return out, nil
}

func (v *VariablesObjectStore) Delete(key string) error {
	pth, err := v.path(key)
	if err != nil {
		return err
	}
	return v.Vars.Delete(pth, NoCAS)
}

// OpenStore: variables://objects (the default on this cluster) · file:///path ·
// http(s)://host/bucket (anonymous) · s3+http(s)://host/bucket?region=r
// (SigV4, credentials from AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY — on a
// server, from a Variable).
func OpenStore(raw string) (ObjectStore, error) {
	switch {
	case strings.HasPrefix(raw, "variables://"):
		return NewVariablesObjectStore(NewNomadVariables(), strings.TrimPrefix(raw, "variables://")), nil
	case strings.HasPrefix(raw, "s3+http://"), strings.HasPrefix(raw, "s3+https://"):
		u, err := url.Parse(raw[3:])
		if err != nil {
			return nil, err
		}
		region := u.Query().Get("region")
		if region == "" {
			region = "us-east-1"
		}
		return NewS3ObjectStore(u.Scheme+"://"+u.Host, strings.Trim(u.Path, "/"), region,
			os.Getenv("AWS_ACCESS_KEY_ID"), os.Getenv("AWS_SECRET_ACCESS_KEY"))
	case strings.HasPrefix(raw, "http://"), strings.HasPrefix(raw, "https://"):
		return NewHttpObjectStore(raw), nil
	case strings.HasPrefix(raw, "file://"):
		return p.NewFsObjectStore(strings.TrimPrefix(raw, "file://"))
	}
	return p.NewFsObjectStore(raw)
}
