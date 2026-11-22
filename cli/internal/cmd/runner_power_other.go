//go:build !linux && !darwin && !windows

package cmd

// startPlatformPowerSource has no pre-sleep signal on this platform. Sleep
// reads as offline; the clock jump detector still reconnects right after
// wake.
func startPlatformPowerSource(chan<- runnerPowerEvent, <-chan struct{}, *runnerPowerEpoch) func() {
	return func() {}
}
