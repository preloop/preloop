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

func claudeSysProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Setpgid: true}
}

func terminateClaudeProcess(cmd *exec.Cmd, wait <-chan error) error {
	pgid, err := syscall.Getpgid(cmd.Process.Pid)
	if err == nil {
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
