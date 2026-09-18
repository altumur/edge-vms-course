// The snapshot's SHAPE: one object per worker, not one object per cluster.
//
// Every other object the platform publishes is already sharded by its writer — a heartbeat per worker, a
// resource heartbeat per server — and each stays the size of what that writer knows. The snapshot was the
// exception: one object holding every unit in the cluster, published into a store with a ceiling. Nomad
// Variables cap an object at 64 KiB, and the cluster's own design maximum is 600 cameras (12 workers x
// capacity 50, from the worker jobspec). The single object broke at about a third of that.
//
// Sharded by the worker that holds the unit, it grows the way the cluster grows. The arithmetic is
// measured below rather than asserted.
package vms_test

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"vmsserver/testbox"
	"vmsserver/vms"
	p "vmsserver/w2cplatform"
)

const cap64 = 64 * 1024 // Nomad Variables: the whole object, keys and values

var shardLabels = []string{"vlan:cctv-a", "site:msk-hq", "floor:3"}

func workerAlive(box *testbox.Box, worker, server string, capacity int) {
	box.Objects.Put(vms.Spec.Sub().HeartbeatKey(worker), p.Heartbeat{Worker: worker, Ts: box.Wall.Now(),
		Extra: map[string]any{"server": server, "capacity": capacity, "headroom": capacity,
			"labels": strings.Join(shardLabels, ",")}}.ToBytes())
	raw, _ := json.Marshal(map[string]any{"server": server, "ts": box.Wall.Now(),
		"url": "http://" + server, "units": map[string]any{}})
	box.Objects.Put("platform/resources/"+server+"/heartbeat", raw)
}

// A realistic cluster: cameras with the fields an operator actually fills in, on workers that heartbeat —
// so `server` in the snapshot is a real name and the bytes measured below are the bytes that would be
// published.
func shardCluster(t *testing.T, box *testbox.Box, cameras, workers, capacity int) *vms.VmsController {
	t.Helper()
	ctl := vms.NewVmsController(box.Vars.AsWriter("vmscontroller", vms.Spec.ACLController()...), box.Objects, capacity, box.Wall.Now)
	con := vms.NewVmsController(box.Vars.AsWriter("console", vms.Spec.ACLConsole()...), box.Objects, capacity, box.Wall.Now)
	for i := 0; i < workers; i++ {
		workerAlive(box, fmt.Sprintf("w-%d", i), fmt.Sprintf("srv-cctv-%02d", i), capacity)
	}
	for i := 0; i < cameras; i++ {
		mustCreate(t, con, map[string]any{
			"name":   fmt.Sprintf("Подъезд %d — вход", i),
			"ref":    fmt.Sprintf("msk-hq/fl3/cam-%04d", i),
			"source": fmt.Sprintf("driverpack://hikvision/10.20.%d.%d/Streaming/Channels/101", i/254, i%254),
			"labels": shardLabels})
	}
	return ctl
}

func shardKeys(t *testing.T, box *testbox.Box) []string {
	t.Helper()
	keys, err := box.Objects.List("vms/snapshot/")
	if err != nil {
		t.Fatal(err)
	}
	return keys
}

func TestTheSnapshotIsOneObjectPerWorker(t *testing.T) {
	box := testbox.NewBox()
	ctl := shardCluster(t, box, 6, 2, 3)
	if _, err := ctl.EnsurePlaced(nil); err != nil {
		t.Fatal(err)
	}
	if err := ctl.PublishSnapshot(); err != nil {
		t.Fatal(err)
	}
	keys := shardKeys(t, box)
	if strings.Join(keys, ",") != "vms/snapshot/w-0,vms/snapshot/w-1" {
		t.Fatal("the workers, and nothing else:", keys)
	}
	for _, key := range keys {
		raw, _ := box.Objects.Get(key)
		var shard map[string]any
		if err := json.Unmarshal(raw, &shard); err != nil {
			t.Fatal(err)
		}
		name := key[strings.LastIndex(key, "/")+1:]
		if shard["worker"] != name {
			t.Fatal("a shard that does not say whose it is:", shard["worker"], name)
		}
		rows := shard["cameras"].([]any)
		if len(rows) != 3 {
			t.Fatal("the worker's assignment, and only that:", len(rows))
		}
		for _, r := range rows {
			if r.(map[string]any)["worker"] != name {
				t.Fatal("a row in the wrong shard:", r)
			}
		}
	}
	snap, _ := box.PublishedSnapshot("vms", "cameras")
	if len(snap["cameras"].([]any)) != 6 {
		t.Fatal("merged, it is not the cluster's cameras:", snap)
	}
}

// They are not dropped and they are not mixed in with a worker's: a row with no worker is exactly what
// М12 must be able to see.
func TestTheUnitsNobodyHoldsHaveAShardOfTheirOwn(t *testing.T) {
	box := testbox.NewBox()
	ctl := shardCluster(t, box, 4, 1, 2) // capacity 2: two cameras have nowhere to go
	if _, err := ctl.EnsurePlaced(nil); err != nil {
		t.Fatal(err)
	}
	if err := ctl.PublishSnapshot(); err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(shardKeys(t, box), ","); got != "vms/snapshot/unplaced,vms/snapshot/w-0" {
		t.Fatal(got)
	}
	raw, _ := box.Objects.Get("vms/snapshot/unplaced")
	var shard map[string]any
	json.Unmarshal(raw, &shard)
	if shard["worker"] != nil || len(shard["cameras"].([]any)) != 2 {
		t.Fatal("the units nobody holds:", shard)
	}
	for _, r := range shard["cameras"].([]any) {
		if r.(map[string]any)["worker"] != nil {
			t.Fatal("a placed row in the unplaced shard:", r)
		}
	}
}

// Nothing in the platform deletes an object. A worker that is scaled in, or whose units moved away, would
// go on being reported to the domain out of the shard it left behind — so the pass writes it EMPTY.
func TestAWorkerThatIsGoneStopsReportingItsCameras(t *testing.T) {
	box := testbox.NewBox()
	ctl := shardCluster(t, box, 2, 2, 1)
	if _, err := ctl.EnsurePlaced(nil); err != nil {
		t.Fatal(err)
	}
	if err := ctl.PublishSnapshot(); err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(shardKeys(t, box), ","); got != "vms/snapshot/w-0,vms/snapshot/w-1" {
		t.Fatal(got)
	}
	if _, err := ctl.MoveTo("2", "w-0", "w-1 scaled in"); err != nil {
		t.Fatal(err)
	}
	if err := ctl.PublishSnapshot(); err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(shardKeys(t, box), ","); got != "vms/snapshot/w-0,vms/snapshot/w-1" {
		t.Fatal("the key stays:", got)
	}
	raw, _ := box.Objects.Get("vms/snapshot/w-1")
	var shard map[string]any
	json.Unmarshal(raw, &shard)
	if len(shard["cameras"].([]any)) != 0 {
		t.Fatal("a worker that is gone is still reporting:", shard)
	}
	snap, _ := box.PublishedSnapshot("vms", "cameras")
	rows := snap["cameras"].([]any)
	if len(rows) != 2 {
		t.Fatal("each camera once:", rows)
	}
	for _, r := range rows {
		if r.(map[string]any)["worker"] != "w-0" {
			t.Fatal("a camera on a worker that is gone:", r)
		}
	}
}

// `vms/snapshot/` is the worker name space, and `unplaced` is reserved inside it. A reserved name needs a
// rule that reserves it, not a hope.
func TestAWorkerMayNotBeCalledUnplaced(t *testing.T) {
	box := testbox.NewBox()
	ctl := shardCluster(t, box, 1, 0, 50)
	workerAlive(box, "unplaced", "srv-cctv-00", 50)
	if _, err := ctl.EnsurePlaced(nil); err != nil {
		t.Fatal(err)
	}
	err := ctl.PublishSnapshot()
	if err == nil || !strings.Contains(err.Error(), `may not be called "unplaced"`) {
		t.Fatal("a worker shadowed the shard for the units nobody holds:", err)
	}
}

// The measurement the shape exists for. Both numbers come from the real publisher, against the real
// 64 KiB ceiling — nothing here is asserted from a table.
func TestTheShardFitsWhereTheOneObjectDidNot(t *testing.T) {
	box := testbox.NewBox()
	ctl := shardCluster(t, box, 600, 12, 50) // 12 workers x 50: the jobspec's maximum
	if _, err := ctl.EnsurePlaced(nil); err != nil {
		t.Fatal(err)
	}
	if err := ctl.PublishSnapshot(); err != nil {
		t.Fatal(err)
	}
	keys := shardKeys(t, box)
	if len(keys) != 12 {
		t.Fatal("twelve workers, twelve objects:", keys)
	}
	biggest := 0
	for _, key := range keys {
		raw, _ := box.Objects.Get(key)
		if len(raw) > biggest {
			biggest = len(raw)
		}
	}
	oneObject, _ := json.Marshal(ctl.Snapshot()) // what it used to publish
	if len(oneObject) <= cap64 {
		t.Fatalf("the single object fits after all (%d B) — this test proved nothing", len(oneObject))
	}
	if biggest > cap64/3 {
		t.Fatalf("a shard is %d B: the margin the heartbeat has is gone", biggest)
	}
	t.Logf("600 камер: один объект %.0f КиБ (потолок %d), крупнейший шард %.1f КиБ — запас x%.1f",
		float64(len(oneObject))/1024, cap64/1024, float64(biggest)/1024, float64(cap64)/float64(biggest))
}
