//go:build windows

package cmd

import (
	"os/exec"
	"syscall"
)

func hostExecSysProcAttr() *syscall.SysProcAttr {
	return nil
}

func killRunnerJobProcess(cmd *exec.Cmd) {
	if cmd == nil || cmd.Process == nil {
		return
	}
	_ = cmd.Process.Kill()
}
