// Package testbox is one box in a temp directory: the platform's two
// stores, a spool and an archive, a clock. No GStreamer — the actuator is
// the fake. Shared by the М10 and М11 suites.
package testbox

import (
	"os"
	"path/filepath"
	"sync"

	p "vmsserver/vmsplatform"
)

// Clock is a clock the tests own.
type Clock struct {
	mu sync.Mutex
	T  float64
}

func NewClock(t float64) *Clock { return &Clock{T: t} }

func (c *Clock) Now() float64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.T
}

func (c *Clock) Advance(s float64) {
	c.mu.Lock()
	c.T += s
	c.mu.Unlock()
}

func (c *Clock) Set(t float64) {
	c.mu.Lock()
	c.T = t
	c.mu.Unlock()
}

type Box struct {
	Root    string
	Vars    *p.FileVariables
	Objects *p.FsObjectStore
	Spool   string
	Archive string
	Clock   *Clock
	Wall    *Clock
}

func NewBox() *Box {
	root, _ := os.MkdirTemp("", "vmsserver-")
	vars, _ := p.NewFileVariables(filepath.Join(root, "config"))
	objects, _ := p.NewFsObjectStore(filepath.Join(root, "objects"))
	return &Box{root, vars, objects, filepath.Join(root, "spool"), filepath.Join(root, "archive"), NewClock(1000), NewClock(1_757_500_000)}
}
