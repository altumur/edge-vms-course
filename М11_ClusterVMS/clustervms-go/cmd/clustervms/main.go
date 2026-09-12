// cmd/clustervms — the recorder under a scheduler, in Go. The same environment
// the Python version reads (NODE_ID, CONFIG_OBJECT, NOMAD_ADDR/NOMAD_TOKEN,
// OBJECT_STORE_URL, LEASE_TTL, …), the same Variables, the same objects:
// a Go recorder and a Python recorder are interchangeable allocations of one job.
package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"clustervms/cluster"
)

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	settings := cluster.SettingsFromEnv()
	vars := cluster.NewNomadVariables()
	ident, err := cluster.FromEnvironment(vars)
	if err != nil {
		log.Fatal(err)
	}
	if ident.ColumnKey != nil { // М9's key, delivered by the cluster, not from a backup
		os.MkdirAll(filepath.Dir(settings.ColumnKeyFile), 0o700)
		if err := os.WriteFile(settings.ColumnKeyFile, ident.ColumnKey, 0o600); err != nil {
			log.Fatal(err)
		}
	}
	objects, err := cluster.OpenStore(settings.ObjectStoreURL)
	if err != nil {
		log.Fatal(err)
	}
	store, closeStore, err := openStore(ctx, settings.DatabaseURL)
	if err != nil {
		log.Fatal(err)
	}
	defer closeStore()
	// The actuator is where a Go controller stops being a host of pipelines
	// and becomes a client of a media worker (М9 Lesson 9). Until that
	// worker exists, the fake records nothing and the rest of the recorder is real.
	host := cluster.NewClusterAppHost(settings, store, vars, objects, ident, cluster.NewFakeActuator(), nil, nil)
	if err := host.Run(ctx, true); err != nil {
		log.Fatal(err)
	}
}
