//go:build !windows

package cmd

import (
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"golang.org/x/sys/unix"
)

func inventoryElevated() bool { return os.Geteuid() == 0 }
func localInventoryProfiles() ([]inventoryProfile, bool, error) {
	switch runtime.GOOS {
	case "darwin":
		return directoryInventoryProfiles("/Users", false)
	case "linux":
		return directoryInventoryProfiles("/home", true)
	default:
		return nil, false, errors.New("unsupported platform")
	}
}

// Open each component relative to an already-open directory descriptor.
// Metadata prechecks alone cannot prevent a parent from becoming a symlink.
func openInventoryConfig(root *os.Root, path string) (*os.File, error) {
	if !filepath.IsLocal(path) || filepath.Clean(path) != path || path == "." {
		return nil, os.ErrPermission
	}
	directory, err := root.Open(".")
	if err != nil {
		return nil, err
	}
	defer func() { directory.Close() }()
	components := strings.Split(path, string(filepath.Separator))
	for i, component := range components {
		flags := os.O_RDONLY | unix.O_NOFOLLOW | unix.O_CLOEXEC | unix.O_NONBLOCK
		if i < len(components)-1 {
			flags |= unix.O_DIRECTORY
		}
		fd, err := unix.Openat(int(directory.Fd()), component, flags, 0)
		if err != nil {
			return nil, err
		}
		opened := os.NewFile(uintptr(fd), component)
		if i == len(components)-1 {
			var info unix.Stat_t
			if err := unix.Fstat(fd, &info); err != nil {
				opened.Close()
				return nil, err
			}
			// A known config name must not alias another profile's config or a
			// credential file through a hard link, or block on a special file.
			if info.Mode&unix.S_IFMT != unix.S_IFREG || info.Nlink != 1 {
				opened.Close()
				return nil, os.ErrPermission
			}
			return opened, nil
		}
		directory.Close()
		directory = opened
	}
	return nil, os.ErrPermission
}
