//go:build !windows

package cmd

import (
	"os"
	"os/exec"
	"syscall"
	"time"
)

func cancelStdinRead(fd int) {
	_ = os.Stdin.SetReadDeadline(time.Now())
	_ = syscall.SetNonblock(fd, true)
}

func restoreStdinRead(fd int) {
	_ = syscall.SetNonblock(fd, false)
	_ = os.Stdin.SetReadDeadline(time.Time{})
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
