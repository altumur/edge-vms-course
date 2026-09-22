package w2cplatform_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// The platform runs where the operator's box is, and that box is not always a Unix one.
//
// Two calls made it impossible: `syscall.Flock` under the store's lock and `syscall.Statfs` under the
// watermark. Neither is a wrong answer on Windows — neither EXISTS, so the package did not compile and
// nothing that imports it could run. A test that calls functions cannot see that: there are no functions.
// So this test compiles, and compiling is all it does.
//
// It is the cheapest honest check available here and it is worth being clear about its limit: a clean
// cross-compile says the calls exist and the types line up. It does not say LockFileEx takes the lock or
// that GetDiskFreeSpaceExW returns what we think. Nothing in this repository has run on Windows.
// The packages that CLAIM portability — and not `./...`, which is every package there happens to be.
// A media path is cgo over GStreamer and a vendor's driver pack: it cross-compiles for nothing, and a
// red line here would say that and nothing about the platform. So portability is a property a package
// DECLARES by being on this list, and adding one is a decision somebody makes on purpose.
var portable = []string{"./w2cplatform/...", "./vms/...", "./cmd/..."}

func TestThePlatformCompilesForEveryOperatingSystemItClaims(t *testing.T) {
	goBin, err := exec.LookPath("go")
	if err != nil {
		t.Skip("no go on PATH: this check needs the toolchain it is testing")
	}
	root, err := filepath.Abs("..")
	if err != nil {
		t.Fatal(err)
	}
	for _, goos := range []string{"windows", "linux", "darwin"} {
		if goos == runtime.GOOS {
			continue // the suite you are reading this in already proves that one
		}
		cmd := exec.Command(goBin, append([]string{"build", "-o", os.DevNull}, portable...)...)
		cmd.Dir = root
		cmd.Env = append(os.Environ(), "GOOS="+goos, "GOARCH=amd64", "CGO_ENABLED=0")
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("GOOS=%s does not build — a call that exists on this machine and not on that one:\n%s",
				goos, strings.TrimSpace(string(out)))
		}
	}
}

// …and the seam is one place, not an `if` at every call site. A portability seam that leaks into its
// callers is not a seam: it is the same branch repeated, and the day a third platform appears one of them
// is missed. Both of these are build-tagged FILES, so the branch is taken by the compiler and there is
// nothing to get wrong at run time.
func TestThePlatformSpecificCallsLiveInFilesOfTheirOwn(t *testing.T) {
	for _, pair := range [][2]string{{"flock_unix.go", "flock_windows.go"}, {"space_unix.go", "space_windows.go"}} {
		for _, f := range pair {
			b, err := os.ReadFile(f)
			if err != nil {
				t.Fatal(err)
			}
			if !strings.HasPrefix(string(b), "//go:build ") {
				t.Fatalf("%s: a per-OS file without a build tag is a file that builds everywhere", f)
			}
		}
	}
	// and nothing outside them reaches for a call that only one OS has
	ents, _ := os.ReadDir(".")
	for _, e := range ents {
		n := e.Name()
		if !strings.HasSuffix(n, ".go") || strings.Contains(n, "_unix") || strings.Contains(n, "_windows") || strings.HasSuffix(n, "_test.go") {
			continue
		}
		b, _ := os.ReadFile(n)
		for _, call := range []string{"syscall.Flock", "syscall.Statfs", "syscall.NewLazyDLL"} {
			if strings.Contains(string(b), call) {
				t.Fatalf("%s names %s: it belongs in a file with a build tag", n, call)
			}
		}
	}
}
