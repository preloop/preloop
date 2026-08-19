//go:build windows

package cmd

import (
	"errors"
	"os/exec"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

var procPeekNamedPipe = windows.NewLazySystemDLL("kernel32.dll").NewProc("PeekNamedPipe")

// stdinHeld keeps a byte that the EOF probe consumed if data arrived
// between PeekNamedPipe and the non-blocking ReadFile.
var stdinHeld struct {
	has bool
	b   byte
}

func peekPipeAvail(handle windows.Handle) (uint32, error) {
	var avail uint32
	r1, _, err := procPeekNamedPipe.Call(
		uintptr(handle),
		0,
		0,
		0,
		uintptr(unsafe.Pointer(&avail)),
		0,
	)
	if r1 == 0 {
		if err != nil {
			return 0, err
		}
		return 0, syscall.EINVAL
	}
	return avail, nil
}

func pipeEOF(handle windows.Handle) bool {
	// PeekNamedPipe reports a closed write end as success with avail==0,
	// not ERROR_BROKEN_PIPE. A non-blocking 1-byte read distinguishes EOF
	// from an empty live pipe (Unix poll surfaces POLLHUP the same way).
	nowait := uint32(windows.PIPE_NOWAIT)
	if err := windows.SetNamedPipeHandleState(handle, &nowait, nil, nil); err != nil {
		return false
	}
	defer func() {
		wait := uint32(windows.PIPE_WAIT)
		_ = windows.SetNamedPipeHandleState(handle, &wait, nil, nil)
	}()
	var n uint32
	var buf [1]byte
	err := windows.ReadFile(handle, buf[:], &n, nil)
	if n > 0 {
		stdinHeld.has = true
		stdinHeld.b = buf[0]
		return true
	}
	return errors.Is(err, windows.ERROR_BROKEN_PIPE) ||
		errors.Is(err, windows.ERROR_PIPE_NOT_CONNECTED)
}

func stdinByteReady(fd int, timeout time.Duration) (bool, error) {
	handle := windows.Handle(fd)
	infinite := timeout < 0
	deadline := time.Now().Add(timeout)

	var mode uint32
	if err := windows.GetConsoleMode(handle, &mode); err == nil {
		ms := uint32(windows.INFINITE)
		if !infinite {
			ms = uint32(timeout.Milliseconds())
			if ms == 0 && timeout > 0 {
				ms = 1
			}
		}
		ev, err := windows.WaitForSingleObject(handle, ms)
		if err != nil {
			return false, err
		}
		return ev == windows.WAIT_OBJECT_0, nil
	}

	// Anonymous pipes are always signaled for WaitForSingleObject, so
	// that wait cannot mean "byte available". Peek instead.
	for {
		avail, err := peekPipeAvail(handle)
		if err != nil {
			if errors.Is(err, windows.ERROR_BROKEN_PIPE) ||
				errors.Is(err, windows.ERROR_PIPE_NOT_CONNECTED) {
				return true, nil
			}
			return false, err
		}
		if avail > 0 {
			return true, nil
		}
		if pipeEOF(handle) {
			return true, nil
		}
		if !infinite && !time.Now().Before(deadline) {
			return false, nil
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func consumeStdinByte(fd int) {
	if stdinHeld.has {
		stdinHeld.has = false
		return
	}
	var b [1]byte
	_, _ = syscall.Read(syscall.Handle(fd), b[:])
}

func claudeSysProcAttr() *syscall.SysProcAttr {
	return nil
}

func terminateClaudeProcess(cmd *exec.Cmd, wait <-chan error) error {
	return cmd.Process.Kill()
}
