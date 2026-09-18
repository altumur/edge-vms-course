//go:build !windows

package w2cplatform

import (
	"os"
	"syscall"
)

// flockFile takes an exclusive advisory lock on f, released when f closes.
func flockFile(f *os.File) error {
	return syscall.Flock(int(f.Fd()), syscall.LOCK_EX)
}
