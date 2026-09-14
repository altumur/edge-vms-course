// clustervms worker|controller|console|resource — the cluster's jobs, in Go.
//
//	NOMAD_ADDR, NOMAD_TOKEN         the task's own workload identity (Variables)
//	OBJECT_STORE_URL                variables://objects (default) · s3+http://minio:9000/vms?region=us-east-1 · file:///path
//	NOMAD_ALLOC_INDEX               the worker's slot: w-<index>
//	NOMAD_NODE_NAME, NOMAD_META_labels, CAPACITY
//	SPOOL, ARCHIVE                  this server's resource
//	RESOURCE_URL                    how peers reach this resource (http://<host>:8090)
//	CLUSTER                         the name the snapshot carries; CONSOLE_PORT
package main

import (
	"log"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"clustervms/cluster"
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
	if len(os.Args) < 2 {
		log.Fatal("usage: clustervms worker|controller|console|resource")
	}
	stop := make(chan struct{})
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
	go func() { <-sig; close(stop) }()
	vars := cluster.NewNomadVariables()
	objects, err := cluster.OpenStore(env("OBJECT_STORE_URL", "variables://objects"))
	if err != nil {
		log.Fatal(err)
	}
	spool, archive := env("SPOOL", "/data/spool"), env("ARCHIVE", "/data/archive")
	server := env("NOMAD_NODE_NAME", "")
	if server == "" {
		server, _ = os.Hostname()
	}
	every := func(d time.Duration, f func()) {
		for {
			f()
			select {
			case <-stop:
				return
			case <-time.After(d):
			}
		}
	}
	switch os.Args[1] {
	case "worker":
		res := vms.NewArchiveResource(spool, archive, 600, nil)
		for _, pth := range res.ClosedInSpool(30, p.Wall()()) {
			res.Promote(pth, 0)
		}
		log.Println("no GStreamer in the Go port: the fake actuator records nothing")
		w, err := cluster.NewClusterWorker(vars, objects, vms.NewFakeActuator(), nil, vms.VmsWorkerOptions{ArchiveRoot: archive})
		if err != nil {
			log.Fatal(err)
		}
		log.Printf("worker %s (alloc %s) on %s claimed its slot", w.Name, w.Alloc, w.Server)
		w.Run(2*time.Second, stop)
	case "controller": // count = 1, the only writer of placement; no HTTP — nothing asks it anything
		capacity, _ := strconv.Atoi(env("CAPACITY", "50"))
		ctl := cluster.NewClusterController(vars, objects, capacity, nil, env("CLUSTER", "cluster-a"))
		every(5*time.Second, func() {
			if _, err := ctl.EnsurePlaced(nil); err != nil {
				log.Println("placement:", err)
			}
			ctl.Redistribute(nil)
			ctl.PublishSnapshot()
		})
	case "console": // a system job, one per server: the page and the API; no event database of its own — /events asks the resources
		capacity, _ := strconv.Atoi(env("CAPACITY", "50"))
		ctl := cluster.NewClusterController(vars, objects, capacity, nil, env("CLUSTER", "cluster-a"))
		srv, ln, err := cluster.Serve(ctl, "0.0.0.0:"+env("CONSOLE_PORT", "8080"), cluster.ConsoleOptions{ArchiveRoot: archive})
		if err != nil {
			log.Fatal(err)
		}
		log.Printf("console on %s", ln.Addr())
		<-stop
		srv.Close()
	case "resource":
		ar := vms.NewArchiveResource(spool, archive, 600, nil)
		r := cluster.ClusterResource(ar, server, env("RESOURCE_URL", "http://"+server+":8090"), vars, objects, nil, nil)
		if _, _, err := p.Serve(r, "0.0.0.0:"+env("RESOURCE_PORT", "8090"), cluster.VmsRoutes(ar)); err != nil {
			log.Fatal(err)
		}
		r.Heartbeat()
		log.Printf("resource %s: restore %v", server, r.Restore())
		r.Database.Start() // a cache over MY tree: rebuilt after restore, tailed every 3 s
		last := time.Time{}
		every(10*time.Second, func() {
			r.Heartbeat()
			if time.Since(last) >= 600*time.Second {
				log.Printf("policy: %v", r.Pass())
				last = time.Now()
			}
		})
		r.Database.Stop()
	default:
		log.Fatal("usage: clustervms worker|controller|console|resource")
	}
}
