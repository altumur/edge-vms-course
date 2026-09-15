// vms worker|controller|recorder|reccontroller|console|resource — the processes, on one box.
//
//	PLATFORM_DIR=/data/platform     the platform's stores (config/, objects/)
//	SPOOL=/data/spool  ARCHIVE=/data/archive   the recorder's two roots; the worker uses ARCHIVE for its events only
//	SHM_DIR=/run/vms                 the worker's shared-memory fan-out branch, read by a recorder on this box
//	WORKER_NAME=w-1 / RECORDER_NAME=r-1   the slot to claim (systemd: %i); unset: NOMAD_ALLOC_INDEX → w-<index>;
//	                                 neither: the first free slot, a lapsed one first
//	CAPACITY=50                      cameras this worker can hold (a recorder: recordings it can write) — exported as headroom
//	CONSOLE_PORT=8080                the console (its own process, its own token: the operator's rows, never placement)
package main

import (
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	p "vmsserver/psimplatform"
	"vmsserver/vms"
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
		log.Println("no GStreamer in the Go port: the fake actuator holds nothing")
		w, err := vms.NewVmsWorker(name, vars, objects, vms.NewFakeActuator(), vms.VmsWorkerOptions{Capacity: capacity, ArchiveRoot: archive})
		if err != nil {
			log.Fatal(err)
		}
		log.Printf("worker %s (instance %s) claimed its slot", w.Name, w.Instance)
		w.Run(2*time.Second, stop)
	case "recorder": // the only writer of footage: subscribes to the worker's tee, writes rec/<cam>/e<epoch>/ on THIS box's archive
		vars, _ := p.NewFileVariables(filepath.Join(root, "config"))
		vars = vars.AsWriter("vmsrecorder", "rec/epoch/*", "rec/slots/*")
		log.Println("no GStreamer in the Go port: the fake actuator records nothing")
		r, err := vms.NewRecWorker(os.Getenv("RECORDER_NAME"), vars, objects, vms.NewFakeActuator(), vms.NewArchiveResource(spool, archive, 600, nil),
			vms.VmsWorkerOptions{Capacity: capacity})
		if err != nil {
			log.Fatal(err)
		}
		log.Printf("recorder %s (instance %s) claimed its slot", r.Name, r.Instance)
		r.Run(2*time.Second, stop)
	case "reccontroller": // count = 1, the only writer of rec placement: recordings onto recorders whose resource answers, beside the camera's worker when there is room
		vars, _ := p.NewFileVariables(filepath.Join(root, "config"))
		vars = vars.AsWriter("vmsreccontroller", vms.RecSpec.ACLController()...)
		ctl := p.NewSpecController(vms.RecSpec, vars, objects, capacity, nil, "")
		for {
			if _, err := ctl.EnsurePlaced(nil); err != nil {
				log.Println("rec placement pass failed:", err)
			}
			ctl.Redistribute(nil)
			select {
			case <-stop:
				return
			case <-time.After(5 * time.Second):
			}
		}
	case "controller": // count = 1, the only writer of placement; no HTTP — nothing asks it anything
		vars, _ := p.NewFileVariables(filepath.Join(root, "config"))
		vars = vars.AsWriter("vmscontroller", vms.Spec.ACLController()...)
		ctl := vms.NewVmsController(vars, objects, capacity, nil)
		for {
			if _, err := ctl.EnsurePlaced(nil); err != nil { // deleted rows unplaced; new cameras onto the workers it sees
				log.Println("placement pass failed:", err)
			}
			ctl.Redistribute(nil) // cameras of a RELEASED slot (scale-in) onto the rest; nothing else, ever
			ctl.PublishSnapshot()
			select {
			case <-stop:
				return
			case <-time.After(5 * time.Second):
			}
		}
	case "console": // the screen and the API: its own process, a token for the operator's rows and nothing else
		vars, _ := p.NewFileVariables(filepath.Join(root, "config"))
		vars = vars.AsWriter("vmsconsole", append(vms.Spec.ACLConsole(), vms.RecSpec.ACLConsole()...)...) // the operator's rows of EVERY subsystem it fronts
		ctl := vms.NewVmsController(vars, objects, capacity, nil)
		res := vms.NewArchiveResource(spool, archive, 600, nil)
		srv, ln, err := vms.Serve(ctl, res, env("CONSOLE_HOST", "127.0.0.1")+":"+env("CONSOLE_PORT", "8080"), nil,
			p.NewSpecController(vms.RecSpec, vars, objects, capacity, nil, "")) // the recorder at /rec/…: the page's Record toggle
		if err != nil {
			log.Fatal(err)
		}
		log.Printf("console on %s", ln.Addr())
		<-stop
		srv.Close()
	case "resource": // the archive has no controller — it has a policy pass, a heartbeat, its HTTP, and the event database
		vars, _ := p.NewFileVariables(filepath.Join(root, "config"))
		ar := vms.NewArchiveResource(spool, archive, 600, nil)
		host, port := env("RESOURCE_HOST", "127.0.0.1"), env("RESOURCE_PORT", "8090")
		hostname, _ := os.Hostname()
		r := vms.NewVmsResource(ar, hostname, env("RESOURCE_URL", "http://"+host+":"+port), vars, objects, nil, nil)
		srv, ln, err := p.Serve(r, host+":"+port, vms.ResourceRoutes(ar))
		if err != nil {
			log.Fatal(err)
		}
		r.Heartbeat()
		log.Printf("resource %s on %s: restore %v", hostname, ln.Addr(), r.Restore())
		r.Database.Start() // a cache over THIS tree: rebuilt after restore, tailed every 3 s
		last := time.Time{}
		t := time.NewTicker(10 * time.Second)
	loop:
		for {
			select {
			case <-stop:
				break loop
			case <-t.C:
				r.Heartbeat()
				if time.Since(last) >= 600*time.Second {
					log.Printf("policy: %v", r.Pass())
					last = time.Now()
				}
			}
		}
		r.Database.Stop()
		srv.Close()
	default:
		log.Fatal("usage: vms worker|controller|recorder|reccontroller|console|resource")
	}
}

func env1(args []string) string {
	if len(args) > 1 {
		return args[1]
	}
	return ""
}
