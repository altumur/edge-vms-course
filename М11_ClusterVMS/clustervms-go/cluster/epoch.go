package cluster

// Lesson 3 — the epoch by check-and-set, and the lease that fences.
//
// The epoch is a fencing token: it must come from ONE issuer and it must
// INCREASE. A Nomad Variable with cas is both. A variable lock is neither
// (its ID is an opaque UUID). A database sequence reissues numbers after a
// restore. The archive checks the token by construction: the epoch is in
// the path, so a stale instance cannot name the live files.
//
// The lease is what makes the stale instance STOP, on a monotonic clock:
//
//	may write while  now − last_renewal < TTL − margin
//
// Renewal is: read my own epoch Variable and find it still mine. A different
// epoch there means a replacement was issued one; that is the moment the
// zombie learns what it is, and node_epoch_conflicts is the counter.

import (
	"errors"
	"fmt"
	"strconv"
	"time"
)

func epochPath(node string) string { return "nodes/" + node + "/epoch" }

// NextEpoch issues the next epoch for node. Two callers racing get two
// DIFFERENT epochs, in order; the loser of the CAS re-reads and goes again.
// Nobody ever receives the same number.
func NextEpoch(v Variables, node string) (epoch, index int64, err error) {
	const retries = 10
	for i := 0; i < retries; i++ {
		items, idx, err := v.Get(epochPath(node))
		if err != nil {
			return 0, 0, err
		}
		current := atoi(items["epoch"])
		newIdx, err := v.Put(epochPath(node), Items{"epoch": strconv.FormatInt(current+1, 10)}, idx)
		if errors.Is(err, ErrConflict) {
			continue
		}
		if err != nil {
			return 0, 0, err
		}
		return current + 1, newIdx, nil
	}
	return 0, 0, fmt.Errorf("could not issue an epoch for %s after %d conflicts", node, retries)
}

func CurrentEpoch(v Variables, node string) (int64, error) {
	items, _, err := v.Get(epochPath(node))
	if err != nil {
		return 0, err
	}
	return atoi(items["epoch"]), nil
}

// Clock is seconds on a monotonic origin; tests hand in their own.
type Clock func() float64

func Monotonic() Clock {
	start := time.Now()
	return func() float64 { return time.Since(start).Seconds() }
}

// Lease is held by the running instance. Renew is a read of the epoch
// Variable; MayWrite is a purely local decision on a monotonic clock.
type Lease struct {
	Vars        Variables
	recorder        string
	Epoch       int64
	TTL, Margin float64
	Clock       Clock
	LastRenewal float64
	Fenced      bool
	Conflicts   int // node_epoch_conflicts
}

func NewLease(v Variables, node string, epoch int64, ttl, margin float64, clock Clock) *Lease {
	return &Lease{Vars: v, recorder: node, Epoch: epoch, TTL: ttl, Margin: margin, Clock: clock, LastRenewal: clock()}
}

// Renew reports whether the lease still holds. False (and Fenced) if the
// cluster issued a newer epoch — or false, not fenced, if the read failed
// and the TTL ran out.
func (l *Lease) Renew() bool {
	if l.Fenced {
		return false
	}
	live, err := CurrentEpoch(l.Vars, l.recorder)
	if err != nil { // the cluster is unreachable; keep going until TTL − margin
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
	if l.Fenced {
		return false
	}
	return l.Clock()-l.LastRenewal < l.TTL-l.Margin
}

func (l *Lease) SecondsLeft() float64 {
	left := (l.TTL - l.Margin) - (l.Clock() - l.LastRenewal)
	if left < 0 {
		return 0
	}
	return left
}

func atoi(s string) int64 {
	n, _ := strconv.ParseInt(s, 10, 64)
	return n
}
