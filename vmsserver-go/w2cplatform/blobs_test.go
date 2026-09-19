// A field too big for a row: the bytes in the object store, the digest in the row.
//
// Some units carry one opaque lump — a detector's per-pixel mask, a panel's firmware, a model. It belongs
// to exactly one unit, the platform never reads inside it, and it does not fit in a row: a Nomad Variable
// holds 64 KiB and a per-pixel mask for 1920x1080 is 345 KB as base64.
//
// Moving such configuration wholesale into the object store is the obvious fix and the wrong one: an
// object has no CAS, no one writer and no revision, so the reconciler never learns it changed. These tests
// are about the shape that keeps the mechanism — the row holds a DIGEST, and everything follows from that.
package w2cplatform_test

import (
	"bytes"
	"encoding/base64"
	"errors"
	"sort"
	"strings"
	"testing"

	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/w2cplatform"
)

const detYAML = `
name: det
unit:
  rows: units
  id: name
  fields:
    name:   {type: string, required: true}
    cam:    {type: string, required: true}
    kind:   {type: string, required: true}
    params: {type: string}
    mask:   {type: blob}
    enabled: {type: bool, default: true}
placement:
  capacity:   {from: capacity, fallback: 8}
  headroom:   {from: headroom}
  constraint: labels-subset
  requires:   none
  tie_break:  most-free-capacity
`

// 345 600 bytes: the real thing, not a stand-in.
var mask = []byte(base64.StdEncoding.EncodeToString(make([]byte, 1920*1080/8)))

func detSpec(t *testing.T) *p.SubsystemSpec {
	t.Helper()
	v, err := p.ParseYAML(detYAML)
	if err != nil {
		t.Fatal(err)
	}
	spec, err := p.SpecFromMap(v.(map[string]any))
	if err != nil {
		t.Fatal(err)
	}
	return spec
}

func detCtl(t *testing.T, box *testbox.Box, objects p.ObjectStore) *p.SpecController {
	t.Helper()
	if objects == nil {
		objects = box.Objects
	}
	return p.NewSpecController(detSpec(t), box.Vars, objects, 8, box.Wall.Now, "cluster-a")
}

// The obvious thing for a client to do is paste the lump into the row, and that is also what puts a row
// over the store's ceiling. The refusal names the route instead of just saying no.
func TestTheRowTakesADigestAndSaysSoWhenHandedTheBytes(t *testing.T) {
	box := testbox.NewBox()
	ctl := detCtl(t, box, nil)
	if _, err := ctl.Create(map[string]any{"name": "7-linecross", "cam": "7", "kind": "linecross"}); err != nil {
		t.Fatal(err)
	}
	_, err := ctl.Update("7-linecross", map[string]any{"mask": string(mask)})
	if err == nil || !strings.Contains(err.Error(), "takes a digest, not the bytes") {
		t.Fatal("the bytes went into the row:", err)
	}
	if !strings.Contains(err.Error(), "/units/<id>/mask") {
		t.Fatal("the refusal does not say where to put them instead:", err)
	}
}

func TestTheBytesGoToTheObjectStoreUnderTheirOwnName(t *testing.T) {
	box := testbox.NewBox()
	ctl := detCtl(t, box, nil)
	if _, err := ctl.Create(map[string]any{"name": "7-linecross", "cam": "7", "kind": "linecross"}); err != nil {
		t.Fatal(err)
	}
	d, err := ctl.PutBlob(mask) // 1. the object
	if err != nil {
		t.Fatal(err)
	}
	row, err := ctl.Update("7-linecross", map[string]any{"mask": d}) // 2. the row that names it
	if err != nil {
		t.Fatal(err)
	}
	if !p.IsDigest(d) || d != p.Digest(mask) || p.Str(row["mask"]) != d {
		t.Fatal(d, row["mask"])
	}
	got, _ := box.Objects.Get("det/blobs/" + d)
	if string(got) != string(mask) {
		t.Fatal("the bytes are not in the object store under their digest")
	}
	back, _ := ctl.Blob(d)
	if string(back) != string(mask) {
		t.Fatal("Blob did not give them back")
	}
	// the row stayed a row: the lump is not in it, by three orders of magnitude
	if n := p.ItemsBytes(detSpec(t).ItemsOf(row)); n > 500 || n >= len(mask) {
		t.Fatal("the row grew with the lump:", n)
	}
}

// Content addressed, so this is true without anyone arranging it — and it is also why writing a blob
// twice is safe: the second write writes the same bytes to the same key.
func TestTheSameBytesAreOneObjectHoweverManyUnitsNameThem(t *testing.T) {
	box := testbox.NewBox()
	ctl := detCtl(t, box, nil)
	for _, i := range []string{"7", "8", "9"} {
		if _, err := ctl.Create(map[string]any{"name": i + "-linecross", "cam": i, "kind": "linecross"}); err != nil {
			t.Fatal(err)
		}
		d, err := ctl.PutBlob(mask)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := ctl.Update(i+"-linecross", map[string]any{"mask": d}); err != nil {
			t.Fatal(err)
		}
	}
	keys, _ := box.Objects.List("det/blobs/")
	if len(keys) != 1 || len(ctl.BlobsReferenced()) != 1 {
		t.Fatal(keys, ctl.BlobsReferenced())
	}
}

// The whole reason the digest is in the row. Nothing here is a new mechanism: a unit whose revision moved
// gets restarted, and a different lump is a different digest is a different row.
//
// Put the bytes in the object store alone and this test is impossible to write — the object changes, the
// row does not, and the worker goes on with the old mask until something else restarts it.
func TestChangingTheBlobMovesTheRevision(t *testing.T) {
	box := testbox.NewBox()
	ctl := detCtl(t, box, nil)
	if _, err := ctl.Create(map[string]any{"name": "7-linecross", "cam": "7", "kind": "linecross"}); err != nil {
		t.Fatal(err)
	}
	d1, _ := ctl.PutBlob(mask)
	r1, err := ctl.Update("7-linecross", map[string]any{"mask": d1})
	if err != nil {
		t.Fatal(err)
	}
	other := []byte(base64.StdEncoding.EncodeToString(append([]byte{0xff}, make([]byte, 1920*1080/8-1)...)))
	d2, _ := ctl.PutBlob(other)
	r2, err := ctl.Update("7-linecross", map[string]any{"mask": d2})
	if err != nil {
		t.Fatal(err)
	}
	if d1 == d2 {
		t.Fatal("different bytes, same digest")
	}
	if p.ToFloat(r2["revision"]) != p.ToFloat(r1["revision"])+1 {
		t.Fatal("the revision did not move, so nothing would restart:", r1["revision"], r2["revision"])
	}
}

// Refused at LOAD time, like a secret — and for a plainer reason: the snapshot is one object per worker
// under a ceiling, and a blob is by definition what did not fit in a row.
func TestABlobMayNotBeInTheSnapshot(t *testing.T) {
	spec := detSpec(t)
	for _, n := range spec.Snapshot {
		if n == "mask" {
			t.Fatal("the default snapshot kept the blob:", spec.Snapshot)
		}
	}
	v, _ := p.ParseYAML(detYAML + "snapshot: [name, mask]\n")
	_, err := p.SpecFromMap(v.(map[string]any))
	if err == nil || !strings.Contains(err.Error(), "a blob may not be in the snapshot") {
		t.Fatal("a blob was accepted into the snapshot:", err)
	}
}

func TestTheConsoleTakesTheBytesOnTheirOwnRoute(t *testing.T) {
	box := testbox.NewBox()
	ctl := detCtl(t, box, nil)
	con := p.NewSpecConsole(ctl, p.ConsoleOptions{})
	if rep := con.Create(map[string]any{"name": "7-linecross", "cam": "7", "kind": "linecross"}); rep.Status != 201 {
		t.Fatal(rep)
	}
	rep := con.PutBlob("7-linecross", "mask", mask)
	if rep.Status != 200 || body(rep)["mask"] != p.Digest(mask) || body(rep)["bytes"] != len(mask) {
		t.Fatal(rep.Status, body(rep))
	}
	if p.Str(ctl.Unit("7-linecross")["mask"]) != p.Digest(mask) {
		t.Fatal("the row does not name the bytes")
	}
	// a field that is not a blob, and a unit that is not there, are both 404 and not a stack trace
	if con.PutBlob("7-linecross", "params", []byte("x")).Status != 404 {
		t.Fatal("params is not a blob field")
	}
	if con.PutBlob("9-linecross", "mask", []byte("x")).Status != 404 {
		t.Fatal("no such unit")
	}
}

// The one place where "change the object store" is the right answer, and the message says so: a blob is
// the class of data an object store exists for, unlike the snapshot, which was a shape problem.
func TestABlobOverTheStoresCeilingNamesTheStore(t *testing.T) {
	box := testbox.NewBox()
	capped, err := p.NewFsObjectStoreCapped(t.TempDir(), 65536-len("data"))
	if err != nil {
		t.Fatal(err)
	}
	ctl := detCtl(t, box, capped)
	con := p.NewSpecConsole(ctl, p.ConsoleOptions{})
	con.Create(map[string]any{"name": "7-linecross", "cam": "7", "kind": "linecross"})
	rep := con.PutBlob("7-linecross", "mask", mask)
	if rep.Status != 413 {
		t.Fatal(rep.Status, rep.Body)
	}
	detail := p.Str(body(rep)["detail"])
	if !strings.Contains(detail, "OBJECTS=s3+https://") || !strings.Contains(detail, "variables:// does not") {
		t.Fatal(detail)
	}
	if p.Str(ctl.Unit("7-linecross")["mask"]) != "" {
		t.Fatal("the row was written even though the bytes were not")
	}
}

// BlobsReferenced is the honest half. Nothing in the platform deletes an object, so a replaced mask stays
// in the store forever and this is the list that would tell a collector which ones to keep.
func TestWhatASweepWouldKeepAndTheSweepThatDoesNotExist(t *testing.T) {
	box := testbox.NewBox()
	ctl := detCtl(t, box, nil)
	ctl.Create(map[string]any{"name": "7-linecross", "cam": "7", "kind": "linecross"})
	old, _ := ctl.PutBlob(mask)
	ctl.Update("7-linecross", map[string]any{"mask": old})
	fresh, _ := ctl.PutBlob([]byte("a small mask"))
	ctl.Update("7-linecross", map[string]any{"mask": fresh})

	keys, _ := box.Objects.List("det/blobs/")
	if len(keys) != 2 {
		t.Fatal("the replaced blob was collected by something; nothing collects:", keys)
	}
	ref := ctl.BlobsReferenced()
	if len(ref) != 1 || !ref[fresh] {
		t.Fatal(ref)
	}
	if ref[old] {
		t.Fatal("the replaced blob is still referenced")
	}
	// and this is the garbage nobody collects
	if !strings.Contains(strings.Join(keys, ","), old) {
		t.Fatal("expected the orphan to still be there")
	}
}

// A digest is the only thing a blob key is built from — a name is refused rather than accepted and then
// puzzled over later.
func TestABlobKeyIsBuiltOnlyFromADigest(t *testing.T) {
	sub := p.Subsystem{Name: "det"}
	if _, err := sub.BlobKey("mask.png"); err == nil || !strings.Contains(err.Error(), "not a digest") {
		t.Fatal(err)
	}
	k, err := sub.BlobKey(p.Digest([]byte("x")))
	if err != nil || !strings.HasPrefix(k, "det/blobs/sha256-") {
		t.Fatal(k, err)
	}
	if strings.Contains(k, ":") {
		t.Fatal("a colon in a key is not a path character everywhere (Lesson 24):", k)
	}
	var tooLarge *p.TooLarge
	_ = errors.As(error(nil), &tooLarge)
}

// The property an ACL cannot give.
//
// Whoever can write `det/blobs/<digest>` can put other bytes there — on a cluster that is anyone the
// policy lets near the object prefix, and the policy is static text written by a person. The digest is the
// only thing that says what those bytes ARE, and it is worth nothing until someone checks it.
//
// Checked on the READ, where the bytes are about to be used. Checking only on the write would be trusting
// the writer again, which is the thing being replaced.
func TestAPoisonedBlobIsCaughtOnTheReadAndNotTrusted(t *testing.T) {
	box := testbox.NewBox()
	ctl := detCtl(t, box, nil)
	ctl.Create(map[string]any{"name": "7-linecross", "cam": "7", "kind": "linecross"})
	d, _ := ctl.PutBlob(mask)
	ctl.Update("7-linecross", map[string]any{"mask": d})
	if got, err := ctl.Blob(d); err != nil || string(got) != string(mask) {
		t.Fatal(err)
	}

	box.Objects.Put("det/blobs/"+d, []byte("not the mask at all")) // the write an ACL is supposed to stop
	_, err := ctl.Blob(d)
	if !errors.Is(err, p.ErrBlobMismatch) {
		t.Fatal("the poisoned bytes were handed to the caller:", err)
	}
	if !strings.Contains(err.Error(), p.Digest([]byte("not the mask at all"))) {
		t.Fatal("the refusal does not say what it actually found:", err)
	}

	// …and it holds for reasons that have nothing to do with a writer: a truncated object, a bad disk, a
	// copy between stores that lost a byte.
	box.Objects.Put("det/blobs/"+d, mask[:len(mask)-1])
	if _, err := ctl.Blob(d); !errors.Is(err, p.ErrBlobMismatch) {
		t.Fatal("a truncated object was handed to the caller:", err)
	}
}

// Three sibling directories under the subsystem, one writer each — the layout the grants are cut from.
// The worker's name moved to the LAST segment so that "the workers write here and nowhere else" is a
// prefix a policy can name at all; while it was first, the narrowest expressible grant was the whole
// subsystem, which also covers the snapshot М12 reads and the blobs the workers trust.
func TestTheThreeObjectDirectoriesHaveOneWriterEach(t *testing.T) {
	sub := p.Subsystem{Name: "vms"}
	hb := sub.HeartbeatKey("w-1")
	snap, _ := sub.SnapshotKey("w-1")
	blob, _ := sub.BlobKey(p.Digest([]byte("x")))
	if hb != "vms/heartbeats/w-1" || snap != "vms/snapshot/w-1" || !strings.HasPrefix(blob, "vms/blobs/") {
		t.Fatal(hb, snap, blob)
	}
	// each grant covers its own directory and neither of the others
	for _, c := range []struct {
		grants []string
		mine   string
		theirs []string
	}{
		{sub.ACLObjectsWorker(), hb, []string{snap, blob}},
		{sub.ACLObjectsController(), snap, []string{hb, blob}},
		{sub.ACLObjectsConsole(), blob, []string{hb, snap}},
	} {
		if len(c.grants) != 1 {
			t.Fatal("one directory, one grant:", c.grants)
		}
		pre := strings.TrimSuffix(c.grants[0], "*")
		if !strings.HasPrefix(c.mine, pre) {
			t.Fatal(c.grants[0], "does not cover", c.mine)
		}
		for _, other := range c.theirs {
			if strings.HasPrefix(other, pre) {
				t.Fatal(c.grants[0], "also covers", other)
			}
		}
	}
}

// Collecting blobs nothing names any more — the one thing in the platform that deletes an object.
//
// A blob key is the digest of its bytes, so every edit of a blob field makes a NEW permanent object. That
// is what makes blobs different from everything else in the object store: a heartbeat's key is reused by
// the next instance of the slot, so stale heartbeats are bounded and are kept on purpose (a worker reads
// the previous instance's heartbeat to measure its own failover). Blobs grow with the number of EDITS.
func blobNames(t *testing.T, box *testbox.Box) []string {
	t.Helper()
	keys, _ := box.Objects.List("det/blobs/")
	out := []string{}
	for _, k := range keys {
		out = append(out, strings.TrimPrefix(k, "det/blobs/"))
	}
	sort.Strings(out)
	return out
}

var maskA, maskB = bytes.Repeat([]byte("a"), 900), bytes.Repeat([]byte("b"), 900)

func TestABlobNothingNamesIsCollectedButNeverOnThePassThatNoticedIt(t *testing.T) {
	box := testbox.NewBox()
	ctl := detCtl(t, box, nil)
	ctl.Create(map[string]any{"name": "7-linecross", "cam": "7", "kind": "linecross"})
	d1, _ := ctl.PutBlob(maskA)
	ctl.Update("7-linecross", map[string]any{"mask": d1})
	d2, _ := ctl.PutBlob(maskB)
	ctl.Update("7-linecross", map[string]any{"mask": d2}) // maskA is now an orphan
	if len(blobNames(t, box)) != 2 {
		t.Fatal(blobNames(t, box))
	}

	r, err := ctl.SweepBlobs(0, 0)
	if err != nil || r.Marked != 1 || r.Deleted != 0 {
		t.Fatal(r, err)
	}
	if len(blobNames(t, box)) != 2 {
		t.Fatal("the pass that noticed also deleted")
	}
	if r, _ := ctl.SweepBlobs(0, 0); r.Waiting != 1 { // inside the grace period
		t.Fatal(r)
	}

	box.Wall.Advance(301)
	if r, err := ctl.SweepBlobs(0, 0); err != nil || r.Deleted != 1 {
		t.Fatal(r, err)
	}
	if got := blobNames(t, box); len(got) != 1 || got[0] != d2 {
		t.Fatal("the referenced blob went too, or the orphan stayed:", got)
	}
}

// The race the design exists for. PutBlob writes the object first and the row second; a sweep that decided
// in one breath would delete the bytes a row is about to name. It cannot: the digest was not on the list.
func TestABlobWrittenBetweenTheTwoPassesSurvives(t *testing.T) {
	box := testbox.NewBox()
	ctl := detCtl(t, box, nil)
	ctl.Create(map[string]any{"name": "7-linecross", "cam": "7", "kind": "linecross"})
	d1, _ := ctl.PutBlob(maskA)
	ctl.Update("7-linecross", map[string]any{"mask": d1})
	d2, _ := ctl.PutBlob(maskB)
	ctl.Update("7-linecross", map[string]any{"mask": d2})
	ctl.SweepBlobs(0, 0) // marks maskA

	box.Wall.Advance(301)
	ctl.Create(map[string]any{"name": "8-linecross", "cam": "8", "kind": "linecross"})
	fresh, _ := ctl.PutBlob([]byte("a brand new mask")) // object written; the row not yet
	ctl.SweepBlobs(0, 0)                                // …and the sweep runs right here
	if raw, _ := box.Objects.Get("det/blobs/" + fresh); raw == nil {
		t.Fatal("the sweep ate a blob whose row was in flight")
	}
	if _, err := ctl.Update("8-linecross", map[string]any{"mask": fresh}); err != nil {
		t.Fatal(err)
	}
}

// The residual race, closed. The same bytes can be uploaded for a second unit while the first unit's copy
// is marked. PutBlob takes the digest off the list, which makes the sweep's own CAS fail — and a sweep
// that loses that CAS deletes NOTHING, because the clear comes before any delete.
func TestReUploadingAMarkedBlobCallsTheWholeSweepOff(t *testing.T) {
	box := testbox.NewBox()
	ctl := detCtl(t, box, nil)
	ctl.Create(map[string]any{"name": "7-linecross", "cam": "7", "kind": "linecross"})
	d1, _ := ctl.PutBlob(maskA)
	ctl.Update("7-linecross", map[string]any{"mask": d1})
	d2, _ := ctl.PutBlob(maskB)
	ctl.Update("7-linecross", map[string]any{"mask": d2})
	ctl.SweepBlobs(0, 0)

	box.Wall.Advance(301)
	ctl.Create(map[string]any{"name": "8-linecross", "cam": "8", "kind": "linecross"})
	again, _ := ctl.PutBlob(maskA) // the marked digest, uploaded again
	items, _, _ := box.Vars.Get("det/sweep")
	if items["digests"] != "[]" {
		t.Fatal("PutBlob did not take it off the list:", items)
	}
	ctl.Update("8-linecross", map[string]any{"mask": again})

	ctl.SweepBlobs(0, 0)
	if raw, _ := box.Objects.Get("det/blobs/" + again); raw == nil {
		t.Fatal("the sweep deleted a blob a row names")
	}
}

// The invariant Lesson 4 stated as "nothing deletes" is narrower now, and the narrower one has to be
// checked: the READERS still assume an object stays. Sweep the heartbeats and failover measurement goes
// with them. So the sweep touches blobs/ and nothing else.
func TestNothingButTheSweepLeansOnObjectsGoingAway(t *testing.T) {
	box := testbox.NewBox()
	ctl := detCtl(t, box, nil)
	box.Objects.Put("det/heartbeats/d-1", []byte(`{"worker":"d-1","ts":0,"status":[]}`))
	box.Objects.Put("det/snapshot/d-1", []byte(`{"cluster":"c","worker":"d-1","ts":0,"units":[]}`))
	ctl.Create(map[string]any{"name": "7-linecross", "cam": "7", "kind": "linecross"})
	ctl.PutBlob(maskA) // an orphan from the first breath

	ctl.SweepBlobs(0, 0)
	box.Wall.Advance(301)
	ctl.SweepBlobs(0, 0)
	if len(blobNames(t, box)) != 0 {
		t.Fatal("the orphan stayed:", blobNames(t, box))
	}
	if raw, _ := box.Objects.Get("det/heartbeats/d-1"); raw == nil {
		t.Fatal("the sweep took a heartbeat")
	}
	if raw, _ := box.Objects.Get("det/snapshot/d-1"); raw == nil {
		t.Fatal("the sweep took a snapshot shard")
	}
}

// `det/sweep` is a Variable, and a Variable has the store's ceiling over it (Lesson 26). So the sweep
// collects at most limit per pass — the rule applies to the thing written under it.
func TestTheSweepIsBoundedBecauseItsOwnBookkeepingIsARow(t *testing.T) {
	box := testbox.NewBox()
	ctl := detCtl(t, box, nil)
	ctl.Create(map[string]any{"name": "7-linecross", "cam": "7", "kind": "linecross"})
	for i := 0; i < 10; i++ {
		d, _ := ctl.PutBlob(bytes.Repeat([]byte{byte(i)}, 500))
		ctl.Update("7-linecross", map[string]any{"mask": d})
	}
	if len(blobNames(t, box)) != 10 {
		t.Fatal(len(blobNames(t, box)))
	}
	if r, _ := ctl.SweepBlobs(4, 0); r.Marked != 4 {
		t.Fatal(r)
	}
	box.Wall.Advance(301)
	if r, _ := ctl.SweepBlobs(4, 0); r.Deleted != 4 {
		t.Fatal(r)
	}
	if len(blobNames(t, box)) != 6 {
		t.Fatal(blobNames(t, box))
	}
}

// A subsystem with no blob field must not list, not decide and not write a row: nothing to collect is not
// the same as nothing collected.
func TestASubsystemWithNoBlobFieldIsNotSweptAtAll(t *testing.T) {
	box := testbox.NewBox()
	ctl := p.NewSpecController(vms.Spec, box.Vars, box.Objects, 50, box.Wall.Now, "cluster-a")
	r, err := ctl.SweepBlobs(0, 0)
	if err != nil || (r != p.SweepResult{}) {
		t.Fatal(r, err)
	}
	if items, _, _ := box.Vars.Get("vms/sweep"); items != nil {
		t.Fatal("a subsystem with nothing to sweep wrote bookkeeping:", items)
	}
}

// racingVars fires a hook the first time something lists — which, inside SweepBlobs, is BlobsReferenced
// walking the rows: after the sweep read its decision and before it writes the row back.
type racingVars struct {
	p.Variables
	hook func()
}

func (v *racingVars) List(prefix string) ([]string, error) {
	if v.hook != nil {
		h := v.hook
		v.hook = nil
		h()
	}
	return v.Variables.List(prefix)
}

// The ordering argument, exercised where it lives: INSIDE one pass.
//
// The sweeper reads the list, checks, clears the row by CAS, and only then removes the bytes. If somebody
// re-uploads a marked blob after that read, the CAS fails — and because the clear comes FIRST, nothing has
// been deleted when it does. Delete first and the same interleaving destroys bytes whose row is in flight.
func TestTheDecisionIsClearedBeforeAnythingIsDeleted(t *testing.T) {
	box := testbox.NewBox()
	other := detCtl(t, box, nil)
	racing := &racingVars{Variables: box.Vars}
	ctl := p.NewSpecController(detSpec(t), racing, box.Objects, 8, box.Wall.Now, "cluster-a")

	ctl.Create(map[string]any{"name": "7-linecross", "cam": "7", "kind": "linecross"})
	d1, _ := ctl.PutBlob(maskA)
	ctl.Update("7-linecross", map[string]any{"mask": d1})
	d2, _ := ctl.PutBlob(maskB)
	ctl.Update("7-linecross", map[string]any{"mask": d2})
	ctl.SweepBlobs(0, 0) // maskA is marked
	box.Wall.Advance(301)

	// the real interleaving: the OBJECT is written and the row naming it is still in flight, so the digest
	// is genuinely unreferenced at the re-check — and the only thing between it and deletion is that
	// PutBlob took it off the list, which the CAS is about to notice
	racing.hook = func() { other.PutBlob(maskA) }

	if _, err := ctl.SweepBlobs(0, 0); !errors.Is(err, p.ErrConflict) {
		t.Fatal("the sweeper wrote over a decision something had contradicted:", err)
	}
	if raw, _ := box.Objects.Get("det/blobs/" + p.Digest(maskA)); raw == nil {
		t.Fatal("the CAS was lost AFTER the delete: the bytes of a row still in flight are gone")
	}
}
