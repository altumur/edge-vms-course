// vms worker|controller — the two processes, on one box.
//
//	PLATFORM_DIR=/data/platform     the platform's stores (config/, objects/)
//	SPOOL=/data/spool  ARCHIVE=/data/archive
//	WORKER_NAME=w-1                  the slot to claim (systemd: %i); unset: NOMAD_ALLOC_INDEX → w-<index>;
//	                                 neither: the first free slot, a lapsed one first
//	CAPACITY=50                      cameras this worker can carry — exported as headroom for the autoscaler
//	CONSOLE_PORT=8080                the controller's console
package main

import (
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"vmsserver/vms"
	p "vmsserver/vmsplatform"
)

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
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
		name := os.Getenv("WORKER_NAME")
		if name == "" && os.Getenv("NOMAD_ALLOC_INDEX") != "" {
			name = "w-" + os.Getenv("NOMAD_ALLOC_INDEX")
		}
		vars, _ := p.NewFileVariables(filepath.Join(root, "config"))
		vars = vars.AsWriter("vmsworker", "vms/epoch/*", "vms/slots/*")
		res := vms.NewArchiveResource(spool, archive, 600, nil)
		for _, pth := range res.ClosedInSpool(30, p.Wall()()) { // what the last instance closed but did not promote
			res.Promote(pth, 0)
		}
		log.Println("no GStreamer in the Go port: the fake actuator records nothing")
		w, err := vms.NewVmsWorker(name, vars, objects, vms.NewFakeActuator(), vms.VmsWorkerOptions{Capacity: capacity, ArchiveRoot: archive})
		if err != nil {
			log.Fatal(err)
		}
		log.Printf("worker %s (instance %s) claimed its slot", w.Name, w.Instance)
		w.Run(2*time.Second, stop, nil)
	case "controller":
		vars, _ := p.NewFileVariables(filepath.Join(root, "config"))
		vars = vars.AsWriter("vmscontroller", "vms/*")
		ctl := vms.NewVmsController(vars, objects, capacity, nil)
		res := vms.NewArchiveResource(spool, archive, 600, nil)
		srv, ln, err := vms.Serve(ctl, res, env("CONSOLE_HOST", "127.0.0.1")+":"+env("CONSOLE_PORT", "8080"), nil)
		if err != nil {
			log.Fatal(err)
		}
		log.Printf("console on %s", ln.Addr())
		for {
			if _, err := ctl.EnsurePlaced(nil); err != nil { // new cameras onto the workers it sees
				log.Println("placement pass failed:", err)
			}
			ctl.Redistribute(nil) // cameras of a RELEASED slot (scale-in) onto the rest; nothing else, ever
			select {
			case <-stop:
				srv.Close()
				return
			case <-time.After(5 * time.Second):
			}
		}
	default:
		log.Fatal("usage: vms worker|controller")
	}
}

func env1(args []string) string {
	if len(args) > 1 {
		return args[1]
	}
	return ""
}
