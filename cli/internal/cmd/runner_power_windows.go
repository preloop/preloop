//go:build windows

package cmd

import (
	"sync"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// runnerWindowsSleepReleaseAfter bounds how long the suspend callback
// blocks. Windows gives applications about 2 s to handle PBT_APMSUSPEND.
const runnerWindowsSleepReleaseAfter = 1500 * time.Millisecond

// deviceNotifyCallback is DEVICE_NOTIFY_CALLBACK: Recipient points at a
// DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS instead of a window handle, so no
// message loop is needed and the runner can be a background service.
const deviceNotifyCallback = 2

var (
	powrprof                                     = windows.NewLazySystemDLL("powrprof.dll")
	procPowerRegisterSuspendResumeNotification   = powrprof.NewProc("PowerRegisterSuspendResumeNotification")
	procPowerUnregisterSuspendResumeNotification = powrprof.NewProc("PowerUnregisterSuspendResumeNotification")

	windowsPowerOnce     sync.Once
	windowsPowerCallback uintptr

	windowsPowerMu     sync.Mutex
	windowsPowerBridge *callbackPowerBridge
	// windowsPowerParams must stay reachable while registered: Windows
	// keeps the pointer.
	windowsPowerParams *deviceNotifySubscribeParameters
)

type deviceNotifySubscribeParameters struct {
	callback uintptr
	context  uintptr
}

// windowsPowerEvent is the DEVICE_NOTIFY_CALLBACK_ROUTINE.
func windowsPowerEvent(_ uintptr, eventType uintptr, _ uintptr) uintptr {
	windowsPowerMu.Lock()
	bridge := windowsPowerBridge
	windowsPowerMu.Unlock()
	if bridge != nil {
		bridge.handleWindowsPowerEvent(uint32(eventType))
	}
	return 0 // ERROR_SUCCESS
}

// startPlatformPowerSource registers for suspend and resume notifications
// with PowerRegisterSuspendResumeNotification (Windows 8 and later). On
// failure, sleep reads as offline as before. Modern Standby (S0ix) hosts
// deliver PBT_APMSUSPEND only when the system actually enters sleep, which
// can be some minutes after the lid closes.
func startPlatformPowerSource(events chan<- runnerPowerEvent, _ <-chan struct{}, epoch *runnerPowerEpoch) func() {
	if procPowerRegisterSuspendResumeNotification.Find() != nil {
		return func() {}
	}
	windowsPowerOnce.Do(func() {
		windowsPowerCallback = windows.NewCallback(windowsPowerEvent)
	})
	windowsPowerMu.Lock()
	defer windowsPowerMu.Unlock()
	if windowsPowerBridge != nil {
		return func() {}
	}
	params := &deviceNotifySubscribeParameters{callback: windowsPowerCallback}
	var handle uintptr
	r, _, _ := procPowerRegisterSuspendResumeNotification.Call(
		deviceNotifyCallback,
		uintptr(unsafe.Pointer(params)),
		uintptr(unsafe.Pointer(&handle)),
	)
	if r != 0 {
		return func() {}
	}
	windowsPowerParams = params
	windowsPowerBridge = &callbackPowerBridge{
		events: events, epoch: epoch, releaseAfter: runnerWindowsSleepReleaseAfter,
	}
	var once sync.Once
	return func() {
		once.Do(func() {
			_, _, _ = procPowerUnregisterSuspendResumeNotification.Call(handle)
			windowsPowerMu.Lock()
			windowsPowerBridge = nil
			windowsPowerParams = nil
			windowsPowerMu.Unlock()
		})
	}
}
