package cmd

import (
	"errors"
	"io"
	"os"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestClockJumpDetectsSleepGap(t *testing.T) {
	base := time.Date(2026, 11, 4, 10, 0, 0, 0, time.UTC)
	interval := 5 * time.Second
	cases := []struct {
		name     string
		wall     time.Duration
		expected bool
	}{
		{"steady tick", 5 * time.Second, false},
		{"small drift", 6 * time.Second, false},
		// Holds whether or not the monotonic clock ran during the suspend
		// (Windows), because only the wall-clock gap is used.
		{"slept ten minutes", 10*time.Minute + 5*time.Second, true},
		{"clock set back", -time.Hour, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := runnerClockJumped(base, base.Add(tc.wall), interval)
			if got != tc.expected {
				t.Fatalf("runnerClockJumped = %v, want %v", got, tc.expected)
			}
		})
	}
}

func withHeartbeatEvery(t *testing.T, d time.Duration) {
	t.Helper()
	old := runnerHeartbeatEvery
	runnerHeartbeatEvery = d
	t.Cleanup(func() { runnerHeartbeatEvery = old })
}

func TestSleepSignalSendsGoingToSleepAndStopsHeartbeats(t *testing.T) {
	testenv.SetTempHome(t)
	withHeartbeatEvery(t, 50*time.Millisecond)
	events := make(chan runnerPowerEvent, 4)
	server, messages, _ := jobServer(t, map[string]any{"type": "hello"})

	interrupt := make(chan os.Signal, 1)
	jobs := newRunnerJobs(1)
	jobs.power = events
	stopped := make(chan error, 1)
	go func() {
		stopped <- runRunnerSession(dialJobServer(t, server), interrupt, io.Discard, jobs)
	}()
	t.Cleanup(func() {
		select {
		case interrupt <- os.Interrupt:
		default:
		}
	})

	// Initial heartbeat first.
	waitForMessageType(t, messages, "heartbeat")
	released := make(chan struct{})
	events <- runnerPowerEvent{kind: runnerPowerSleep, done: func() { close(released) }, at: time.Now()}
	waitForMessageType(t, messages, "going_to_sleep")
	select {
	case <-released:
	case <-time.After(2 * time.Second):
		t.Fatal("sleep inhibitor was never released after going_to_sleep was sent")
	}
	// A heartbeat now would clear the sleep flag on the server.
	deadline := time.After(300 * time.Millisecond)
	for {
		select {
		case message := <-messages:
			if message["type"] == "heartbeat" {
				t.Fatal("heartbeat sent after going_to_sleep")
			}
		case <-deadline:
			return
		}
	}
}

func TestWakeSignalEndsSessionForImmediateReconnect(t *testing.T) {
	testenv.SetTempHome(t)
	events := make(chan runnerPowerEvent, 4)
	server, messages, _ := jobServer(t, map[string]any{"type": "hello"})

	interrupt := make(chan os.Signal, 1)
	jobs := newRunnerJobs(1)
	jobs.power = events
	stopped := make(chan error, 1)
	go func() {
		stopped <- runRunnerSession(dialJobServer(t, server), interrupt, io.Discard, jobs)
	}()
	waitForMessageType(t, messages, "heartbeat")
	events <- runnerPowerEvent{kind: runnerPowerWake}
	select {
	case err := <-stopped:
		if !errors.Is(err, errRunnerWoke) {
			t.Fatalf("session ended with %v, want errRunnerWoke", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("session did not end on wake")
	}
}

func TestReconnectWaitIsCutShortByWake(t *testing.T) {
	events := make(chan runnerPowerEvent, 4)
	interrupt := make(chan os.Signal, 1)
	events <- runnerPowerEvent{kind: runnerPowerWake}
	start := time.Now()
	ok, woke := waitForReconnect(interrupt, events, time.Minute)
	if !ok || !woke {
		t.Fatalf("waitForReconnect = (%v, %v), want (true, true)", ok, woke)
	}
	if time.Since(start) > time.Second {
		t.Fatal("wake did not cut the reconnect wait short")
	}
}

func TestReconnectWaitIgnoresStaleSleepEvents(t *testing.T) {
	events := make(chan runnerPowerEvent, 4)
	interrupt := make(chan os.Signal, 1)
	released := false
	events <- runnerPowerEvent{kind: runnerPowerSleep, done: func() { released = true }}
	ok, woke := waitForReconnect(interrupt, events, 50*time.Millisecond)
	if !ok || woke {
		t.Fatalf("waitForReconnect = (%v, %v), want (true, false)", ok, woke)
	}
	if !released {
		t.Fatal("a sleep event while disconnected must still release its inhibitor")
	}
}

func TestHeartbeatCarriesOSIdleSecondsWhenKnown(t *testing.T) {
	known := map[string]any{}
	addOSIdleSecondsFrom(known, func() (int, bool) { return 420, true })
	if got := known["os_idle_seconds"]; got != 420 {
		t.Fatalf("os_idle_seconds = %v, want 420", got)
	}
	unknown := map[string]any{}
	addOSIdleSecondsFrom(unknown, func() (int, bool) { return 0, false })
	if _, present := unknown["os_idle_seconds"]; present {
		t.Fatal("os_idle_seconds must be absent when the platform cannot report it")
	}
}

func waitForMessageType(t *testing.T, messages chan map[string]any, kind string) {
	t.Helper()
	deadline := time.After(5 * time.Second)
	for {
		select {
		case message := <-messages:
			if message["type"] == kind {
				return
			}
		case <-deadline:
			t.Fatalf("no %q message", kind)
		}
	}
}

func TestParseHIDIdleSeconds(t *testing.T) {
	out := `+-o IOHIDSystem  <class IOHIDSystem>
    {
      "HIDIdleTime" = 421337000000
    }`
	if got, ok := parseHIDIdleSeconds(out); !ok || got != 421 {
		t.Fatalf("parseHIDIdleSeconds = (%d, %v), want (421, true)", got, ok)
	}
	if _, ok := parseHIDIdleSeconds("no such key"); ok {
		t.Fatal("missing key must not parse")
	}
}

func TestStaleSleepEventAfterWakeIsIgnored(t *testing.T) {
	testenv.SetTempHome(t)
	withHeartbeatEvery(t, 50*time.Millisecond)
	events := make(chan runnerPowerEvent, 4)
	server, messages, _ := jobServer(t, map[string]any{"type": "hello"})

	interrupt := make(chan os.Signal, 1)
	jobs := newRunnerJobs(1)
	jobs.power = events
	go func() { _ = runRunnerSession(dialJobServer(t, server), interrupt, io.Discard, jobs) }()
	t.Cleanup(func() {
		select {
		case interrupt <- os.Interrupt:
		default:
		}
	})
	waitForMessageType(t, messages, "heartbeat")
	released := make(chan struct{})
	// Announced before a suspend, handled only after the wake.
	events <- runnerPowerEvent{
		kind: runnerPowerSleep,
		done: func() { close(released) },
		at:   time.Now().Add(-10 * time.Minute),
	}
	select {
	case <-released:
	case <-time.After(2 * time.Second):
		t.Fatal("stale sleep event was not released")
	}
	deadline := time.After(400 * time.Millisecond)
	sawHeartbeat := false
	for {
		select {
		case message := <-messages:
			if message["type"] == "going_to_sleep" {
				t.Fatal("stale sleep event re-sent going_to_sleep")
			}
			if message["type"] == "heartbeat" {
				sawHeartbeat = true
			}
		case <-deadline:
			if !sawHeartbeat {
				t.Fatal("heartbeats stopped after a stale sleep event")
			}
			return
		}
	}
}

func TestWakeDropsSleepEventQueuedBehindIt(t *testing.T) {
	testenv.SetTempHome(t)
	events := make(chan runnerPowerEvent, 4)
	server, messages, _ := jobServer(t, map[string]any{"type": "hello"})
	interrupt := make(chan os.Signal, 1)
	jobs := newRunnerJobs(1)
	jobs.power = events
	stopped := make(chan error, 1)
	go func() {
		stopped <- runRunnerSession(dialJobServer(t, server), interrupt, io.Discard, jobs)
	}()
	waitForMessageType(t, messages, "heartbeat")
	released := make(chan struct{})
	// A short suspend: the wake is handled first, its sleep event is still
	// queued and fresh.
	events <- runnerPowerEvent{kind: runnerPowerWake}
	events <- runnerPowerEvent{kind: runnerPowerSleep, done: func() { close(released) }, at: time.Now()}
	select {
	case err := <-stopped:
		if !errors.Is(err, errRunnerWoke) {
			t.Fatalf("session ended with %v, want errRunnerWoke", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("session did not end on wake")
	}
	select {
	case <-released:
	case <-time.After(time.Second):
		t.Fatal("sleep event queued behind the wake was not drained")
	}
	if len(events) != 0 {
		t.Fatal("events left queued after wake")
	}
}
