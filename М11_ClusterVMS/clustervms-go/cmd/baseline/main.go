// cmd/baseline — a Go server at idle: one worker (reconcile, pump, lease,
// heartbeat) and one controller (placement pass, snapshot) against the
// in-memory raft fake and a directory for the object store, with fifty
// cameras, doing nothing. Prints its own proportional set size (PSS) after
// settling. No GStreamer in either language — that is measured separately
// by М9's probe and costs the same everywhere.
package main

import (
	"bufio"
	"fmt"
	"io"
	"log"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"

	"clustervms/cluster"
	"vmsserver/vms"
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
	vars := cluster.NewFakeVariables()
	dir, _ := os.MkdirTemp("", "baseline-")
	defer os.RemoveAll(dir)
	objects, _ := cluster.NewFsObjectStore(dir + "/objects")
	ctl := cluster.NewClusterController(vars, objects, 50, nil, "bench")
	for i := 1; i <= 50; i++ {
		ctl.CreateCamera(map[string]any{"source": fmt.Sprintf("driverpack://file/%d.mp4", i)})
	}
	w, err := cluster.NewClusterWorker(vars, objects, vms.NewFakeActuator(), cluster.Env{"NOMAD_ALLOC_INDEX": "0", "NOMAD_NODE_NAME": "srv-a"},
		vms.VmsWorkerOptions{ArchiveRoot: dir + "/archive", Capacity: 50})
	if err != nil {
		panic(err)
	}
	w.HeartbeatOnce()
	ctl.EnsurePlaced(nil)
	stop := make(chan struct{})
	go w.Run(500*time.Millisecond, stop)
	go func() {
		for {
			ctl.EnsurePlaced(nil)
			ctl.Redistribute(nil)
			ctl.PublishSnapshot()
			select {
			case <-stop:
				return
			case <-time.After(time.Second):
			}
		}
	}()
	time.Sleep(1500 * time.Millisecond) // every loop has ticked at least once
	runtime.GC()
	fmt.Printf("go server idle: PSS %d kB, %d goroutines, %d cameras running\n", pssKB(), runtime.NumGoroutine(), len(w.Reconciler.Actual))
	close(stop)
	time.Sleep(100 * time.Millisecond)
}
