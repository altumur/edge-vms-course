// cmd/baseline — a controller-shaped Go process at idle: the reconciler,
// a status map, a JSON encoder and an HTTP listener, doing nothing. Prints
// its own proportional set size (PSS) after settling. This is the "B" of
// М9 Lesson 7 for the CONTROLLER, without GStreamer — GStreamer costs the
// same in every language and is measured separately by the probe.
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"

	"recorder/reconciler"
)

type store struct{ rows []reconciler.Camera }

func (s *store) Desired() []reconciler.Camera { return s.rows }

func pssKB() int {
	f, err := os.Open("/proc/self/smaps_rollup")
	if err != nil {
		return -1
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if strings.HasPrefix(sc.Text(), "Pss:") {
			fields := strings.Fields(sc.Text())
			n, _ := strconv.Atoi(fields[1])
			return n
		}
	}
	return -1
}

func main() {
	s := &store{}
	for i := int64(1); i <= 50; i++ {
		s.rows = append(s.rows, reconciler.Camera{ID: i, Enabled: true, Revision: 1})
	}
	r := reconciler.New(s, func(string, reconciler.Camera) bool { return true })
	r.Reconcile(0)
	mux := http.NewServeMux()
	mux.HandleFunc("/status", func(w http.ResponseWriter, _ *http.Request) {
		json.NewEncoder(w).Encode(r.Status())
	})
	go http.ListenAndServe("127.0.0.1:0", mux)
	time.Sleep(500 * time.Millisecond)
	runtime.GC()
	fmt.Printf("go controller idle: PSS %d kB, %d goroutines\n", pssKB(), runtime.NumGoroutine())
}
