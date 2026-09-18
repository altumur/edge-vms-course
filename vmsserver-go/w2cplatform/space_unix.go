//go:build !windows

package w2cplatform

import "syscall"

// DiskSpace: (total, free) bytes of the filesystem root is on. f_bavail and not f_bfree, because reserved
// blocks are not ours to spend. A test cannot fill a disk, so the probe is a seam (Resource.SpaceProbe).
func DiskSpace(root string) (int64, int64) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(root, &st); err != nil {
		return 0, 0
	}
	return int64(st.Blocks) * int64(st.Bsize), int64(st.Bavail) * int64(st.Bsize)
}
