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
	events <- newRunnerSleepEvent(func() { close(released) })
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

func TestShortSuspendDoesNotReplaySleep(t *testing.T) {
	for _, order := range []string{"sleep then wake", "wake then sleep", "sleep delivered after wake"} {
		t.Run(order, func(t *testing.T) {
			testenv.SetTempHome(t)
			withHeartbeatEvery(t, 50*time.Millisecond)
			events := make(chan runnerPowerEvent, 4)
			jobs := newRunnerJobs(1)
			jobs.power = events
			interrupt := make(chan os.Signal, 1)
			releases := 0
			epoch := &runnerPowerEpoch{}
			sleep := newRunnerSleepEvent(func() { releases++ })
			sleep.epoch = epoch
			sleep.generation = epoch.generation.Load()
			sleep.at = time.Now().Add(-5 * time.Second)
			// Simulate the 3 s inhibitor deadline firing while the session
			// is blocked, followed by a wake inside the 10 s age limit.
			// Keep one event unreleased so the new session must reject its
			// old epoch and release it, without relying on expiration.
			if order != "wake then sleep" {
				sleep.release()
			}
			wake := epoch.wakeEvent()
			switch order {
			case "sleep then wake":
				events <- sleep
				events <- wake
			case "wake then sleep":
				events <- wake
				events <- sleep
			default:
				events <- wake
			}

			// Queue notifications before starting the session, so the
			// test does not depend on scheduling between channel sends.
			server, messages, _ := jobServer(t, map[string]any{"type": "hello"})
			conn := dialJobServer(t, server)
			if err := runRunnerSession(conn, interrupt, io.Discard, jobs); !errors.Is(err, errRunnerWoke) {
				t.Fatalf("session ended with %v, want errRunnerWoke", err)
			}
			_ = conn.Close()
			// Closing the client does not guarantee that the server has
			// finished reading its frames; use a separate server below.
			waitForMessageType(t, messages, "heartbeat")

			if order == "sleep delivered after wake" {
				events <- sleep
			}
			server, messages, _ = jobServer(t, map[string]any{"type": "hello"})
			conn = dialJobServer(t, server)
			stopped := make(chan error, 1)
			go func() { stopped <- runRunnerSession(conn, interrupt, io.Discard, jobs) }()
			t.Cleanup(func() {
				interrupt <- os.Interrupt
				select {
				case err := <-stopped:
					if err != nil {
						t.Errorf("reconnected session ended with %v", err)
					}
				case <-time.After(2 * time.Second):
					t.Error("reconnected session did not stop")
				}
			})
			// Observe the initial heartbeat and two periodic ones. A stale
			// going_to_sleep would suppress those periodic heartbeats.
			deadline := time.After(2 * time.Second)
			for heartbeats := 0; heartbeats < 3; {
				select {
				case message := <-messages:
					switch message["type"] {
					case "going_to_sleep":
						t.Fatal("short suspend replayed going_to_sleep after reconnect")
					case "heartbeat":
						heartbeats++
					}
				case <-deadline:
					t.Fatal("heartbeats did not continue after reconnect")
				}
			}
			if releases != 1 {
				t.Fatalf("sleep inhibitor released %d times, want 1", releases)
			}
			if len(events) != 0 {
				t.Fatal("new session did not consume stale sleep events")
			}
		})
	}
}

func TestSleepEventExpiresWhenInhibitorIsReleased(t *testing.T) {
	event := newRunnerSleepEvent(func() {})
	if event.stale(time.Now()) {
		t.Fatal("sleep event expired before the inhibitor was released")
	}
	event.release()
	if !event.stale(time.Now()) {
		t.Fatal("released sleep event can be replayed inside the age limit")
	}
}

func TestReconnectWakeLeavesStaleSleepForNextConsumer(t *testing.T) {
	events := make(chan runnerPowerEvent, 4)
	epoch := &runnerPowerEpoch{}
	released := false
	sleep := newRunnerSleepEvent(func() { released = true })
	sleep.epoch = epoch
	sleep.generation = epoch.generation.Load()
	events <- epoch.wakeEvent()
	events <- sleep
	if ok, woke := waitForReconnect(nil, events, time.Minute); !ok || !woke {
		t.Fatalf("waitForReconnect = (%v, %v), want (true, true)", ok, woke)
	}
	if released || len(events) != 1 {
		t.Fatal("wake must leave later events queued for the next consumer")
	}
	queued := <-events
	if !queued.stale(time.Now()) {
		t.Fatal("old sleep event was not invalidated by the wake")
	}
	queued.release()
	if !released {
		t.Fatal("stale event did not release its inhibitor when consumed")
	}
}

func TestClockWakeDoesNotInvalidateSleepAfterReportedWake(t *testing.T) {
	epoch := &runnerPowerEpoch{}
	epoch.wakeEvent() // logind reported the wake before the detector's sample.
	nextSleep := newRunnerSleepEvent(func() {})
	nextSleep.epoch = epoch
	nextSleep.generation = epoch.generation.Load()
	now := time.Now().Round(0)
	previousSample := now.Add(-time.Minute)
	if _, woke := epoch.clockWakeEvent(previousSample, now, 5*time.Second); woke {
		t.Fatal("late clock sample reported the same wake twice")
	}
	if nextSleep.stale(now) {
		t.Fatal("duplicate clock wake invalidated a genuine subsequent sleep")
	}
	// Two suspends can occur between detector samples. A later gap since
	// the reported wake must still reconnect, even though logind advanced
	// the epoch since previousSample.
	later := now.Add(time.Minute)
	if event, woke := epoch.clockWakeEvent(previousSample, later, 5*time.Second); !woke || event.kind != runnerPowerWake {
		t.Fatal("a later real suspend was hidden by the earlier logind wake")
	}
	if nextSleep.generation == epoch.generation.Load() {
		t.Fatal("the later wake did not invalidate the previous sleep")
	}
	if _, woke := epoch.clockWakeEvent(previousSample, later, 5*time.Second); woke {
		t.Fatal("repeated sample reported the later wake twice")
	}
}

func TestClockWakeRecoversAfterWallClockMovesBack(t *testing.T) {
	epoch := &runnerPowerEpoch{}
	epoch.wakeEvent()
	now := time.Now().Round(0)
	reset := now.Add(-time.Hour)
	if _, woke := epoch.clockWakeEvent(now, reset, 5*time.Second); woke {
		t.Fatal("backward clock adjustment was treated as a wake")
	}
	if _, woke := epoch.clockWakeEvent(reset, reset.Add(time.Minute), 5*time.Second); !woke {
		t.Fatal("old wake timestamp masked a suspend after the clock reset")
	}
}
