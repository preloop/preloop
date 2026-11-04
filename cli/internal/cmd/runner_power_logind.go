package cmd

import (
	"sync"
	"time"

	"github.com/godbus/dbus/v5"
)

const (
	logindDest      = "org.freedesktop.login1"
	logindPath      = "/org/freedesktop/login1"
	logindInterface = "org.freedesktop.login1.Manager"
)

// logindPrepareForSleep reads a PrepareForSleep signal from logind's current
// unique bus name: (true, true) before sleep, (false, true) after wake,
// (_, false) for anything else.
func logindPrepareForSleep(signal *dbus.Signal, owner string) (sleeping, ok bool) {
	if signal == nil || owner == "" || signal.Sender != owner ||
		signal.Name != logindInterface+".PrepareForSleep" || len(signal.Body) != 1 {
		return false, false
	}
	value, isBool := signal.Body[0].(bool)
	return value, isBool
}

// logindInhibitor owns one sleep delay at a time. acquire returns a function
// that closes the acquired descriptor; keeping acquisition separate allows
// the event lifecycle to be tested without a live system bus.
type logindInhibitor struct {
	acquire func() (func(), error)
	mu      sync.Mutex
	close   func()
	// gen counts takes, so a release armed for one sleep never drops the
	// delay taken again after the following wake.
	gen int
}

func (l *logindInhibitor) take() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.close != nil {
		return true
	}
	close, err := l.acquire()
	if err != nil {
		return false
	}
	l.close = close
	l.gen++
	return true
}

// releaser returns an idempotent function that drops the current delay.
func (l *logindInhibitor) releaser() func() {
	l.mu.Lock()
	gen := l.gen
	l.mu.Unlock()
	var once sync.Once
	return func() {
		once.Do(func() {
			l.mu.Lock()
			defer l.mu.Unlock()
			if l.close != nil && l.gen == gen {
				l.close()
				l.close = nil
			}
		})
	}
}

func (l *logindInhibitor) powerEvent(sleeping bool) runnerPowerEvent {
	if sleeping {
		// Retry if startup or the previous wake could not acquire a delay.
		held := l.take()
		event := newRunnerSleepEvent(l.releaser())
		if !held {
			// Without a delay the host may already have suspended by the
			// time the session can handle this notification.
			event.release()
		}
		return event
	}
	// A cancelled suspend can wake before its queued sleep event releases
	// the delay. Replace that generation so its eventual release cannot
	// drop the inhibitor needed for the next suspend.
	l.releaser()()
	l.take()
	return runnerPowerEvent{kind: runnerPowerWake}
}

// logindPowerSource separates authenticated signal handling from the system
// bus transport, so delayed replies and wake races can be tested directly.
type logindPowerSource struct {
	inhibitor         *logindInhibitor
	epoch             *runnerPowerEpoch
	owner             func() (string, error)
	preparingForSleep func(string) (bool, error)
}

func (s *logindPowerSource) event(signal *dbus.Signal) (runnerPowerEvent, bool) {
	// Capture before any blocking bus request. A clock wake can finish its
	// queue drain while the owner or property reply is still pending.
	generation := s.epoch.generation.Load()
	receivedAt := time.Now()
	owner, err := s.owner()
	if err != nil {
		return runnerPowerEvent{}, false
	}
	sleeping, ok := logindPrepareForSleep(signal, owner)
	if !ok {
		return runnerPowerEvent{}, false
	}
	if sleeping {
		// The signal itself may first be dequeued after a clock wake.
		// Query the authenticated unique owner, not just the service name,
		// to distinguish that old signal from a new suspend request.
		preparing, err := s.preparingForSleep(owner)
		if err != nil || !preparing {
			return runnerPowerEvent{}, false
		}
	} else {
		s.epoch.wakeEvent()
	}
	event := s.inhibitor.powerEvent(sleeping)
	event.at = receivedAt
	event.epoch = s.epoch
	event.generation = generation
	return event, true
}
