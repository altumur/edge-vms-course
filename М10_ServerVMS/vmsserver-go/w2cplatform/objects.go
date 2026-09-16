package w2cplatform

// An object store: large, or frequent, never queried by key. A directory
// here; Variables-as-objects or S3 in М11. An object appears whole or not at all.

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// ObjectStore: Get returns (nil, nil) for an object that does not exist.
type ObjectStore interface {
	Put(key string, data []byte) error
	Get(key string) ([]byte, error)
	List(prefix string) ([]string, error)
}

type FsObjectStore struct{ Root string }

func NewFsObjectStore(root string) (*FsObjectStore, error) {
	return &FsObjectStore{root}, os.MkdirAll(root, 0o755)
}

func (f *FsObjectStore) path(key string) (string, error) {
	if strings.Contains(key, "..") || strings.HasPrefix(key, "/") {
		return "", fmt.Errorf("bad key %q", key)
	}
	return filepath.Join(f.Root, key), nil
}

func (f *FsObjectStore) Put(key string, data []byte) error {
	p, err := f.path(key)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(p+".tmp", data, 0o644); err != nil {
		return err
	}
	return os.Rename(p+".tmp", p)
}

func (f *FsObjectStore) Get(key string) ([]byte, error) {
	p, err := f.path(key)
	if err != nil {
		return nil, err
	}
	b, err := os.ReadFile(p)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	return b, err
}

func (f *FsObjectStore) List(prefix string) ([]string, error) {
	var out []string
	err := filepath.WalkDir(f.Root, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || strings.HasSuffix(p, ".tmp") {
			return nil
		}
		rel, _ := filepath.Rel(f.Root, p)
		rel = filepath.ToSlash(rel)
		if strings.HasPrefix(rel, prefix) {
			out = append(out, rel)
		}
		return nil
	})
	sort.Strings(out)
	return out, err
}
