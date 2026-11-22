package cmd

import (
	"errors"
	"io"
	"os"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func newTestPowerBridge(releaseAfter time.Duration) (*callbackPowerBridge, chan runnerPowerEvent) {
	events := make(chan runnerPowerEvent, 4)
	return &callbackPowerBridge{
		events: events, epoch: &runnerPowerEpoch{}, releaseAfter: releaseAfter,
	}, events
}

// The OS waits for the callback; it must return as soon as the session
// handled the event, not after the full bound.
func TestCallbackWillSleepReturnsWhenSessionReleases(t *testing.T) {
	bridge, events := newTestPowerBridge(time.Minute)
	go func() {
		event := <-events
		if event.kind != runnerPowerSleep || event.stale(time.Now()) {
			t.Errorf("want a fresh sleep event, got kind=%v", event.kind)
		}
		event.release()
	}()
	done := make(chan struct{})
	go func() { bridge.willSleep(); close(done) }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("willSleep did not return after release")
	}
}

// Nothing consumes the event (session between connections): the callback
// still returns within the bound and the event cannot be replayed later.
func TestCallbackWillSleepNeverHoldsSleepPastBound(t *testing.T) {
	bridge, events := newTestPowerBridge(20 * time.Millisecond)
	start := time.Now()
	bridge.willSleep()
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Fatalf("willSleep held sleep for %v", elapsed)
	}
	event := <-events
	if !event.stale(time.Now()) {
		t.Fatal("an event released by the bound must be stale")
	}
}

func TestCallbackWillSleepDoesNotBlockOnFullQueue(t *testing.T) {
	bridge, events := newTestPowerBridge(time.Minute)
	for i := 0; i < cap(events); i++ {
		events <- runnerPowerEvent{kind: runnerPowerWake}
	}
	done := make(chan struct{})
	go func() { bridge.willSleep(); close(done) }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("willSleep blocked on a full queue")
	}
}

// Wake after a sleep the session never read invalidates that sleep, so a
// running host is not reported asleep.
func TestCallbackWakeInvalidatesQueuedSleep(t *testing.T) {
	bridge, events := newTestPowerBridge(20 * time.Millisecond)
	bridge.willSleep()
	bridge.woke()
	sleep, wake := <-events, <-events
	if sleep.kind != runnerPowerSleep || !sleep.stale(time.Now()) {
		t.Fatal("sleep queued before wake must be stale")
	}
	if wake.kind != runnerPowerWake {
		t.Fatalf("want wake, got %v", wake.kind)
	}
}

func TestIOKitPowerMessageDispatch(t *testing.T) {
	bridge, events := newTestPowerBridge(time.Minute)
	if !bridge.handleIOKitPowerMessage(ioMessageCanSystemSleep) {
		t.Fatal("idle sleep query must be allowed")
	}
	if len(events) != 0 {
		t.Fatal("idle sleep query is not a sleep")
	}

	go func() { (<-events).release() }()
	if !bridge.handleIOKitPowerMessage(ioMessageSystemWillSleep) {
		t.Fatal("will-sleep must be acknowledged")
	}

	if bridge.handleIOKitPowerMessage(ioMessageSystemHasPoweredOn) {
		t.Fatal("powered-on needs no acknowledgement")
	}
	if event := <-events; event.kind != runnerPowerWake {
		t.Fatalf("powered-on must report wake, got %v", event.kind)
	}
	if bridge.handleIOKitPowerMessage(0xe0000320) || len(events) != 0 {
		t.Fatal("will-power-on is ignored")
	}
}

func TestWindowsPowerEventDispatch(t *testing.T) {
	bridge, events := newTestPowerBridge(time.Minute)
	go func() { (<-events).release() }()
	bridge.handleWindowsPowerEvent(pbtAPMSuspend)

	bridge.handleWindowsPowerEvent(pbtAPMResumeAutomatic)
	if event := <-events; event.kind != runnerPowerWake {
		t.Fatalf("resume must report wake, got %v", event.kind)
	}
	// PBT_APMRESUMESUSPEND follows a user wake; it must not cut the fresh
	// session short again.
	bridge.handleWindowsPowerEvent(0x0007)
	if len(events) != 0 {
		t.Fatal("PBT_APMRESUMESUSPEND must not report a second wake")
	}
}

// End to end through a live session: the OS callback blocks until the
// session sent going_to_sleep, and the matching wake ends the session for
// an immediate reconnect.
func TestCallbackSleepAndWakeDriveRunnerSession(t *testing.T) {
	testenv.SetTempHome(t)
	bridge, events := newTestPowerBridge(time.Minute)
	server, messages, _ := jobServer(t, map[string]any{"type": "hello"})
	interrupt := make(chan os.Signal, 1)
	jobs := newRunnerJobs(1)
	jobs.power = events
	stopped := make(chan error, 1)
	go func() {
		stopped <- runRunnerSession(dialJobServer(t, server), interrupt, io.Discard, jobs)
	}()
	waitForMessageType(t, messages, "heartbeat")

	acknowledged := make(chan bool, 1)
	go func() { acknowledged <- bridge.handleIOKitPowerMessage(ioMessageSystemWillSleep) }()
	waitForMessageType(t, messages, "going_to_sleep")
	select {
	case ok := <-acknowledged:
		if !ok {
			t.Fatal("will-sleep was not acknowledged")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("callback still blocked after going_to_sleep was sent")
	}

	bridge.handleIOKitPowerMessage(ioMessageSystemHasPoweredOn)
	select {
	case err := <-stopped:
		if !errors.Is(err, errRunnerWoke) {
			t.Fatalf("session ended with %v, want errRunnerWoke", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("session did not end on wake")
	}
}

// A wake the full queue dropped must not hide the next clock jump, or a
// session that sent going_to_sleep could stay asleep.
func TestCallbackDroppedWakeLeavesClockFallback(t *testing.T) {
	bridge, events := newTestPowerBridge(time.Minute)
	for i := 0; i < cap(events); i++ {
		events <- runnerPowerEvent{kind: runnerPowerSleep}
	}
	before := bridge.epoch.generation.Load()
	bridge.woke()
	if bridge.epoch.generation.Load() == before {
		t.Fatal("dropped wake must still invalidate queued sleep")
	}
	prev := time.Now().Add(-time.Minute).Round(0)
	if _, woke := bridge.epoch.clockWakeEvent(prev, time.Now().Round(0), runnerWakeCheckEvery); !woke {
		t.Fatal("clock-jump fallback suppressed by a dropped wake")
	}
}
