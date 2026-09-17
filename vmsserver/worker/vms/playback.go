package vms

// The holder's SECOND surface, and the reason it is HTTP and not the RTSP
// fan-out: a browser has to seek inside what it gets, and the recorder fetches
// ranges through the same door (Lesson 16). The console proxies to it; nothing
// about a device leaves this process except bytes and the summary.
//
//	GET /playback/<cam>?from&to   the device's own footage for that range
//	GET /devices                  what is held, and what channels are not imported yet

import (
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"strconv"
	"strings"
)

func sendJSON(w http.ResponseWriter, status int, body any) {
	b, _ := json.Marshal(body)
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", strconv.Itoa(len(b)))
	w.WriteHeader(status)
	w.Write(b)
}

// PlaybackHandler is the door as a handler, so a test can drive it without a port.
func (w *VmsWorker) PlaybackHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(rw http.ResponseWriter, req *http.Request) {
		if req.Method != "GET" {
			sendJSON(rw, 405, map[string]any{"detail": "read only", "error": "method not allowed"})
			return
		}
		if req.URL.Path == "/devices" {
			sendJSON(rw, 200, w.DeviceStatus())
			return
		}
		if !strings.HasPrefix(req.URL.Path, "/playback/") {
			sendJSON(rw, 404, map[string]any{"detail": "no such route", "error": "no such path"})
			return
		}
		q := req.URL.Query()
		from, _ := strconv.ParseFloat(q.Get("from"), 64)
		to := 1e12
		if v, err := strconv.ParseFloat(q.Get("to"), 64); err == nil {
			to = v
		}
		cam := req.URL.Path[strings.LastIndex(req.URL.Path, "/")+1:]
		data, err := w.Playback(cam, from, to)
		switch {
		case errors.Is(err, ErrNoDeviceArchive):
			sendJSON(rw, 404, map[string]any{"detail": err.Error(), "error": "no device archive"})
			return
		case errors.Is(err, ErrDeviceBusy): // the DEVICE's ceiling, not ours
			sendJSON(rw, 503, map[string]any{"detail": err.Error(), "error": "device busy"})
			return
		case err != nil:
			sendJSON(rw, 500, map[string]any{"detail": err.Error(), "error": "playback failed"})
			return
		}
		rw.Header().Set("Content-Type", "video/mp4")
		rw.Header().Set("Content-Length", strconv.Itoa(len(data)))
		rw.WriteHeader(200)
		rw.Write(data)
	})
	return mux
}

// ServePlayback opens the door on addr (":0" in a test: read the listener).
func (w *VmsWorker) ServePlayback(addr string) (*http.Server, net.Listener, error) {
	if addr == "" {
		addr = "127.0.0.1:" + strconv.Itoa(PlaybackPort)
	}
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, nil, err
	}
	srv := &http.Server{Handler: w.PlaybackHandler()}
	go srv.Serve(ln)
	return srv, ln, nil
}
