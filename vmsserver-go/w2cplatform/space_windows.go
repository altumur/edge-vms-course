//go:build windows

package w2cplatform

import (
	"syscall"
	"unsafe"
)

// GetDiskFreeSpaceExW is the Windows answer to statfs: bytes rather than
// blocks, and — the part that matters — its FIRST out-parameter is what is
// available to the caller, a quota's share of the volume rather than the
// volume's own free space. That is the same choice `f_bavail` makes on Unix
// (what is ours to spend, not what exists), so the two return one meaning and
// the watermark above them needs no `if`.
//
// See flock_windows.go for why this is NewLazyDLL and why that is safe here.
var procGetDiskFreeSpaceEx = syscall.NewLazyDLL("kernel32.dll").NewProc("GetDiskFreeSpaceExW")

// DiskSpace: (total, free) bytes of the volume root is on. (0, 0) on failure,
// as on Unix — a probe that cannot answer says so, and Relieve treats a total
// of zero as "no opinion" rather than as a full disk.
func DiskSpace(root string) (int64, int64) {
	p, err := syscall.UTF16PtrFromString(root)
	if err != nil {
		return 0, 0
	}
	var freeToCaller, total, free uint64
	r1, _, _ := procGetDiskFreeSpaceEx.Call(uintptr(unsafe.Pointer(p)),
		uintptr(unsafe.Pointer(&freeToCaller)), uintptr(unsafe.Pointer(&total)), uintptr(unsafe.Pointer(&free)))
	if r1 == 0 {
		return 0, 0
	}
	return int64(total), int64(freeToCaller)
}
