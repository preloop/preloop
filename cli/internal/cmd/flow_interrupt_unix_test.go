//go:build !windows

package cmd

import (
	"bytes"
	"strings"
	"syscall"
	"testing"
)

// TestFlowTriggerRealSIGINTStopsExecution delivers a real SIGINT to the test
// process through the production signal subscription, the way a CI runner
// cancels a job.
func TestFlowTriggerRealSIGINTStopsExecution(t *testing.T) {
	fake := &fakeFlowAPI{}
	setupInterruptTrigger(t, fake, false)
	flowNotifyInterrupts = defaultFlowNotifyInterrupts
	fake.onStatus = func(reads int) {
		if reads == 1 {
			if err := syscall.Kill(syscall.Getpid(), syscall.SIGINT); err != nil {
				t.Errorf("kill: %v", err)
			}
		}
	}

	var out bytes.Buffer
	flowTriggerCmd.SetOut(&out)
	err := runFlowTrigger(flowTriggerCmd, []string{"PR Review"})

	if fake.stopCalls != 1 {
		t.Fatalf("stop calls = %d, want exactly 1", fake.stopCalls)
	}
	if ProcessExitCode(err) != 130 {
		t.Fatalf("err = %v, want exit 130", err)
	}
	if !strings.Contains(out.String(), "Stopped execution exec-9") {
		t.Fatalf("output = %q", out.String())
	}
}
