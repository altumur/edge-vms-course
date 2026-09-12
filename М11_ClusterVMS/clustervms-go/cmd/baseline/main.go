// cmd/baseline — the Go recorder at idle: the whole ClusterAppHost (reconcile,
// report, publish, lease, heartbeat, reindex, console) against in-memory
// fakes for Nomad and Postgres and a directory for the object store, with
// fifty cameras, doing nothing. Prints its own proportional set size (PSS)
// after settling. No GStreamer in either language — that is measured
// separately by М9's probe and costs the same everywhere.
package main

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"log"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"

	"clustervms/cluster"
)

func pssKB() int {
	f, err := os.Open("/proc/self/smaps_rollup")
	if err != nil {
		return -1
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if strings.HasPrefix(sc.Text(), "Pss:") {
			n, _ := strconv.Atoi(strings.Fields(sc.Text())[1])
			return n
		}
	}
	return -1
}

func main() {
	log.SetOutput(io.Discard) // idle means idle: no log lines either
	os.Setenv("CONSOLE_PORT", "0")
	os.Setenv("ARCHIVE_DIR", os.TempDir()+"/clustervms-baseline-archive")
	settings := cluster.SettingsFromEnv()
	vars := cluster.NewFakeVariables()
	dir, _ := os.MkdirTemp("", "restore-")
	defer os.RemoveAll(dir)
	objs, _ := cluster.NewFsObjectStore(dir)

	// node-3 has been seen: fifty cameras published by its previous instance.
	cams := make([]cluster.CameraRow, 0, 50)
	for i := int64(1); i <= 50; i++ {
		cams = append(cams, cluster.Cam(i, 1))
	}
	cluster.NewPublisher("node-3", cluster.NewFakeClusterStore(cams...), vars, objs, 0, cluster.Monotonic()).PublishOnce()
	items, _, _ := vars.Get("nodes/node-3")
	ident := cluster.Identity{recorder: "node-3", ConfigObject: items["config"], ConfigRevision: 1}

	host := cluster.NewClusterAppHost(settings, cluster.NewFakeClusterStore(), vars, objs, ident, cluster.NewFakeActuator(), nil, nil)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- host.Run(ctx, true) }()
	time.Sleep(1500 * time.Millisecond) // every task has ticked at least once
	runtime.GC()
	fmt.Printf("go node idle: PSS %d kB, %d goroutines, epoch %d, %s\n", pssKB(), runtime.NumGoroutine(),
		host.Settings.Epoch, host.Restore.State)
	cancel()
	<-done
}
