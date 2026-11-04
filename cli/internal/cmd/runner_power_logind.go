package cmd

import (
	"sync"

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

func (l *logindInhibitor) take() {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.close != nil {
		return
	}
	close, err := l.acquire()
	if err != nil {
		return
	}
	l.close = close
	l.gen++
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
		l.take()
		return newRunnerSleepEvent(l.releaser())
	}
	// A cancelled suspend can wake before its queued sleep event releases
	// the delay. Replace that generation so its eventual release cannot
	// drop the inhibitor needed for the next suspend.
	l.releaser()()
	l.take()
	return runnerPowerEvent{kind: runnerPowerWake}
}
