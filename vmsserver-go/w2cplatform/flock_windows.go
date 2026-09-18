//go:build windows

package w2cplatform

import (
	"os"
	"syscall"
	"unsafe"
)

// The Windows shape of flock(LOCK_EX), through kernel32 rather than
// golang.org/x/sys/windows: this module has no dependencies, and one lock call
// is not a reason for its first one.
//
// NewLazyDLL and not NewLazySystemDLL — the latter is x/sys's, not the standard
// library's. For kernel32 the difference does not bite: every Windows process
// has it loaded before a single line of Go runs, so LoadLibrary returns the
// module already in the process and never searches a path. Do NOT copy this
// line for a DLL that is not already loaded: there NewLazyDLL is a
// search-order hijack waiting to happen, and x/sys is the right answer.
var procLockFileEx = syscall.NewLazyDLL("kernel32.dll").NewProc("LockFileEx")

const lockfileExclusiveLock = 0x00000002

// flockFile takes an exclusive lock on f. Released when f closes: Windows drops
// a file's locks with its last handle, as flock does with its fd — which is
// what lets the caller keep saying `defer l.Close()` on both.
//
// Two differences from flock worth knowing, neither of which this store can
// see. One byte is locked, not the file: `lock` exists to BE a lock and nobody
// reads its contents, so a range of one is the whole of it. And LockFileEx is
// mandatory where flock is advisory — invisible here for the same reason.
func flockFile(f *os.File) error {
	var ol syscall.Overlapped
	// LockFileEx(handle, LOCKFILE_EXCLUSIVE_LOCK, reserved=0, bytesLow=1, bytesHigh=0, &overlapped).
	// No LOCKFILE_FAIL_IMMEDIATELY: this waits, as flock(LOCK_EX) does.
	r1, _, err := procLockFileEx.Call(f.Fd(), uintptr(lockfileExclusiveLock), 0, 1, 0, uintptr(unsafe.Pointer(&ol)))
	if r1 == 0 {
		return err
	}
	return nil
}
