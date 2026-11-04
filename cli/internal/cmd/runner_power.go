package cmd

// Sleep and wake handling for personal runners (#1487, #1507).
//
// The control plane derives presence from heartbeats (#1506): a runner that
// sends going_to_sleep before its socket drops reads as asleep, any other
// drop reads as offline after the heartbeat timeout. This file feeds that
// contract from the host:
//
//   - Before sleep, where the OS announces it (Linux logind PrepareForSleep),
//     the session sends going_to_sleep and stops heartbeating, because any
//     later heartbeat clears the flag on the server.
//   - On wake, detected from the OS signal or from a gap in wall-clock time
//     between two detector samples far larger than the sample interval, the
//     runner drops the stale socket and reconnects at once with the backoff
//     reset. The wall clock keeps running through sleep on every OS; whether
//     the monotonic clock does differs (it stops on macOS and Linux, and may
//     not on Windows), so only the wall-clock gap is used.
//
// Hosts without a pre-sleep signal (macOS and Windows in this build) fall
// back to offline, then return to online right after wake.

import (
	"errors"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

// errRunnerWoke ends a session after the host woke: the caller reconnects
// at once instead of waiting for the stale socket to time out.
var errRunnerWoke = errors.New("runner host woke from sleep")

type runnerPowerKind int

const (
	runnerPowerSleep runnerPowerKind = iota + 1
	runnerPowerWake
)

// runnerPowerEvent is one sleep or wake notification. done, when set,
// releases the OS sleep delay (logind inhibitor) once the event was handled.
type runnerPowerEvent struct {
	kind runnerPowerKind
	done func()
	// at is when the OS announced the event; a sleep event older than
	// runnerSleepEventMaxAge was buffered across a suspend and is stale.
	at time.Time
	// released closes when the sleep delay is released, including by its
	// timeout. After that the host may already have suspended and resumed.
	released <-chan struct{}
}

// newRunnerSleepEvent shares expiration across queued copies and releases the
// inhibitor only once, whether the session or the timeout gets there first.
func newRunnerSleepEvent(release func()) runnerPowerEvent {
	released := make(chan struct{})
	var once sync.Once
	return runnerPowerEvent{
		kind: runnerPowerSleep,
		at:   time.Now(),
		done: func() {
			once.Do(func() {
				close(released)
				release()
			})
		},
		released: released,
	}
}

// runnerSleepEventMaxAge bounds how old a sleep event may be when the
// session handles it. Older ones were delivered after the wake they
// preceded and must not mark a running host asleep.
const runnerSleepEventMaxAge = 10 * time.Second

func (e runnerPowerEvent) stale(now time.Time) bool {
	if e.kind != runnerPowerSleep {
		return false
	}
	select {
	case <-e.released:
		// Even a short suspend can finish inside the age limit. Once the
		// delay is gone, this notification can no longer precede sleep.
		return true
	default:
		return e.at.IsZero() || now.Round(0).Sub(e.at.Round(0)) > runnerSleepEventMaxAge
	}
}

func (e runnerPowerEvent) release() {
	if e.done != nil {
		e.done()
	}
}

// runnerOSIdleSeconds reports seconds since the last user input, when the
// platform can tell (see runner_idle_*.go).
var runnerOSIdleSeconds = platformOSIdleSeconds

var (
	// runnerWakeCheckEvery is how often the clock-jump detector samples.
	runnerWakeCheckEvery = 5 * time.Second
)

// runnerWakeJumpThreshold is how much longer than the sample interval the
// wall-clock gap between two samples must be to count as a sleep.
const runnerWakeJumpThreshold = 20 * time.Second

// runnerClockJumped reports whether the wall clock advanced at least the
// threshold beyond the expected sample interval: the host was suspended.
// It holds whether or not the monotonic clock ran during the suspend.
func runnerClockJumped(prevWall, nowWall time.Time, interval time.Duration) bool {
	return nowWall.Sub(prevWall)-interval >= runnerWakeJumpThreshold
}

// startRunnerPowerWatcher starts the platform sleep source and the clock
// jump wake detector, and returns the merged events and a stop function.
func startRunnerPowerWatcher() (<-chan runnerPowerEvent, func()) {
	events := make(chan runnerPowerEvent, 4)
	stop := make(chan struct{})
	stopPlatform := startPlatformPowerSource(events, stop)
	go watchRunnerClockJumps(events, stop)
	return events, func() {
		close(stop)
		stopPlatform()
	}
}

func watchRunnerClockJumps(events chan<- runnerPowerEvent, stop <-chan struct{}) {
	ticker := time.NewTicker(runnerWakeCheckEvery)
	defer ticker.Stop()
	prevWall := time.Now().Round(0)
	for {
		select {
		case <-stop:
			return
		case <-ticker.C:
			nowWall := time.Now().Round(0)
			if runnerClockJumped(prevWall, nowWall, runnerWakeCheckEvery) {
				select {
				case events <- runnerPowerEvent{kind: runnerPowerWake}:
				default:
				}
			}
			prevWall = nowWall
		}
	}
}

// waitForReconnect waits d before the next connection attempt. It returns
// ok=false on interrupt and woke=true when a wake cut the wait short, so
// the caller retries at once with the backoff reset. A sleep event that
// arrives while disconnected has nothing to report and is just released.
func waitForReconnect(
	interrupt <-chan os.Signal, power <-chan runnerPowerEvent, d time.Duration,
) (ok, woke bool) {
	timer := time.NewTimer(d)
	defer timer.Stop()
	for {
		select {
		case <-interrupt:
			return false, false
		case <-timer.C:
			return true, false
		case event := <-power:
			event.release()
			if event.kind == runnerPowerWake {
				drainRunnerPowerEvents(power)
				return true, true
			}
		}
	}
}

// drainRunnerPowerEvents releases every queued event. Called on wake: a
// sleep event still queued then belongs to the suspend that just ended,
// however short it was, and must not mark the woken host asleep.
func drainRunnerPowerEvents(power <-chan runnerPowerEvent) {
	for {
		select {
		case event := <-power:
			event.release()
		default:
			return
		}
	}
}

// addOSIdleSeconds adds the optional os_idle_seconds heartbeat field.
func addOSIdleSeconds(msg map[string]any) {
	addOSIdleSecondsFrom(msg, runnerOSIdleSeconds)
}

func addOSIdleSecondsFrom(msg map[string]any, source func() (int, bool)) {
	if seconds, ok := source(); ok && seconds >= 0 {
		msg["os_idle_seconds"] = seconds
	}
}

// parseHIDIdleSeconds reads macOS `ioreg -c IOHIDSystem` output: the
// HIDIdleTime property is nanoseconds since the last keyboard or mouse input.
func parseHIDIdleSeconds(out string) (int, bool) {
	const key = `"HIDIdleTime" = `
	idx := strings.Index(out, key)
	if idx < 0 {
		return 0, false
	}
	rest := out[idx+len(key):]
	end := 0
	for end < len(rest) && rest[end] >= '0' && rest[end] <= '9' {
		end++
	}
	if end == 0 {
		return 0, false
	}
	nanos, err := strconv.ParseInt(rest[:end], 10, 64)
	if err != nil || nanos < 0 {
		return 0, false
	}
	return int(nanos / int64(time.Second)), true
}
