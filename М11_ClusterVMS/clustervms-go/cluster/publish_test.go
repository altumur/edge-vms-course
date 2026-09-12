package cluster

// Lesson 3 — object first, then the Variable; the floor; `replicated`;
// the six steps; unconfigured; a dangling pointer refused.

import (
	"errors"
	"strings"
	"testing"
)

type spyObjs struct {
	ObjectStore
	calls *[]string
}

func (s spyObjs) Put(k string, d []byte) error {
	*s.calls = append(*s.calls, "object "+k)
	return s.ObjectStore.Put(k, d)
}

type spyVars struct {
	Variables
	calls *[]string
}

func (s spyVars) Put(p string, items Items, cas int64) (int64, error) {
	*s.calls = append(*s.calls, "variable "+items["config"])
	return s.Variables.Put(p, items, cas)
}

func TestPublishOrderObjectThenPointer(t *testing.T) {
	v, objs := world(t)
	store := NewFakeClusterStore(Cam(7, 3))
	var calls []string
	pub := NewPublisher("node-3", store, spyVars{v, &calls}, spyObjs{objs, &calls}, 0, (&fakeClock{1000}).now)
	if !pub.PublishOnce() {
		t.Fatal("publish")
	}
	if strings.Join(calls, ",") != "object node-3/rev-3,variable node-3/rev-3" {
		t.Fatalf("order: %v", calls)
	}
	items, _, _ := v.Get("nodes/node-3")
	if items["config"] != "node-3/rev-3" || items["cameras"] != "7" || items["revision"] != "3" {
		t.Fatalf("pointer: %v", items)
	}
	if b, _ := objs.Get("node-3/rev-3"); b == nil {
		t.Fatal("object missing")
	}
}

func TestPublishOnChangeWithAFloor(t *testing.T) {
	v, objs := world(t)
	clk := &fakeClock{1000}
	store := NewFakeClusterStore(Cam(7, 1))
	pub := NewPublisher("node-3", store, v, objs, 5, clk.now)
	pub.PublishOnce()
	pub.PublishOnce() // nothing changed: nothing published
	if pub.Publishes != 1 {
		t.Fatal("publish on change")
	}
	store.Edit(7, func(c *CameraRow) { c.RetentionDays = 14 }) // rev 2
	pub.PublishOnce()
	if pub.Publishes != 1 { // inside the floor: not yet
		t.Fatal("the floor")
	}
	if ok, why := pub.Replicated(2); ok || why != "not yet replicated (1 edit(s) pending)" {
		t.Fatalf("%v %q", ok, why)
	}
	clk.advance(5)
	pub.PublishOnce()
	if ok, why := pub.Replicated(2); pub.Publishes != 2 || !ok || why != "" {
		t.Fatal("after the floor")
	}
}

type downObjs struct{}

func (downObjs) Put(string, []byte) error   { return errors.New("minio down") }
func (downObjs) Get(string) ([]byte, error) { return nil, nil }

func TestDirectoryUnreachableNeverBlocksTheEdit(t *testing.T) {
	v, _ := world(t)
	clk := &fakeClock{1000}
	store := NewFakeClusterStore(Cam(7, 1))
	pub := NewPublisher("node-3", store, v, downObjs{}, 0, clk.now)
	if pub.PublishOnce() {
		t.Fatal("must fail")
	}
	if rev := store.Edit(7, func(c *CameraRow) { c.Name = "x" }); rev != 2 { // acknowledged locally regardless
		t.Fatal("local ack")
	}
	clk.advance(90)
	if ok, why := pub.Replicated(2); ok || !strings.Contains(why, "unreachable for 90s") {
		t.Fatalf("%v %q", ok, why)
	}
}

func seen(t *testing.T, v *FakeVariables, objs ObjectStore, node string, cams ...CameraRow) Identity {
	t.Helper()
	a := NewFakeClusterStore(cams...)
	if !NewPublisher(node, a, v, objs, 0, (&fakeClock{}).now).PublishOnce() {
		t.Fatal("publish")
	}
	items, _, _ := v.Get("nodes/" + node)
	ids := make([]int64, 0, len(cams))
	for _, c := range cams {
		ids = append(ids, c.ID)
	}
	return Identity{recorder: node, ConfigObject: items["config"], ConfigRevision: atoi(items["revision"]), CameraIDs: ids}
}

func TestTheSixStepsRestoreASeenNode(t *testing.T) {
	v, objs := world(t)
	ident := seen(t, v, objs, "node-3", Cam(7, 4), Cam(8, 2))
	b := NewFakeClusterStore() // step 1: empty
	r, err := Rehydrate(ident, b, objs)
	must(t, err)
	if r.State != "restored" || r.Revision != 4 || r.Cameras != 2 {
		t.Fatalf("%+v", r)
	}
	if b.Rows[7].Revision != 4 || b.Rows[8].RetentionDays != 30 {
		t.Fatal("rows")
	}
}

func TestTheRPOIsTheUnpublishedEdit(t *testing.T) {
	v, objs := world(t)
	a := NewFakeClusterStore(Cam(7, 1))
	pub := NewPublisher("node-3", a, v, objs, 0, (&fakeClock{}).now)
	pub.PublishOnce()
	a.Edit(7, func(c *CameraRow) { c.RetentionDays = 14 }) // acknowledged locally: rev 2
	if ok, _ := pub.Replicated(2); ok {                    // ...and shown as not yet replicated
		t.Fatal("must show unreplicated")
	}
	items, _, _ := v.Get("nodes/node-3")
	b := NewFakeClusterStore()
	r, err := Rehydrate(Identity{recorder: "node-3", ConfigObject: items["config"], ConfigRevision: atoi(items["revision"])}, b, objs)
	must(t, err)
	if r.Revision != 1 || b.Rows[7].RetentionDays != 30 { // the edit is gone. That is the RPO.
		t.Fatal("RPO")
	}
}

func TestNeverSeenComesUpUnconfigured(t *testing.T) {
	_, objs := world(t)
	r, err := Rehydrate(Identity{recorder: "node-9"}, NewFakeClusterStore(), objs)
	must(t, err)
	if r.State != "unconfigured" {
		t.Fatal(r.State)
	}
}

func TestDanglingPointerAndRevisionMismatchAreRefused(t *testing.T) {
	v, objs := world(t)
	var refused *RestoreRefused
	_, err := Rehydrate(Identity{recorder: "node-3", ConfigObject: "node-3/rev-812", ConfigRevision: 812}, NewFakeClusterStore(), objs)
	if !errors.As(err, &refused) || !strings.Contains(err.Error(), "no such object") {
		t.Fatalf("must refuse: %v", err)
	}
	seen(t, v, objs, "node-3", Cam(7, 4))
	_, err = Rehydrate(Identity{recorder: "node-3", ConfigObject: "node-3/rev-4", ConfigRevision: 5}, NewFakeClusterStore(), objs)
	if !errors.As(err, &refused) || !strings.Contains(err.Error(), "revision 4") {
		t.Fatalf("must refuse: %v", err)
	}
}

func TestRestartOnTheSameServerRestoresNothing(t *testing.T) {
	_, objs := world(t)
	a := NewFakeClusterStore(Cam(7, 4))
	r, err := Rehydrate(Identity{recorder: "node-3", ConfigObject: "node-3/rev-1", ConfigRevision: 1}, a, objs)
	must(t, err)
	if r.State != "already-configured" || a.Rows[7].Revision != 4 {
		t.Fatal(r.State)
	}
}

// The blob is format 1 on both sides: this is what Python's FakeClusterStore
// published for cameras 7 (rev 4) and 8 (rev 2), captured verbatim. A Go
// recorder restores it — and a Python recorder restores Go's (the README shows the
// round trip).
const pythonBlob = `{"cameras": [{"cred_secret": null, "cred_username": null, "enabled": true, "id": 7, "name": "cam7", "priority": 100, "retention_days": 30, "revision": 4, "rtsp_url": "rtsp://10.0.0.7/s", "site_id": "hq"}, {"cred_secret": null, "cred_username": null, "enabled": true, "id": 8, "name": "cam8", "priority": 100, "retention_days": 30, "revision": 2, "rtsp_url": "rtsp://10.0.0.8/s", "site_id": "hq"}], "format": 1, "grants": [], "operators": [], "revision": 4, "sites": [{"id": "hq", "name": "hq"}]}`

func TestAGoNodeRestoresWhatAPythonNodePublished(t *testing.T) {
	_, objs := world(t)
	must(t, objs.Put("node-3/rev-4", []byte(pythonBlob)))
	b := NewFakeClusterStore()
	r, err := Rehydrate(Identity{recorder: "node-3", ConfigObject: "node-3/rev-4", ConfigRevision: 4}, b, objs)
	must(t, err)
	if r.State != "restored" || r.Cameras != 2 || b.Rows[8].RtspURL != "rtsp://10.0.0.8/s" || b.Rows[7].CredSecret != nil {
		t.Fatalf("%+v %+v", r, b.Rows)
	}
	blob, _ := b.DumpConfig() // and what Go publishes decodes back to the same rows
	c, err := Decode(blob)
	must(t, err)
	if c.Revision != 4 || len(c.Cameras) != 2 || c.Cameras[0].Name != "cam7" {
		t.Fatal("round trip")
	}
}
