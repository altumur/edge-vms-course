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
	// MaxBytes is what one object may weigh here; NoCeiling (0) when the store has none. Declared, not
	// guessed — see limits.go. A write over it returns *TooLarge and leaves the previous object alone.
	MaxBytes() int
	Put(key string, data []byte) error
	Get(key string) ([]byte, error)
	List(prefix string) ([]string, error)

	// Delete removes one object; true if it was there. A capability of the STORE — a store can either
	// delete or it cannot — and deliberately not "delete, but only under blobs/": that would be policy
	// welded into the seam, and policy lives with the caller that has it (SpecController.SweepBlobs) and
	// with the scheduler's ACL, which is the only place that can enforce it.
	Delete(key string) (bool, error)
}

type FsObjectStore struct {
	Root string
	Max  int // what this store says it can hold; a directory has no ceiling worth naming
}

func NewFsObjectStore(root string) (*FsObjectStore, error) {
	return &FsObjectStore{Root: root}, os.MkdirAll(root, 0o755)
}

// NewFsObjectStoreCapped is the same store that DOES declare a ceiling — which is how the platform's own
// limits get exercised on one box, without a cluster.
func NewFsObjectStoreCapped(root string, max int) (*FsObjectStore, error) {
	return &FsObjectStore{Root: root, Max: max}, os.MkdirAll(root, 0o755)
}

func (f *FsObjectStore) MaxBytes() int { return f.Max }

func (f *FsObjectStore) path(key string) (string, error) {
	if strings.Contains(key, "..") || strings.HasPrefix(key, "/") {
		return "", fmt.Errorf("bad key %q", key)
	}
	return filepath.Join(f.Root, key), nil
}

func (f *FsObjectStore) Put(key string, data []byte) error {
	if err := Check(key, len(data), f.Max); err != nil { // refused before the write: the old object survives
		return err
	}
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

// Delete removes the file; a missing key is not an error, so a sweep that runs twice on the same
// candidate — two consoles, a retry — does the same thing the second time.
func (f *FsObjectStore) Delete(key string) (bool, error) {
	p, err := f.path(key)
	if err != nil {
		return false, err
	}
	if err := os.Remove(p); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return false, nil
		}
		return false, err
	}
	return true, nil
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
