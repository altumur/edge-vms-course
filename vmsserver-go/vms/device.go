package vms

// What DriverPack connects to, without DriverPack: N channels, an archive of
// its own, a session budget. A camera with an SD card is a device with one
// channel; an NVR is a device with thirty-two. The real one is a DriverPack
// session; the fake below is what the tests hold.
//
// The point of the type is that ONE session serves every channel assigned from
// it. An NVR with thirty-two cameras is one connection, not thirty-two — the
// same argument as "one connection to the camera" (Lesson 4), a level up.

import (
	"errors"
	"fmt"
	"sort"
	"sync"
)

// ErrDeviceBusy: the device's own ceiling on concurrent playbacks is reached.
// It belongs to the hardware, not to the worker — the worker's capacity is
// still counted in cameras. The door turns this into a 503, which is the same
// admission control the live gateway does for viewers (Lesson 13), one floor
// down.
var ErrDeviceBusy = errors.New("all of this device's playback sessions are in use")

// ErrNoDeviceArchive: this camera has no archive of its own here.
var ErrNoDeviceArchive = errors.New("this camera has no archive of its own here")

// ErrNoIndex: this driver cannot list what the device holds — which is not "the device holds nothing".
var ErrNoIndex = errors.New("this driver cannot list what the device holds")

// Coverage is the SUMMARY a device reports for one channel — never the index.
// Drawing a timeline must not cost a playback session, and on a device that
// allows two of them, it must not cost a request either.
type Coverage struct {
	From      float64 `json:"from"`
	To        float64 `json:"to"`
	Fragments int     `json:"fragments"`
}

func (c Coverage) ToMap() map[string]any {
	return map[string]any{"from": c.From, "to": c.To, "fragments": c.Fragments}
}

// Device is a held device: its channels, its own footage, and how many
// playbacks it allows at once.
type Device interface {
	Channels() []string
	Coverage(cam string) (Coverage, bool)
	InUse() int
	MaxPlaybacks() int
	OpenPlayback(cam string, t0, t1 float64) (string, error)
	Read(sid string) ([]byte, error)
	ClosePlayback(sid string)
	Close()
}

// Lister is the device's INDEX: what it actually holds, span by span. A second interface and not a method
// of Device, because not every driver can list — and "this driver cannot list" is a different answer from
// "the device holds nothing here". A card recording continuously has one span and its summary says
// everything; an NVR recording on motion has hundreds, and between its `from` and its `to` there is mostly
// nothing. Without this a scan is promised minutes that do not exist.
type Lister interface {
	// Recordings: the spans clipped to [t0, t1). ok=false: this channel has no index to read.
	Recordings(cam string, t0, t1 float64) (spans [][2]float64, ok bool)
}

// FakeDevice is what the tests hold where a box holds a DriverPack session.
type FakeDevice struct {
	mu       sync.Mutex
	Key      string
	Chans    []string
	Cov      map[string]Coverage
	Index    map[string][][2]float64 // camera -> spans; a camera absent here cannot be listed
	MaxPlays int
	Bps      int
	open     map[string][3]any // sid -> (cam, t0, t1)
	Fetched  [][3]any
}

func NewFakeDevice(key string, channels []string, coverage map[string]Coverage) *FakeDevice {
	return &FakeDevice{Key: key, Chans: channels, Cov: coverage, MaxPlays: 2, Bps: 1, open: map[string][3]any{}}
}

func (d *FakeDevice) Channels() []string { return append([]string(nil), d.Chans...) }

func (d *FakeDevice) Coverage(cam string) (Coverage, bool) {
	c, ok := d.Cov[cam]
	return c, ok
}

func (d *FakeDevice) Recordings(cam string, t0, t1 float64) ([][2]float64, bool) {
	spans, ok := d.Index[cam]
	if !ok {
		return nil, false
	}
	out := [][2]float64{}
	for _, s := range spans {
		if s[1] > t0 && s[0] < t1 {
			out = append(out, [2]float64{max(s[0], t0), min(s[1], t1)})
		}
	}
	return out, true
}

func (d *FakeDevice) InUse() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return len(d.open)
}

func (d *FakeDevice) MaxPlaybacks() int {
	if d.MaxPlays == 0 {
		return 2
	}
	return d.MaxPlays
}

func (d *FakeDevice) OpenPlayback(cam string, t0, t1 float64) (string, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if len(d.open) >= d.MaxPlaybacks() {
		return "", fmt.Errorf("device %s: %d playback sessions, all in use: %w", d.Key, d.MaxPlaybacks(), ErrDeviceBusy)
	}
	sid := fmt.Sprintf("%s:%v:%d:%d", cam, t0, len(d.Fetched), len(d.open))
	d.open[sid] = [3]any{cam, t0, t1}
	return sid, nil
}

func (d *FakeDevice) Read(sid string) ([]byte, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	s, ok := d.open[sid]
	if !ok {
		return nil, fmt.Errorf("no such playback session: %s", sid)
	}
	d.Fetched = append(d.Fetched, s)
	n := int((s[2].(float64) - s[1].(float64)) * float64(d.Bps))
	if n < 1 {
		n = 1
	}
	return make([]byte, n), nil
}

func (d *FakeDevice) ClosePlayback(sid string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	delete(d.open, sid)
}

func (d *FakeDevice) Close() {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.open = map[string][3]any{}
}

// FakeDevices is a factory over a fixed set, for the tests: the keys are what
// DeviceOf() produces from a source.
func FakeDevices(devices map[string]*FakeDevice) func(string) Device {
	return func(key string) Device {
		if d, ok := devices[key]; ok {
			return d
		}
		return nil // a source with no archive of its own (a file): nothing to hold
	}
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
