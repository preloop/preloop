//go:build windows

package cmd

import (
	"os/exec"
	"syscall"
)

func claudeSysProcAttr() *syscall.SysProcAttr {
	return nil
}

func terminateClaudeProcess(cmd *exec.Cmd) error {
	return cmd.Process.Kill()
}
