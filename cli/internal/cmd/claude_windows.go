//go:build windows

package cmd

import (
	"os/exec"
	"syscall"
	"time"

	"golang.org/x/sys/windows"
)

func stdinByteReady(fd int, timeout time.Duration) (bool, error) {
	ms := uint32(timeout.Milliseconds())
	if timeout < 0 {
		ms = windows.INFINITE
	}
	ev, err := windows.WaitForSingleObject(windows.Handle(fd), ms)
	if err != nil {
		return false, err
	}
	return ev == windows.WAIT_OBJECT_0, nil
}

func consumeStdinByte(fd int) {
	var b [1]byte
	_, _ = syscall.Read(fd, b[:])
}

func claudeSysProcAttr() *syscall.SysProcAttr {
	return nil
}

func terminateClaudeProcess(cmd *exec.Cmd, wait <-chan error) error {
	return cmd.Process.Kill()
}
