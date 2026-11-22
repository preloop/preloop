//go:build darwin

package cmd

import "testing"

// Registration against the real root power domain must succeed and stop
// cleanly; a sleep cannot be triggered from a test.
func TestDarwinPowerSourceRegistersAndStops(t *testing.T) {
	if loadDarwinPowerAPI() == nil {
		t.Fatal("IOKit power API did not load")
	}
	events := make(chan runnerPowerEvent, 4)
	stopPlatform := startPlatformPowerSource(events, make(chan struct{}), &runnerPowerEpoch{})
	darwinPowerMu.Lock()
	registered := darwinPowerBridge != nil && darwinRootPort != 0
	darwinPowerMu.Unlock()
	if !registered {
		t.Fatal("IORegisterForSystemPower did not register")
	}
	// Stopping right after start used to race CFRunLoopStop against the
	// loop entering its mode; stop now waits for the source to be gone.
	stopPlatform()
	darwinPowerMu.Lock()
	cleared := darwinPowerBridge == nil
	darwinPowerMu.Unlock()
	if !cleared {
		t.Fatal("source still registered after stop returned")
	}
	// A second runner loop in the same process can register again.
	startPlatformPowerSource(events, make(chan struct{}), &runnerPowerEpoch{})()
}

func TestDarwinKeepAwakeStartsAndReleasesCaffeinate(t *testing.T) {
	release, err := platformKeepAwake()
	if err != nil {
		t.Fatalf("caffeinate did not start: %v", err)
	}
	release()
}
