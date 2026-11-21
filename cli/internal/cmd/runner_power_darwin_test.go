//go:build darwin

package cmd

import (
	"testing"
	"time"
)

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
	stopPlatform()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		darwinPowerMu.Lock()
		cleared := darwinPowerBridge == nil
		darwinPowerMu.Unlock()
		if cleared {
			// A second runner loop in the same process can register again.
			startPlatformPowerSource(events, make(chan struct{}), &runnerPowerEpoch{})()
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("run loop did not stop")
}

func TestDarwinKeepAwakeStartsAndReleasesCaffeinate(t *testing.T) {
	release, err := platformKeepAwake()
	if err != nil {
		t.Fatalf("caffeinate did not start: %v", err)
	}
	release()
}
