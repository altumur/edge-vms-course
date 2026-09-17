// Package gstvms holds what needs a media stack in Python; here only the
// pure part — driverpack://file/<name> -> a path under MEDIA_DIR.
package gstvms

import (
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
)

// Resolve: anything but driverpack://file/<name> is the real DriverPack's.
func Resolve(uri, mediaDir string) (string, error) {
	if mediaDir == "" {
		mediaDir = os.Getenv("MEDIA_DIR")
		if mediaDir == "" {
			mediaDir = "/data/media"
		}
	}
	u, err := url.Parse(uri)
	if err != nil || u.Scheme != "driverpack" {
		return "", fmt.Errorf("not a driverpack URI: %s", uri)
	}
	if u.Host != "file" {
		return "", fmt.Errorf("driverpack://%s/… names a vendor driver; this course ships only driverpack://file/<name>", u.Host)
	}
	name := strings.TrimLeft(u.Path, "/")
	if name == "" || strings.Contains(name, "/") || strings.Contains(name, "..") {
		return "", fmt.Errorf("bad media name in %s", uri)
	}
	return filepath.Join(mediaDir, name), nil
}
