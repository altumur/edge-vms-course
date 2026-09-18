// The contract every Variables backend must keep — one suite, any backend.
//
// A store is swapped by changing CONFIG_URL, which is only safe if "it works"
// means something checkable. This is that meaning: file today, Nomad in М11,
// Kubernetes when there is a site for it. A new backend is accepted when this
// file is green against it, not when it looks right.
//
// Run against another backend by pointing CONTRACT_URL at it:
//
//	CONTRACT_URL=nomad://127.0.0.1:4646 go test ./w2cplatform -run Contract
//
// The contract, in eight clauses:
//
//  1. a path that was never written reads as (nil, Absent);
//  2. Put returns an Index that identifies the version; the next read gives it back;
//  3. cas is the whole of the concurrency story: N racers, one winner, N-1 refusals;
//  4. a writer may only write its own prefixes, and is refused — not ignored — elsewhere;
//  5. the Index is OPAQUE. It is compared for equality and nothing else, because
//     Kubernetes' resourceVersion is a string and arithmetic on it is meaningless.
//     Clause 5 is the one that quietly decides whether the k8s backend is possible.
//  6. a key has exactly ONE spelling: ".." and a leading "/" are REFUSED, not repaired.
//  7. what a read hands back is a COPY. A caller that mutates it must not have edited
//     the store — an edit without an index is the one thing CAS exists to prevent, and a
//     backend that returns its own state gives it away for free.
//  8. the store SAYS what it can hold (MaxBytes, 0 meaning no ceiling), and a write over that
//     is refused, not truncated. Nomad caps a Variable at 64 KiB and the platform has no say
//     in it; before this clause that number lived in prose, FileVariables accepted anything,
//     and a write that production would reject was green in every test.
package w2cplatform_test

import (
	"errors"
	"fmt"
	"os"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"

	p "vmsserver/w2cplatform"
)

// The backend under test: this box's files by default, whatever CONTRACT_URL says otherwise.
func store(t *testing.T) p.Variables { return storeAt(t, "") }

func storeAt(t *testing.T, at string) p.Variables {
	t.Helper()
	url := at
	if url == "" {
		url = os.Getenv("CONTRACT_URL")
	}
	if url == "" {
		url = "file://" + t.TempDir()
	}
	v, err := p.OpenVars(url, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func TestContractAnUnwrittenPathReadsAsAbsent(t *testing.T) {
	// Not an error, not an empty map. The whole platform leans on it: NextEpoch,
	// ClaimSlot and every Controller.Write start from a path that is not there
	// yet and write it with cas=Absent.
	items, idx, err := store(t).Get("contract/absent")
	if err != nil || items != nil || idx != p.Absent || idx.Exists() {
		t.Fatal(items, idx, err)
	}
}

func TestContractAWriteIsReadableAndCarriesAVersion(t *testing.T) {
	v := store(t)
	i1, err := v.Put("contract/a", p.Items{"n": "1"}, p.Absent)
	if err != nil {
		t.Fatal(err)
	}
	items, i2, _ := v.Get("contract/a")
	if !reflect.DeepEqual(items, p.Items{"n": "1"}) || i2 != i1 {
		t.Fatal(items, i2, i1)
	}
	i3, _ := v.Put("contract/a", p.Items{"n": "2"}, i1)
	got, _, _ := v.Get("contract/a")
	if !reflect.DeepEqual(got, p.Items{"n": "2"}) || i3 == i1 {
		t.Fatal(got, i3)
	}
}

// Clause 7, and the one a backend gets for free only by accident.
//
// A file backend parses JSON on every read, so what a caller holds is already its own; a backend that
// keeps its state in memory — this process's memory://, a cache in front of raft, anything that does not
// re-parse — hands back the store itself unless it deliberately does not. Then a caller that edits the map
// it read has written to the store WITH NO INDEX: no CAS, no conflict, no version, and the next reader
// sees a change nobody can point at.
//
// This clause exists because it was missing. memory:// copied correctly from the first line, and removing
// the copy broke nothing in the suite — which meant the suite was not the contract it claimed to be. A
// clause nothing tests is a comment.
func TestContractAReadHandsBackACopy(t *testing.T) {
	v := store(t)
	idx, err := v.Put("contract/copy", p.Items{"n": "1"}, p.Absent)
	if err != nil {
		t.Fatal(err)
	}

	items, got, _ := v.Get("contract/copy")
	items["n"] = "999"
	items["sneaked"] = "yes"

	again, idx2, _ := v.Get("contract/copy")
	if len(again) != 1 || again["n"] != "1" {
		t.Fatalf("a read handed back the store's own row: %v", again)
	}
	if idx2 != got || idx2 != idx {
		t.Fatal("the store changed version without anyone writing to it")
	}

	// …and the same for what a caller PUT: keeping the map it passed is the same hole from the other side.
	row := p.Items{"n": "2"}
	if _, err := v.Put("contract/copy", row, idx); err != nil {
		t.Fatal(err)
	}
	row["n"] = "666"
	if kept, _, _ := v.Get("contract/copy"); kept["n"] != "2" {
		t.Fatalf("Put kept the caller's map: %v", kept)
	}
}

//
// A store is swapped by changing CONFIG_URL, which is only safe if "it works"
// means something checkable. This is that meaning: file today, Nomad in М11,
// Kubernetes when there is a site for it. A new backend is accepted when this
// file is green against it, not when it looks right.
//
// Run against another backend by pointing CONTRACT_URL at it:
//
//	CONTRACT_URL=nomad://127.0.0.1:4646 go test ./w2cplatform -run Contract
//
// The contract, in eight clauses:
//
//  1. a path that was never written reads as (nil, Absent);
//  2. Put returns an Index that identifies the version; the next read gives it back;
//  3. cas is the whole of the concurrency story: N racers, one winner, N-1 refusals;
//  4. a writer may only write its own prefixes, and is refused — not ignored — elsewhere;
//  5. the Index is OPAQUE. It is compared for equality and nothing else, because
//     Kubernetes' resourceVersion is a string and arithmetic on it is meaningless.
//     Clause 5 is the one that quietly decides whether the k8s backend is possible.
//  6. a key has exactly ONE spelling: ".." and a leading "/" are REFUSED, not repaired.
//  7. what a read hands back is a COPY. A caller that mutates it must not have edited
//     the store — an edit without an index is the one thing CAS exists to prevent, and a
//     backend that returns its own state gives it away for free.
//  8. the store SAYS what it can hold (MaxBytes, 0 meaning no ceiling), and a write over that
//     is refused, not truncated. Nomad caps a Variable at 64 KiB and the platform has no say
//     in it; before this clause that number lived in prose, FileVariables accepted anything,
//     and a write that production would reject was green in every test.

func TestContractEverythingIsStrings(t *testing.T) {
	// The reason the contract fits Kubernetes at all: ConfigMap.data is
	// map[string]string, and Items has always been one too. The store itself only
	// promises Str(); the true/false spelling a subsystem's rows use is
	// SubsystemSpec's, one layer up — the store never interprets a value.
	v := store(t)
	v.Put("contract/types", p.Items{"n": p.Str(7), "flag": p.Str(true)}, p.Absent)
	got, _, _ := v.Get("contract/types")
	if !reflect.DeepEqual(got, p.Items{"n": "7", "flag": "true"}) {
		t.Fatal(got)
	}
}

func TestContractCASLetsExactlyOneRacerThrough(t *testing.T) {
	// Four goroutines, one path, one winner. This is the only concurrency
	// primitive the platform has: no locks, no leases at this layer, no transactions.
	v := store(t)
	v.Put("contract/race", p.Items{"n": "0"}, p.Absent)
	_, idx, _ := v.Get("contract/race")
	var mu sync.Mutex
	var won, refused []int
	var wg sync.WaitGroup
	for k := 0; k < 4; k++ {
		wg.Add(1)
		go func(k int) {
			defer wg.Done()
			_, err := v.Put("contract/race", p.Items{"n": strconv.Itoa(k)}, idx)
			mu.Lock()
			defer mu.Unlock()
			if err == nil {
				won = append(won, k)
			} else {
				refused = append(refused, k)
			}
		}(k)
	}
	wg.Wait()
	if len(won) != 1 || len(refused) != 3 {
		t.Fatal(won, refused)
	}
	got, _, _ := v.Get("contract/race")
	if got["n"] != strconv.Itoa(won[0]) {
		t.Fatal(got, won)
	}
}

func TestContractAStaleCASIsRefusedNotApplied(t *testing.T) {
	v := store(t)
	i, _ := v.Put("contract/stale", p.Items{"n": "1"}, p.Absent)
	v.Put("contract/stale", p.Items{"n": "2"}, i)
	if _, err := v.Put("contract/stale", p.Items{"n": "3"}, i); !errors.Is(err, p.ErrConflict) {
		t.Fatal("a stale index must not write")
	}
	got, _, _ := v.Get("contract/stale")
	if got["n"] != "2" {
		t.Fatal(got)
	}
}

func TestContractListReturnsThePathsUnderAPrefix(t *testing.T) {
	v := store(t)
	for _, k := range []string{"contract/list/a", "contract/list/b", "contract/other/c"} {
		v.Put(k, p.Items{"x": "1"}, p.Absent)
	}
	got, _ := v.List("contract/list/")
	sort.Strings(got)
	if !reflect.DeepEqual(got, []string{"contract/list/a", "contract/list/b"}) {
		t.Fatal(got)
	}
}

func TestContractAWriterIsRefusedOutsideItsPrefixes(t *testing.T) {
	// Refused, not ignored: a token that writes where it may not must fail
	// loudly, or the split that holds the whole system is decoration.
	url := os.Getenv("CONTRACT_URL")
	if url == "" {
		url = "file://" + t.TempDir()
	}
	w, err := p.OpenVars(url, "contract-writer", map[string][]string{"contract-writer": {"contract/mine/*"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := w.Put("contract/mine/k", p.Items{"x": "1"}, p.Absent); err != nil {
		t.Fatal(err)
	}
	if _, err := w.Put("contract/yours/k", p.Items{"x": "1"}, p.Absent); !errors.Is(err, p.ErrForbidden) {
		t.Fatal("a writer wrote outside its prefixes")
	}
	if items, _, _ := w.Get("contract/yours/k"); items != nil {
		t.Fatal(items)
	}
}

func TestContractAKeyHasOneSpelling(t *testing.T) {
	// Refused, not repaired, and by every backend — because the same text becomes
	// three things: the key, the prefix an ACL is matched against, and (through
	// UnitDir) a directory on a resource's disk. Un-normalised, vms/a/../b and
	// vms/b are two keys one person reads as one, each with its own index, so two
	// writers both win their CAS. Normalised downstream — a URL, a tree — they
	// collapse into one, and the ACL was matched against the string BEFORE that.
	v := store(t)
	for _, bad := range []string{"vms/a/../b", "../etc/passwd", "/vms/a", ""} {
		if _, err := v.Put(bad, p.Items{"x": "1"}, p.Absent); err == nil {
			t.Fatalf("a store accepted %q as a key", bad)
		}
	}
	if _, err := v.Put("vms/..foo", p.Items{"x": "1"}, p.Absent); err != nil {
		t.Fatal("dots that are not a segment are just a name:", err)
	}
	if items, _, _ := v.Get("vms/..foo"); !reflect.DeepEqual(items, p.Items{"x": "1"}) {
		t.Fatal(items)
	}
}

func TestContractTwoSpellingsAreNeverOnePlace(t *testing.T) {
	// The same rule arriving through the encoding instead of through the path: a
	// backend that maps "/" to "%2F" without escaping "%" first makes the key
	// `a%2Fb` and the key `a/b` the same file — and List then reports one of them,
	// so the collision is invisible.
	v := store(t)
	v.Put("a/b", p.Items{"who": "slash"}, p.Absent)
	v.Put("a%2Fb", p.Items{"who": "percent"}, p.Absent)
	if items, _, _ := v.Get("a/b"); items["who"] != "slash" {
		t.Fatal(items)
	}
	if items, _, _ := v.Get("a%2Fb"); items["who"] != "percent" {
		t.Fatal(items)
	}
	got, _ := v.List("a")
	sort.Strings(got)
	if !reflect.DeepEqual(got, []string{"a%2Fb", "a/b"}) {
		t.Fatal(got)
	}
}

func TestContractTheIndexIsOpaque(t *testing.T) {
	// Compared for equality, never ordered and never arithmetic. Nomad's
	// ModifyIndex is a number and invites both; Kubernetes' resourceVersion is a
	// string, and a backend for it is only possible while nothing in the platform
	// does anything to this value but hand it back.
	v := store(t)
	i, _ := v.Put("contract/opaque", p.Items{"x": "1"}, p.Absent)
	if _, got, _ := v.Get("contract/opaque"); got != i {
		t.Fatal(got, i)
	}
	j, _ := v.Put("contract/opaque", p.Items{"x": "2"}, i)
	if j == i {
		t.Fatal("a write must move the version")
	}
}

// opaqueStore is a store whose version is a STRING that means nothing —
// "rv-7", not 7. Kubernetes hands out exactly this kind of value
// (resourceVersion, "treated as opaque … passed unmodified back"), and a
// backend for it is only possible while nothing in the platform interprets the
// index. This is the check for that: not a type annotation, a working store the
// real CAS loops are driven against.
func (s *opaqueStore) MaxBytes() int { return p.NoCeiling }

type opaqueStore struct {
	mu   sync.Mutex
	data map[string]struct {
		items p.Items
		idx   p.Index
	}
	n int
}

func newOpaqueStore() *opaqueStore {
	return &opaqueStore{data: map[string]struct {
		items p.Items
		idx   p.Index
	}{}}
}

func (s *opaqueStore) Get(path string) (p.Items, p.Index, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	d, ok := s.data[path]
	if !ok {
		return nil, p.Absent, nil
	}
	out := p.Items{}
	for k, v := range d.items {
		out[k] = v
	}
	return out, d.idx, nil
}

func (s *opaqueStore) Put(path string, items p.Items, cas p.Index) (p.Index, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	cur := p.Absent
	if d, ok := s.data[path]; ok {
		cur = d.idx
	}
	if cas.Conditional() && cas != cur {
		return p.Absent, fmt.Errorf("%w: %s: cas=%s but the version is %s", p.ErrConflict, path, cas, cur)
	}
	s.n++
	idx := p.Idx("rv-" + strconv.Itoa(s.n)) // no order, no arithmetic, not even a number
	cp := p.Items{}
	for k, v := range items {
		cp[k] = v
	}
	s.data[path] = struct {
		items p.Items
		idx   p.Index
	}{cp, idx}
	return idx, nil
}

func (s *opaqueStore) List(prefix string) ([]string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []string
	for k := range s.data {
		if len(k) >= len(prefix) && k[:len(prefix)] == prefix {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out, nil
}

func (s *opaqueStore) Delete(path string, cas p.Index) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.data, path)
	return nil
}

func TestContractHoldsWhenTheVersionIsNotANumber(t *testing.T) {
	v := newOpaqueStore()
	if items, idx, _ := v.Get("a"); items != nil || idx != p.Absent {
		t.Fatal(items, idx) // absent is Absent everywhere — the one non-version…
	}
	i, _ := v.Put("a", p.Items{"n": "1"}, p.Absent) // …which is what makes cas=Absent mean "create only"
	if _, got, _ := v.Get("a"); got != i || i.String() != "rv-1" {
		t.Fatal(got, i)
	}
	if _, err := v.Put("a", p.Items{"n": "2"}, p.Absent); !errors.Is(err, p.ErrConflict) {
		t.Fatal("create-only must not overwrite an existing path")
	}
	j, _ := v.Put("a", p.Items{"n": "2"}, i)
	if j == i {
		t.Fatal(j)
	}
	if _, err := v.Put("a", p.Items{"n": "3"}, i); !errors.Is(err, p.ErrConflict) {
		t.Fatal("a stale version must not write")
	}
}

func TestContractThePlatformsCASLoopsRunOverANonNumericVersion(t *testing.T) {
	// The loops themselves — NextEpoch, ClaimSlot, Controller.Write — never look
	// inside the index. Driven here against a store whose version is rv-<n>: if
	// any of them ordered or incremented it, this is where that would show.
	v := newOpaqueStore()

	e1, i1, err := p.NextEpoch(v, "vms/epoch/7") // read-modify-CAS, twice, on one path
	if err != nil {
		t.Fatal(err)
	}
	e2, i2, _ := p.NextEpoch(v, "vms/epoch/7")
	if e1 != 1 || e2 != 2 || i1 == i2 { // the EPOCH is a number; the index is not
		t.Fatal(e1, e2, i1, i2)
	}

	objects, _ := p.NewFsObjectStore(t.TempDir())
	now := 1000.0
	wall := func() float64 { return now }
	w := p.NewWorker(p.Subsystem{Name: "vms"}, v, objects, p.WorkerOptions{Wall: wall, Clock: wall})
	name, err := w.ClaimSlot("") // CAS over a path whose version is a string
	if err != nil || name == "" {
		t.Fatal(name, err)
	}
	if !w.RenewSlot() {
		t.Fatal("the slot I hold must renew")
	}
	w.ReleaseSlot()
	items, _, _ := v.Get(w.Sub.SlotKey(name))
	if items["released"] != "true" {
		t.Fatal(items)
	}
}


// Clause 8, in two halves, because every backend has the first and only some have the second.
//
// Every store answers MaxBytes. A directory answers 0 — no ceiling — and that is an ANSWER, not a missing
// method: the platform can ask any store and get a number.
//
// A store that declares a ceiling refuses a write over it and leaves what was there alone. Refusing
// matters more than the number: a store that truncates is a store whose Get returns something its Put
// never wrote, and every CAS loop above is built on Get telling the truth.
//
// Run against a store that HAS a ceiling — memory://…?max_bytes=n — so the second half is exercised here
// and not only on a cluster.
func TestContractTheStoreSaysWhatItCanHoldAndRefusesMore(t *testing.T) {
	v := store(t)
	if v.MaxBytes() < 0 {
		t.Fatal("a store must answer what it can hold:", v.MaxBytes())
	}

	capped := storeAt(t, "memory://contract-capped-go?max_bytes=256")
	if capped.MaxBytes() != 256 {
		t.Fatal(capped.MaxBytes())
	}
	small := p.Items{"a": strings.Repeat("x", 100)}
	idx, err := capped.Put("contract/capped", small, p.Absent)
	if err != nil {
		t.Fatal(err)
	}

	big := p.Items{"a": strings.Repeat("x", 500)}
	_, err = capped.Put("contract/capped", big, idx)
	var tooLarge *p.TooLarge
	if !errors.As(err, &tooLarge) {
		t.Fatal("a write over the store's own ceiling was accepted:", err)
	}
	if tooLarge.Limit != 256 || tooLarge.Size != p.ItemsBytes(big) {
		t.Fatal(tooLarge)
	}

	// and the refusal left the previous value exactly where it was
	items, index, _ := capped.Get("contract/capped")
	if !reflect.DeepEqual(items, small) || index != idx {
		t.Fatal(items, index, idx)
	}
}
