//go:build darwin

package cmd

import (
	"runtime"
	"sync"
	"time"

	"github.com/ebitengine/purego"
)

// runnerDarwinSleepReleaseAfter bounds how long the runner delays sleep
// before acknowledging kIOMessageSystemWillSleep. macOS waits up to 30 s
// for an acknowledgement; the runner needs one websocket write.
const runnerDarwinSleepReleaseAfter = 3 * time.Second

const (
	ioKitPath          = "/System/Library/Frameworks/IOKit.framework/IOKit"
	coreFoundationPath = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)

// darwinPowerAPI holds the IOKit and CoreFoundation entry points, loaded
// with purego so the CLI stays a cgo-free build.
type darwinPowerAPI struct {
	registerForSystemPower   func(refcon uintptr, port *uintptr, callback uintptr, notifier *uint32) uint32
	deregisterForSystemPower func(notifier *uint32) int32
	allowPowerChange         func(kernelPort uint32, notificationID uintptr) int32
	notificationPortGetRLS   func(port uintptr) uintptr
	notificationPortDestroy  func(port uintptr)
	serviceClose             func(connect uint32) int32
	runLoopGetCurrent        func() uintptr
	runLoopAddSource         func(rl, source, mode uintptr)
	runLoopRun               func()
	runLoopStop              func(rl uintptr)
	stringCreateWithCString  func(alloc uintptr, cstr string, encoding uint32) uintptr
}

// cfStringEncodingUTF8 is kCFStringEncodingUTF8.
const cfStringEncodingUTF8 = 0x08000100

var (
	darwinPowerOnce sync.Once
	darwinPower     *darwinPowerAPI
	// darwinPowerCallback is created once: purego callbacks are never freed.
	darwinPowerCallback uintptr

	// darwinPowerMu guards the state the IOKit callback reads. Only one
	// source is active at a time (one runner loop per process).
	darwinPowerMu     sync.Mutex
	darwinPowerBridge *callbackPowerBridge
	darwinRootPort    uint32
)

func loadDarwinPowerAPI() *darwinPowerAPI {
	darwinPowerOnce.Do(func() {
		iokit, err := purego.Dlopen(ioKitPath, purego.RTLD_NOW|purego.RTLD_GLOBAL)
		if err != nil {
			return
		}
		cf, err := purego.Dlopen(coreFoundationPath, purego.RTLD_NOW|purego.RTLD_GLOBAL)
		if err != nil {
			return
		}
		api := &darwinPowerAPI{}
		purego.RegisterLibFunc(&api.registerForSystemPower, iokit, "IORegisterForSystemPower")
		purego.RegisterLibFunc(&api.deregisterForSystemPower, iokit, "IODeregisterForSystemPower")
		purego.RegisterLibFunc(&api.allowPowerChange, iokit, "IOAllowPowerChange")
		purego.RegisterLibFunc(&api.notificationPortGetRLS, iokit, "IONotificationPortGetRunLoopSource")
		purego.RegisterLibFunc(&api.notificationPortDestroy, iokit, "IONotificationPortDestroy")
		purego.RegisterLibFunc(&api.serviceClose, iokit, "IOServiceClose")
		purego.RegisterLibFunc(&api.runLoopGetCurrent, cf, "CFRunLoopGetCurrent")
		purego.RegisterLibFunc(&api.runLoopAddSource, cf, "CFRunLoopAddSource")
		purego.RegisterLibFunc(&api.runLoopRun, cf, "CFRunLoopRun")
		purego.RegisterLibFunc(&api.runLoopStop, cf, "CFRunLoopStop")
		purego.RegisterLibFunc(&api.stringCreateWithCString, cf, "CFStringCreateWithCString")
		darwinPowerCallback = purego.NewCallback(darwinPowerMessage)
		darwinPower = api
	})
	return darwinPower
}

// darwinPowerMessage is the IOServiceInterestCallback for the root power
// domain. It runs on the run-loop thread started below.
func darwinPowerMessage(_ uintptr, _ uint32, messageType uint32, messageArgument uintptr) {
	darwinPowerMu.Lock()
	bridge, root := darwinPowerBridge, darwinRootPort
	darwinPowerMu.Unlock()
	if bridge == nil {
		return
	}
	if bridge.handleIOKitPowerMessage(messageType) {
		darwinPower.allowPowerChange(root, messageArgument)
	}
}

// startPlatformPowerSource registers for IOKit system power notifications
// on a dedicated run-loop thread. kIOMessageSystemWillSleep (lid close,
// Apple menu Sleep, idle sleep) sends going_to_sleep before the
// acknowledgement lets the host suspend. If IOKit cannot be loaded or
// registration fails, sleep reads as offline as before.
func startPlatformPowerSource(events chan<- runnerPowerEvent, stop <-chan struct{}, epoch *runnerPowerEpoch) func() {
	api := loadDarwinPowerAPI()
	if api == nil {
		return func() {}
	}
	started := make(chan uintptr, 1)
	go func() {
		// The run loop and its source belong to this OS thread.
		runtime.LockOSThread()
		defer runtime.UnlockOSThread()
		var port uintptr
		var notifier uint32
		darwinPowerMu.Lock()
		if darwinPowerBridge != nil {
			darwinPowerMu.Unlock()
			started <- 0
			return
		}
		darwinPowerBridge = &callbackPowerBridge{
			events: events, epoch: epoch, releaseAfter: runnerDarwinSleepReleaseAfter,
		}
		root := api.registerForSystemPower(0, &port, darwinPowerCallback, &notifier)
		if root == 0 {
			darwinPowerBridge = nil
			darwinPowerMu.Unlock()
			started <- 0
			return
		}
		darwinRootPort = root
		darwinPowerMu.Unlock()

		rl := api.runLoopGetCurrent()
		// Run-loop modes compare by string value, so an equal CFString
		// stands in for the kCFRunLoopDefaultMode constant.
		mode := api.stringCreateWithCString(0, "kCFRunLoopDefaultMode", cfStringEncodingUTF8)
		api.runLoopAddSource(rl, api.notificationPortGetRLS(port), mode)
		started <- rl
		api.runLoopRun()

		darwinPowerMu.Lock()
		darwinPowerBridge = nil
		darwinRootPort = 0
		darwinPowerMu.Unlock()
		api.deregisterForSystemPower(&notifier)
		api.serviceClose(root)
		api.notificationPortDestroy(port)
	}()
	rl := <-started
	if rl == 0 {
		return func() {}
	}
	var once sync.Once
	return func() { once.Do(func() { api.runLoopStop(rl) }) }
}
