// vms worker|controller|recorder|reccontroller|console|resource — the processes, on one box.
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

	"vmsserver/vms"
	p "vmsserver/w2cplatform"
)

// Housekeeping the console owns BECAUSE THE ACL SAYS SO. `<sub>/blobs/*` is the console's to write
// (Lesson 27), so it is the console's to collect; the controller could not delete a blob if it wanted to,
// and that is the right way round — the process that creates a thing is the one that can be trusted to
// know when nothing names it.
//
// Its own loop and its own log line. That is Lesson 28 applied before the same mistake is made twice: a
// sweep that fails inside somebody else's error handling would be reported as somebody else's failure,
// and the consequence — blobs accumulating with nothing reclaiming them — is the kind that shows up as a
// disk full a year later.
func sweepLoop(stop <-chan struct{}, controllers ...*p.SpecController) {
	for {
		for _, c := range controllers {
			r, err := c.SweepBlobs(0, 0)
			if err != nil {
				log.Printf("the blob sweep failed in %s — nothing is reclaiming its blobs: %v", c.Spec.Name, err)
				continue
			}
			if r.Deleted > 0 {
				log.Printf("swept %d blob(s) nothing names in %s", r.Deleted, c.Spec.Name)
			}
		}
		select {
		case <-stop:
			return
		case <-time.After(60 * time.Second):
		}
	}
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// openVars is the store seam, said once: a URL and an identity, never a class
// name. Whichever backend answers that URL, the loops below are the same.
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
	case "reccontroller": // count = 1, the only writer of rec placement: recordings onto recorders whose resource answers, beside the camera's worker when there is room
		vars := openVars(root, "reccontroller", vms.RecSpec.ACLController()...)
		ctl := p.NewSpecController(vms.RecSpec, vars, objects, capacity, nil, "")
		for {
			if _, err := ctl.EnsurePlaced(nil); err != nil {
				log.Println("rec placement pass failed:", err)
			}
			ctl.Redistribute(nil)
			if err := ctl.PublishSnapshot(); err != nil {
				log.Println("publishing the rec snapshot failed — the layer above is now reading a stale copy:", err)
			}
			select {
			case <-stop:
				return
			case <-time.After(5 * time.Second):
			}
		}
	case "controller": // count = 1, the only writer of placement; no HTTP — nothing asks it anything
		vars := openVars(root, "vmscontroller", vms.Spec.ACLController()...)
		ctl := vms.NewVmsController(vars, objects, capacity, nil)
		for {
			if _, err := ctl.EnsurePlaced(nil); err != nil { // deleted rows unplaced; new cameras onto the workers it sees
				log.Println("placement pass failed:", err)
			}
			ctl.Redistribute(nil) // cameras of a RELEASED slot (scale-in) onto the rest; nothing else, ever
			// Reported on its OWN line, and this is not tidiness. Publishing is the last thing the pass
			// does, so a failure here used to be swallowed entirely — and in the Python port, where the
			// four calls shared one try, it came out as "placement pass failed", naming the one thing that
			// had not failed. Two jobs, two failures, two sentences. The age of what the layer above reads
			// is on /metrics as <sub>_snapshot_age_seconds, from the store rather than from this process.
			if err := ctl.PublishSnapshot(); err != nil {
				log.Println("publishing the snapshot failed — the layer above is now reading a stale copy:", err)
			}
			select {
			case <-stop:
				return
			case <-time.After(5 * time.Second):
			}
		}
	case "console": // the screen and the API: its own process, a token for the operator's rows and nothing else
		vars := openVars(root, "console", append(vms.Spec.ACLConsole(), vms.RecSpec.ACLConsole()...)...) // the operator's rows of EVERY subsystem it fronts
		ctl := vms.NewVmsController(vars, objects, capacity, nil)
		recCtl := p.NewSpecController(vms.RecSpec, vars, objects, capacity, nil, "") // the recorder at /rec/…: the page's Record toggle
		res := vms.NewArchiveResource(spool, archive, 600, nil)
		srv, ln, err := vms.Serve(ctl, res, env("CONSOLE_HOST", "127.0.0.1")+":"+env("CONSOLE_PORT", "8080"), nil, recCtl)
		if err != nil {
			log.Fatal(err)
		}
		log.Printf("console on %s", ln.Addr())
		go sweepLoop(stop, ctl.SpecController, recCtl)
		<-stop
		srv.Close()
	case "resource": // the archive has no controller — it has a policy pass, a heartbeat, its HTTP, and the event database
		vars := openVars(root, "") // the resource writes no configuration: read-only is its whole identity
		ar := vms.NewArchiveResource(spool, archive, 600, nil)
		host, port := env("RESOURCE_HOST", "127.0.0.1"), env("RESOURCE_PORT", "8090")
		hostname := p.ServerOfEnv(nil, "")
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
