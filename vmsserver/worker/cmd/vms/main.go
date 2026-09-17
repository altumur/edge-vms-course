// vms worker|recorder — the two processes of the Go port, on one box.
//
// The other four — controller, reccontroller, console, resource — are Python (../../). That is not a gap
// in this binary: placement, the console and the resource's policy pass are one process each, cheap to
// run and expensive to get right, and they meet these two only in the store. A worker reads its
// assignment and does what it says; it has no opinion about how the assignment was arrived at, which is
// what lets the halves be written in different languages at all.
//
//	PLATFORM_DIR=/data/platform     the platform's stores (config/, objects/)
//	SPOOL=/data/spool  ARCHIVE=/data/archive   the recorder's two roots; the worker uses ARCHIVE for its events only
//	SHM_DIR=/run/vms                 the worker's shared-memory fan-out branch, read by a recorder on this box
//	CONFIG_URL                       the store, as a URL: file://<PLATFORM_DIR>/config by default — in-process,
//	                                 no daemon, no hop. nomad://host:port in a cluster, k8s://ns/prefix at a k8s
//	                                 site; no loop in this binary names an orchestrator (w2cplatform/runtime.go)
//	WORKER_NAME=w-1 / RECORDER_NAME=r-1   the slot to claim (systemd: %i); unset: SLOT_INDEX → w-<index>;
//	                                 neither: the first free slot, a lapsed one first
//	CAPACITY=50                      cameras this worker can hold (a recorder: recordings it can write) — exported as headroom
package main

import (
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"vmsserver/worker/vms"
	p "vmsserver/worker/w2cplatform"
)

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// openVars is the store seam, said once: a URL and an identity, never a class name. Whichever backend
// answers that URL, the loops below are the same — and it is the same URL the Python half opens.
func openVars(root, writer string, allowed ...string) p.Variables {
	url := env("CONFIG_URL", "file://"+filepath.Join(root, "config"))
	acl := map[string][]string(nil)
	if writer != "" {
		acl = map[string][]string{writer: allowed}
	}
	v, err := p.OpenVars(url, writer, acl)
	if err != nil {
		log.Fatal(err)
	}
	return v
}

func main() {
	root := env("PLATFORM_DIR", "/data/platform")
	stop := make(chan struct{})
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
	go func() { <-sig; close(stop) }()
	capacity, _ := strconv.Atoi(env("CAPACITY", "50"))
	objects, _ := p.NewFsObjectStore(filepath.Join(root, "objects"))
	spool, archive := env("SPOOL", "/data/spool"), env("ARCHIVE", "/data/archive")
	switch env1(os.Args) {
	case "worker":
		name := p.SlotName(nil, "WORKER_NAME", "w")
		vars := openVars(root, "vmsworker", "vms/epoch/*", "vms/slots/*")
		log.Println("no GStreamer in the Go port: the fake actuator holds nothing")
		w, err := vms.NewVmsWorker(name, vars, objects, vms.NewFakeActuator(), vms.VmsWorkerOptions{Capacity: capacity, ArchiveRoot: archive})
		if err != nil {
			log.Fatal(err)
		}
		log.Printf("worker %s (instance %s) claimed its slot", w.Name, w.Instance)
		w.Run(2*time.Second, stop)
	case "recorder": // the only writer of footage: subscribes to the worker's tee, writes rec/<cam>/e<epoch>/ on THIS box's archive
		vars := openVars(root, "recworker", "rec/epoch/*", "rec/slots/*")
		log.Println("no GStreamer in the Go port: the fake actuator records nothing")
		r, err := vms.NewRecWorker(os.Getenv("RECORDER_NAME"), vars, objects, vms.NewFakeActuator(), vms.NewArchiveResource(spool, archive, 600, nil),
			vms.VmsWorkerOptions{Capacity: capacity})
		if err != nil {
			log.Fatal(err)
		}
		log.Printf("recorder %s (instance %s) claimed its slot", r.Name, r.Instance)
		r.Run(2*time.Second, stop)
	default:
		log.Fatal("usage: vms worker|recorder  (controller, reccontroller, console and resource are Python: python3 -m vms …)")
	}
}

func env1(args []string) string {
	if len(args) > 1 {
		return args[1]
	}
	return ""
}
