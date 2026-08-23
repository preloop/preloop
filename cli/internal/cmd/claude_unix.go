//go:build !windows

package cmd

import (
	"os/exec"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

func stdinByteReady(fd int, timeout time.Duration) (bool, error) {
	ms := int(timeout.Milliseconds())
	if timeout < 0 {
		ms = -1
	}
	fds := []unix.PollFd{{Fd: int32(fd), Events: unix.POLLIN}}
	n, err := unix.Poll(fds, ms)
	if err != nil {
		if err == unix.EINTR {
			return false, nil
		}
		return false, err
	}
	if n <= 0 {
		return false, nil
	}
	return fds[0].Revents&(unix.POLLIN|unix.POLLHUP|unix.POLLERR) != 0, nil
}

func consumeStdinByte(fd int) {
	_ = syscall.SetNonblock(fd, true)
	var b [1]byte
	_, _ = syscall.Read(fd, b[:])
	_ = syscall.SetNonblock(fd, false)
}

// claudeSidecarSysProcAttr detaches the sidecar daemon into its own process
// group so terminal signals (Ctrl+C) never reach it. It must NOT be applied
// to the TUI child: a new pgroup is a background group on the controlling
// tty, and the TUI stops on SIGTTIN the moment it reads stdin.
func claudeSidecarSysProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Setpgid: true}
}

func terminateClaudeProcess(cmd *exec.Cmd, wait <-chan error) error {
	// The TUI child shares the launcher's (foreground) process group. Never
	// signal that group: kill(-pgid) would SIGTERM the launcher itself. Group
	// signalling is only safe when the child truly has its own group.
	pgid, err := syscall.Getpgid(cmd.Process.Pid)
	if err == nil && pgid != syscall.Getpgrp() {
		_ = syscall.Kill(-pgid, syscall.SIGTERM)
	} else {
		_ = cmd.Process.Signal(syscall.SIGTERM)
	}
	if wait == nil {
		return cmd.Process.Kill()
	}
	select {
	case <-wait:
		return nil
	case <-time.After(3 * time.Second):
		return cmd.Process.Kill()
	}
}
