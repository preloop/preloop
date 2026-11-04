//go:build windows

package cmd

import (
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	procGetLastInputInfo = windows.NewLazySystemDLL("user32.dll").NewProc("GetLastInputInfo")
	procGetTickCount     = windows.NewLazySystemDLL("kernel32.dll").NewProc("GetTickCount")
)

type lastInputInfo struct {
	cbSize uint32
	dwTime uint32
}

// platformOSIdleSeconds uses GetLastInputInfo for the interactive session
// the runner's logon task runs in.
func platformOSIdleSeconds() (int, bool) {
	info := lastInputInfo{cbSize: uint32(unsafe.Sizeof(lastInputInfo{}))}
	if ok, _, _ := procGetLastInputInfo.Call(uintptr(unsafe.Pointer(&info))); ok == 0 {
		return 0, false
	}
	now, _, _ := procGetTickCount.Call()
	// Both are 32-bit millisecond tick counts; unsigned subtraction handles
	// the 49.7-day wrap.
	return int((uint32(now) - info.dwTime) / 1000), true
}
