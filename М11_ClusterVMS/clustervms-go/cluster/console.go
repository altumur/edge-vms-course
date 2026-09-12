package cluster

// Lesson 5 — the cluster's questions, on the console.
//
//	GET /cluster/node               who am I, which epoch, lease, replicated, failover
//	GET /cluster/directory          every recorder's holdings, one scan
//	GET /cluster/where/{camera_id}  where is camera 7
//	GET /metrics                    node_failover_seconds, node_epoch_conflicts, …

import (
	"encoding/json"
	"log"
	"net/http"
	"strconv"
	"strings"
)

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v)
}

func ConsoleMux(h *ClusterAppHost) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		w.Write([]byte(RenderMetrics(h)))
	})
	mux.HandleFunc("/cluster/node", func(w http.ResponseWriter, _ *http.Request) {
		out := map[string]any{"node": h.Identity.recorder, "epoch": h.Settings.Epoch, "replicated": h.ReplicatedNow,
			"failover": h.Failover, "restore": nil, "lease_seconds_left": nil, "fenced": nil}
		if h.Restore != nil {
			out["restore"] = h.Restore.State
		}
		if h.Lease != nil {
			out["lease_seconds_left"], out["fenced"] = h.Lease.SecondsLeft(), h.Lease.Fenced
		}
		writeJSON(w, out)
	})
	mux.HandleFunc("/cluster/directory", func(w http.ResponseWriter, _ *http.Request) {
		scan, err := h.Directory.Scan(false)
		if err != nil {
			http.Error(w, err.Error(), 503)
			return
		}
		writeJSON(w, scan)
	})
	mux.HandleFunc("/cluster/where/", func(w http.ResponseWriter, r *http.Request) {
		id, err := strconv.ParseInt(strings.TrimPrefix(r.URL.Path, "/cluster/where/"), 10, 64)
		if err != nil {
			http.Error(w, "camera id", 400)
			return
		}
		node, err := h.Directory.Where(id)
		if err != nil {
			http.Error(w, err.Error(), 500)
			return
		}
		if node == "" {
			http.Error(w, "camera "+strconv.FormatInt(id, 10)+" is on no recorder this cluster knows", 404)
			return
		}
		writeJSON(w, map[string]any{"camera": id, "node": node, "here": node == h.Identity.recorder})
	})
	return mux
}

func ServeConsole(h *ClusterAppHost, addr string) *http.Server {
	srv := &http.Server{Addr: addr, Handler: ConsoleMux(h)}
	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Printf("console: %v", err)
		}
	}()
	return srv
}
