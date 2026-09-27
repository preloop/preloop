//go:build windows

package cmd

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
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
//
// taskkill addresses the job by PID, and Windows recycles PIDs once the last
// handle to an exited process closes, which Process.Wait does. So a handle
// is opened on the PID first and the job is then confirmed still unwaited:
// Go holds its own handle until Wait returns, so the PID still named the job
// when the new handle was opened, and that handle pins it for the rest of
// the call. Once the job has been waited (the deferred cleanup after a
// normal exit) taskkill never runs, so an unrelated process that inherited
// the PID can never be killed.
func killRunnerJobProcess(cmd *exec.Cmd) {
	if cmd == nil || cmd.Process == nil {
		return
	}
	pin, err := syscall.OpenProcess(
		syscall.SYNCHRONIZE, false, uint32(cmd.Process.Pid),
	)
	if err != nil {
		_ = cmd.Process.Kill()
		return
	}
	defer syscall.CloseHandle(pin)
	if !runnerJobProcessUnwaited(cmd.Process.Signal(syscall.Signal(0))) {
		return
	}
	if err := runWindowsTaskkill(cmd.Process.Pid); err != nil {
		_ = cmd.Process.Kill()
	}
}

// runnerJobProcessUnwaited interprets a probe Signal on Windows. Once Wait
// has returned, os marks the process released (Wait uses statusReleased, not
// statusDone, "for compatibility") and Signal reports syscall.EINVAL; a
// process known to be finished reports os.ErrProcessDone. Either means the
// handle that pinned the PID is gone. A live, unwaited process answers
// signal 0 with EWINDOWS (unsupported signal) today, or nil should os ever
// support it.
func runnerJobProcessUnwaited(signalErr error) bool {
	return !errors.Is(signalErr, os.ErrProcessDone) &&
		!errors.Is(signalErr, syscall.EINVAL)
}

// runWindowsTaskkill kills the process tree rooted at pid. A variable so the
// PID-reuse guard is testable without killing anything.
var runWindowsTaskkill = func(pid int) error {
	taskkill := exec.Command(
		windowsTaskkillPath(), "/T", "/F", "/PID", strconv.Itoa(pid),
	)
	taskkill.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	return taskkill.Run()
}

// windowsTaskkillPath names taskkill.exe under the system directory, so a
// taskkill earlier on the runner's PATH (for example in a user-writable npm
// or tool directory) is never the program that halts a job.
func windowsTaskkillPath() string {
	root := os.Getenv("SystemRoot")
	if root == "" {
		root = `C:\Windows`
	}
	return filepath.Join(root, "System32", "taskkill.exe")
}
