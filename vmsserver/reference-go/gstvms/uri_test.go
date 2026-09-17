package gstvms

// Lesson 2 — driverpacksrc. The URI resolution is pure; the element itself
// needs GStreamer (Track 2) and has no Go counterpart here.

import (
	"strings"
	"testing"
)

func TestURIResolutionAndTheRefusal(t *testing.T) {
	if p, err := Resolve("driverpack://file/lobby.mp4", "/data/media"); err != nil || p != "/data/media/lobby.mp4" {
		t.Fatal(p, err)
	}
	for _, bad := range []string{"rtsp://10.0.0.7/s", "driverpack://hikvision/10.0.0.7", "driverpack://file/../etc/passwd", "driverpack://file/"} {
		_, err := Resolve(bad, "")
		if err == nil {
			t.Fatal(bad)
		}
		if strings.Contains(bad, "hikvision") && !strings.Contains(err.Error(), "vendor driver") {
			t.Fatal(err)
		}
	}
}
