package cluster

// The cluster's object store — large, rare, never queried: the restore
// point (and, since the heartbeat left raft, the heartbeat).
//
// Three adapters with one contract. HttpObjectStore PUTs and GETs against
// any endpoint with plain HTTP object semantics (MinIO with a bucket
// policy, nginx with dav). FsObjectStore is a directory — the tests, and a
// bench with a shared mount. S3ObjectStore (s3.go) signs.

import (
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// ObjectStore: Get returns (nil, nil) for an object that does not exist.
type ObjectStore interface {
	Put(key string, data []byte) error
	Get(key string) ([]byte, error)
}

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

type FsObjectStore struct{ Root string }

func NewFsObjectStore(root string) (*FsObjectStore, error) {
	return &FsObjectStore{root}, os.MkdirAll(root, 0o755)
}

func (f *FsObjectStore) Put(key string, data []byte) error {
	p := filepath.Join(f.Root, key)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(p+".tmp", data, 0o644); err != nil {
		return err
	}
	return os.Rename(p+".tmp", p) // an object appears whole or not at all
}

func (f *FsObjectStore) Get(key string) ([]byte, error) {
	b, err := os.ReadFile(filepath.Join(f.Root, key))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	return b, err
}

// OpenStore: file:///path · http(s)://host/bucket (anonymous) ·
// s3+http(s)://host/bucket?region=r (SigV4, credentials from
// AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY — on a recorder, from its Variable).
func OpenStore(raw string) (ObjectStore, error) {
	switch {
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
		return NewFsObjectStore(strings.TrimPrefix(raw, "file://"))
	}
	return NewFsObjectStore(raw)
}
