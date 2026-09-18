package w2cplatform_test

import (
	"errors"
	"reflect"
	"strconv"
	"testing"

	p "vmsserver/w2cplatform"
)

// A backend is accepted when the contract suite is green against it — Lesson 3's rule, run rather than
// quoted:
//
//	CONTRACT_URL=memory:// go test ./w2cplatform -run Contract
//
// The tests below are what the suite does NOT cover, because they are about this backend's own idea of
// what a URL names rather than about the contract every backend keeps.

// One process, three identities, one store: what a dev binary does with memory://<name>.
func TestANamedMemoryStoreIsSharedAcrossOpens(t *testing.T) {
	ctl, err := p.OpenVars("memory://shared-test", "ctl", map[string][]string{"ctl": {"vms/*"}})
	if err != nil {
		t.Fatal(err)
	}
	wrk, err := p.OpenVars("memory://shared-test", "wrk", map[string][]string{"wrk": {"vms/epoch/*"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ctl.Put("vms/workers/w-1", p.Items{"units": "7"}, p.Absent); err != nil {
		t.Fatal(err)
	}
	if items, _, _ := wrk.Get("vms/workers/w-1"); items["units"] != "7" {
		t.Fatal("a second open of the same name is a different store:", items)
	}
	// …and sharing an address space is still not sharing a writer.
	if _, err := wrk.Put("vms/workers/w-1", p.Items{"units": "8"}, p.NoCAS); !errors.Is(err, p.ErrForbidden) {
		t.Fatal("the worker wrote the controller's path:", err)
	}
}

// A bare memory:// names nothing, so there is nothing to share by — which is what the contract suite
// needs, since every clause starts from an empty store.
func TestABareMemoryStoreIsPrivate(t *testing.T) {
	a, _ := p.OpenVars("memory://", "", nil)
	b, _ := p.OpenVars("memory://", "", nil)
	a.Put("k", p.Items{"x": "1"}, p.Absent)
	if items, idx, _ := b.Get("k"); items != nil || idx != p.Absent {
		t.Fatal("two bare memory:// opens share a store:", items)
	}
}

// A deleted path that comes back must not come back with an index somebody is still holding: otherwise a
// CAS written before the delete matches after it, and a retry that should have conflicted lands on a row
// it has never seen.
//
// Note what is NOT asserted: that the counter starts at 1000. It does, so that two backends read alike
// side by side — but a store that dies with its process has no "before the restart", so the value is
// cosmetic here, and an Index is opaque (clause 5), so there is nothing to compare it to anyway.
func TestAMemoryIndexIsNeverReusedInsideOneStore(t *testing.T) {
	v, _ := p.OpenVars("memory://", "", nil)
	first, err := v.Put("a", p.Items{"x": "1"}, p.Absent)
	if err != nil {
		t.Fatal(err)
	}
	if err := v.Delete("a", p.NoCAS); err != nil {
		t.Fatal(err)
	}
	second, _ := v.Put("a", p.Items{"x": "2"}, p.Absent)
	if reflect.DeepEqual(first, second) {
		t.Fatal("an index was reused after a delete:", first, second)
	}
	seen := map[p.Index]bool{first: true, second: true}
	for i := 0; i < 20; i++ {
		idx, _ := v.Put("k"+strconv.Itoa(i), p.Items{"x": "1"}, p.Absent)
		if seen[idx] {
			t.Fatal("two writes got one index:", idx)
		}
		seen[idx] = true
	}
}
