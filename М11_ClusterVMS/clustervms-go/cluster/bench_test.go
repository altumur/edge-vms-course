package cluster

// The controller's actual work, timed. cmd/pybench/bench.py times the same
// operations in Python; measure.sh prints both.

import (
	"fmt"
	"testing"
)

func BenchmarkSigV4Sign(b *testing.B) {
	payload := make([]byte, 64<<10) // a 64 kB configuration object
	for i := 0; i < b.N; i++ {
		Sign("PUT", "minio.cluster:9000", "/restore/node-3/rev-812", "", nil, payload, access, secret, "us-east-1", may24, "s3")
	}
}

func BenchmarkNextEpochCAS(b *testing.B) {
	v := NewFakeVariables()
	for i := 0; i < b.N; i++ {
		NextEpoch(v, "node-3")
	}
}

func BenchmarkDirectoryScan1000recorders(b *testing.B) {
	v := NewFakeVariables()
	for n := 1; n <= 1000; n++ {
		cams := ""
		for c := 0; c < 20; c++ {
			cams += fmt.Sprintf("%d,", n*100+c)
		}
		v.Put(fmt.Sprintf("nodes/node-%d", n), Items{"node": fmt.Sprintf("node-%d", n), "config": "x", "revision": "1", "cameras": cams}, NoCAS)
		v.Put(fmt.Sprintf("nodes/node-%d/epoch", n), Items{"epoch": "1"}, NoCAS)
	}
	d := NewDirectory(v, 0, (&fakeClock{}).now)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		d.Scan(true)
		d.Where(70007)
	}
}

func BenchmarkPlace120Cameras(b *testing.B) {
	for i := 0; i < b.N; i++ {
		nodes, cams := pworld(int64(i))
		p, _ := NewPlacer(nodes, NewFakeVariables())
		ids := make([]int64, 0, len(cams))
		for c := range cams {
			ids = append(ids, c)
		}
		sortInt64s(ids)
		for _, c := range ids {
			p.Place(cams[c], cams)
		}
	}
}

func BenchmarkParseSegmentPath(b *testing.B) {
	for i := 0; i < b.N; i++ {
		Parse("/data/archive/7/e5/20260908T091000Z.mp4", "/data/archive")
	}
}

func BenchmarkEncodeDecode200Cameras(b *testing.B) {
	cams := make([]CameraRow, 0, 200)
	for i := int64(1); i <= 200; i++ {
		cams = append(cams, Cam(i, i))
	}
	for i := 0; i < b.N; i++ {
		blob, _ := Encode([]Site{{"hq", "hq"}}, cams, nil, nil)
		Decode(blob)
	}
}
