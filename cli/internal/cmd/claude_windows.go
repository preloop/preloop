//go:build windows

package cmd

import (
	"os"
	"os/exec"
	"syscall"
	"time"
)

func cancelStdinRead(fd int) {
	_ = os.Stdin.SetReadDeadline(time.Now())
}

func restoreStdinRead(fd int) {
	_ = os.Stdin.SetReadDeadline(time.Time{})
}

func claudeSysProcAttr() *syscall.SysProcAttr {
	return nil
}

func terminateClaudeProcess(cmd *exec.Cmd, wait <-chan error) error {
	return cmd.Process.Kill()
}
