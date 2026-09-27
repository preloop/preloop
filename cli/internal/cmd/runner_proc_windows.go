//go:build windows

package cmd

import (
	"os/exec"
	"strconv"
	"syscall"
)

// hostExecSysProcAttr starts a host job in its own process group so a
// console Ctrl+C aimed at the runner is not broadcast into the job, which is
// the same ownership Setpgid establishes on Unix. The job may run headless
// under a scheduled task, so no console window is created for it.
func hostExecSysProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{
		CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP,
		HideWindow:    true,
	}
}

// killRunnerJobProcess kills the job and every descendant. Windows has no
// process-group SIGKILL; taskkill /T walks the process tree, which matters
// because agent CLIs spawn node and tool children of their own. If taskkill
// is unavailable or the tree is already gone, the direct process is killed.
func killRunnerJobProcess(cmd *exec.Cmd) {
	if cmd == nil || cmd.Process == nil {
		return
	}
	taskkill := exec.Command(
		"taskkill", "/T", "/F", "/PID", strconv.Itoa(cmd.Process.Pid),
	)
	taskkill.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	if err := taskkill.Run(); err != nil {
		_ = cmd.Process.Kill()
	}
}
