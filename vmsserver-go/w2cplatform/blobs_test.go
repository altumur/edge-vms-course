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
	"encoding/base64"
	"errors"
	"strings"
	"testing"

	"vmsserver/testbox"
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
