package psimplatform

// The fencing token and the lease — generic to any writer that can have two
// instances. The subsystem decides what key the epoch goes in; the platform
// only promises that it comes from one issuer and increases.

import (
	"errors"
	"fmt"
	"strconv"
	"time"
)

// Clock is a source of seconds — monotonic for leases, wall for heartbeats.
type Clock func() float64

// Monotonic is the process's monotonic clock in seconds.
func Monotonic() Clock {
	start := time.Now()
	return func() float64 { return time.Since(start).Seconds() }
}

// Wall is the wall clock in unix seconds.
func Wall() Clock { return func() float64 { return float64(time.Now().UnixNano()) / 1e9 } }

// NextEpoch issues the next epoch for key by check-and-set: two callers
// racing get two different numbers, in order.
func NextEpoch(vars Variables, key string) (int, int64, error) {
	for i := 0; i < 200; i++ {
		items, idx, err := vars.Get(key)
		if err != nil {
			return 0, 0, err
		}
		current := 0
		if items != nil {
			current, _ = strconv.Atoi(items["epoch"])
		}
		newIdx, err := vars.Put(key, Items{"epoch": strconv.Itoa(current + 1)}, idx)
		if errors.Is(err, ErrConflict) {
			continue
		}
		if err != nil {
			return 0, 0, err
		}
		return current + 1, newIdx, nil
	}
	return 0, 0, fmt.Errorf("could not issue an epoch for %s after 200 conflicts", key)
}

func CurrentEpoch(vars Variables, key string) (int, error) {
	items, _, err := vars.Get(key)
	if err != nil || items == nil {
		return 0, err
	}
	n, _ := strconv.Atoi(items["epoch"])
	return n, nil
}

// Lease: MayWrite while now − last renewal < TTL − margin, on a monotonic
// clock; Renew = read the key and find it still mine.
type Lease struct {
	Vars        Variables
	Key         string
	Epoch       int
	TTL, Margin float64
	Clock       Clock
	LastRenewal float64
	Fenced      bool
	Conflicts   int
}

func NewLease(vars Variables, key string, epoch int, ttl, margin float64, clock Clock) *Lease {
	return &Lease{Vars: vars, Key: key, Epoch: epoch, TTL: ttl, Margin: margin, Clock: clock, LastRenewal: clock()}
}

func (l *Lease) Renew() bool {
	if l.Fenced {
		return false
	}
	live, err := CurrentEpoch(l.Vars, l.Key)
	if err != nil { // the store is unreachable; keep going until TTL − margin
		return l.MayWrite()
	}
	if live != l.Epoch {
		l.Fenced = true
		l.Conflicts++
		return false
	}
	l.LastRenewal = l.Clock()
	return true
}

func (l *Lease) MayWrite() bool {
	return !l.Fenced && (l.Clock()-l.LastRenewal) < (l.TTL-l.Margin)
}

func (l *Lease) SecondsLeft() float64 {
	left := (l.TTL - l.Margin) - (l.Clock() - l.LastRenewal)
	if left < 0 {
		return 0
	}
	return left
}
