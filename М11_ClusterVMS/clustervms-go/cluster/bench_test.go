package cluster_test

// The controller's actual work, per operation — the same inputs as
// cmd/pybench/bench.py.

import (
	"fmt"
	"strconv"
	"testing"

	"clustervms/cluster"
	p "vmsserver/w2cplatform"
)

func BenchmarkIssueAnEpochByCAS(b *testing.B) {
	v := cluster.NewFakeVariables()
	for i := 0; i < b.N; i++ {
		p.NextEpoch(v, "vms/epoch/7")
	}
}

func BenchmarkDirectoryScan1000Workers(b *testing.B) {
	v := cluster.NewFakeVariables()
	for w := 0; w < 1000; w++ {
		var units []string
		for c := 0; c < 20; c++ {
			units = append(units, strconv.Itoa(w*20+c+1))
		}
		v.Put("vms/workers/w-"+strconv.Itoa(w), p.Assignment{Worker: "w-" + strconv.Itoa(w), Units: units, Rev: 1}.ToItems(), cluster.NoCAS)
	}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		d := cluster.NewDirectory(v, 5, nil)
		if d.Where(7007) != "w-350" {
			b.Fatal(d.Where(7007))
		}
	}
}

func BenchmarkPlace120CamerasOn4WorkersWithLabels(b *testing.B) {
	for i := 0; i < b.N; i++ {
		b.StopTimer()
		c := newCluster()
		ctl := c.controller(40, "")
		for _, w := range []struct {
			idx int
			srv string
		}{{0, "srv-a"}, {1, "srv-b"}, {2, "srv-c"}, {3, "srv-b"}} {
			c.worker(nil, w.idx, w.srv, 40, nil)
		}
		for _, w := range []string{"w-0", "w-1", "w-2", "w-3"} {
			hb := p.Heartbeat{Worker: w, Ts: c.Wall.Now(), Extra: map[string]any{"capacity": 40, "headroom": 40, "server": "srv-b", "labels": "vlan:cctv-a,vlan:cctv-b"}}
			c.Objects.Put("vms/"+w+"/heartbeat", hb.ToBytes())
		}
		for n := 0; n < 120; n++ {
			if n%2 == 0 {
				ctl.CreateCamera(map[string]any{"source": fmt.Sprintf("driverpack://file/%d.mp4", n), "labels": []string{"vlan:cctv-b"}})
			} else {
				ctl.CreateCamera(map[string]any{"source": fmt.Sprintf("driverpack://file/%d.mp4", n)})
			}
		}
		b.StartTimer()
		ctl.EnsurePlaced(nil)
	}
}

func BenchmarkParseOneBucketPath(b *testing.B) {
	for i := 0; i < b.N; i++ {
		if _, _, _, _, ok := p.ParseBucket("/a/vms/7/e5/20260912T101000Z.events.jsonl", "/a"); !ok {
			b.Fatal("parse")
		}
	}
}

func BenchmarkEncodeDecodeA50CameraHeartbeat(b *testing.B) {
	var status []map[string]any
	for i := 1; i <= 50; i++ {
		status = append(status, map[string]any{"id": i, "ref": "", "name": "cam", "enabled": true, "phase": "running", "position": "converged", "revision": 1, "observed_revision": 1, "epoch": 1})
	}
	hb := p.Heartbeat{Worker: "w-1", Ts: 1.0, Status: status, Extra: map[string]any{"server": "srv-a", "capacity": 50, "headroom": 0}}
	for i := 0; i < b.N; i++ {
		raw := hb.ToBytes()
		if _, err := p.HeartbeatFromBytes(raw); err != nil {
			b.Fatal(err)
		}
	}
}
