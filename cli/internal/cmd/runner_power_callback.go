package cmd

import "time"

// callbackPowerBridge turns synchronous OS power callbacks (macOS IOKit
// IORegisterForSystemPower, Windows PowerRegisterSuspendResumeNotification)
// into runner power events. The OS waits for the callback to return before
// it suspends, so willSleep blocks until the session sent going_to_sleep or
// the bound passed, whichever comes first. The bound keeps the runner from
// ever holding the host awake for long.
type callbackPowerBridge struct {
	events       chan<- runnerPowerEvent
	epoch        *runnerPowerEpoch
	releaseAfter time.Duration
}

// willSleep reports an imminent suspend and returns once the event was
// handled or releaseAfter elapsed. It never blocks longer than the bound.
func (b *callbackPowerBridge) willSleep() {
	generation := b.epoch.generation.Load()
	event := newRunnerSleepEvent(func() {})
	event.epoch = b.epoch
	event.generation = generation
	select {
	case b.events <- event:
	default:
		// The queue is full; the session cannot report this sleep in time.
		event.release()
		return
	}
	timer := time.NewTimer(b.releaseAfter)
	defer timer.Stop()
	select {
	case <-event.released:
	case <-timer.C:
		// Expire the event so a session that reads it after wake drops it.
		event.release()
	}
}

// woke reports that the host resumed. It invalidates any sleep event still
// queued and asks the session to reconnect at once.
func (b *callbackPowerBridge) woke() {
	b.epoch.mu.Lock()
	prevWake := b.epoch.lastWakeWall
	b.epoch.mu.Unlock()
	event := b.epoch.wakeEvent()
	select {
	case b.events <- event:
	default:
		// Dropped: keep the generation bump so queued sleep goes stale,
		// but leave the clock-jump detector free to report this wake.
		b.epoch.mu.Lock()
		b.epoch.lastWakeWall = prevWake
		b.epoch.mu.Unlock()
	}
}

// IOKit system power messages (IOKit/IOMessage.h).
const (
	ioMessageCanSystemSleep     uint32 = 0xe0000270
	ioMessageSystemWillSleep    uint32 = 0xe0000280
	ioMessageSystemHasPoweredOn uint32 = 0xe0000300
)

// handleIOKitPowerMessage dispatches one IOKit power message and reports
// whether the caller must acknowledge it with IOAllowPowerChange. Idle
// sleep queries are allowed at once: the runner never vetoes sleep.
func (b *callbackPowerBridge) handleIOKitPowerMessage(message uint32) (allow bool) {
	switch message {
	case ioMessageCanSystemSleep:
		return true
	case ioMessageSystemWillSleep:
		b.willSleep()
		return true
	case ioMessageSystemHasPoweredOn:
		b.woke()
	}
	return false
}

// Windows power broadcast event types (winuser.h).
const (
	pbtAPMSuspend         uint32 = 0x0004
	pbtAPMResumeAutomatic uint32 = 0x0012
)

// handleWindowsPowerEvent dispatches one suspend or resume notification.
// Only PBT_APMRESUMEAUTOMATIC counts as wake: Windows sends it on every
// resume, and also sends PBT_APMRESUMESUSPEND after a user-initiated one,
// which would cut the fresh session short a second time.
func (b *callbackPowerBridge) handleWindowsPowerEvent(eventType uint32) {
	switch eventType {
	case pbtAPMSuspend:
		b.willSleep()
	case pbtAPMResumeAutomatic:
		b.woke()
	}
}
