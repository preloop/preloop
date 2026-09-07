//go:build !windows

package cmd

import (
	"bytes"
	"os"
	"strconv"
	"syscall"
)

func isProcessAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	// kill(pid, 0) succeeds for zombies. Host-exec SIGKILLs a process group
	// whose parent cannot wait on grandchildren; Docker/CI PID 1 often leaves
	// those tasks as zombies. Treat Z/X as not running.
	if state, ok := linuxProcState(pid); ok {
		return state != 'Z' && state != 'X'
	}
	return syscall.Kill(pid, 0) == nil
}

func linuxProcState(pid int) (byte, bool) {
	data, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		return 0, false
	}
	return procStatState(data)
}

func procStatState(stat []byte) (byte, bool) {
	i := bytes.LastIndexByte(stat, ')')
	if i < 0 || i+2 >= len(stat) {
		return 0, false
	}
	return stat[i+2], true
}
