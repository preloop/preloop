//go:build !linux

package cmd

// startPlatformPowerSource has no pre-sleep signal on this platform in a
// cgo-free build: macOS IOKit power notifications and Windows power
// broadcasts both need a native callback. Sleep reads as offline; the clock
// jump detector still reconnects right after wake.
func startPlatformPowerSource(chan<- runnerPowerEvent, <-chan struct{}, *runnerPowerEpoch) func() {
	return func() {}
}
