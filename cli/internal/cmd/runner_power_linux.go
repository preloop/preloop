//go:build linux

package cmd

import (
	"sync"
	"syscall"
	"time"

	"github.com/godbus/dbus/v5"
)

const (
	// runnerSleepReleaseAfter bounds how long the runner holds a sleep
	// delay, well under logind's default InhibitDelayMaxSec (5 s).
	runnerSleepReleaseAfter = 3 * time.Second
)

// startPlatformPowerSource subscribes to logind PrepareForSleep on the
// system bus and holds a "delay" sleep inhibitor so going_to_sleep can go
// out before the host suspends. Without a system bus or logind (containers,
// minimal hosts) it does nothing and sleep reads as offline.
func startPlatformPowerSource(events chan<- runnerPowerEvent, stop <-chan struct{}) func() {
	conn, err := dbus.ConnectSystemBus()
	if err != nil {
		return func() {}
	}
	if err := conn.AddMatchSignal(
		dbus.WithMatchSender(logindDest),
		dbus.WithMatchInterface(logindInterface),
		dbus.WithMatchMember("PrepareForSleep"),
		dbus.WithMatchObjectPath(logindPath),
	); err != nil {
		_ = conn.Close()
		return func() {}
	}
	signals := make(chan *dbus.Signal, 4)
	conn.Signal(signals)
	inhibitor := &logindInhibitor{conn: conn}
	inhibitor.take()
	go func() {
		for {
			select {
			case <-stop:
				return
			case signal, ok := <-signals:
				if !ok {
					return
				}
				// Match rules filter broadcasts, but directed signals can
				// bypass them. Verify the sender against the bus's current
				// owner, including after logind restarts.
				var owner string
				if err := conn.BusObject().Call("org.freedesktop.DBus.GetNameOwner", 0, logindDest).Store(&owner); err != nil {
					continue
				}
				sleeping, isSleep := logindPrepareForSleep(signal, owner)
				if !isSleep {
					continue
				}
				if sleeping {
					event := newRunnerSleepEvent(inhibitor.releaser())
					// Never hold the host awake past the bound, even if the
					// session is between connections. Releasing also expires
					// the event so it cannot be replayed after a short sleep.
					time.AfterFunc(runnerSleepReleaseAfter, event.release)
					deliverRunnerPowerEvent(events, event)
				} else {
					inhibitor.take()
					deliverRunnerPowerEvent(events, runnerPowerEvent{kind: runnerPowerWake})
				}
			}
		}
	}()
	return func() {
		inhibitor.releaser()()
		_ = conn.Close()
	}
}

func deliverRunnerPowerEvent(events chan<- runnerPowerEvent, event runnerPowerEvent) {
	select {
	case events <- event:
	default:
		event.release()
	}
}

type logindInhibitor struct {
	conn *dbus.Conn
	mu   sync.Mutex
	fd   int
	held bool
	// gen counts takes, so a release armed for one sleep never drops the
	// delay taken again after the following wake.
	gen int
}

func (l *logindInhibitor) take() {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.held {
		return
	}
	var fd dbus.UnixFD
	err := l.conn.Object(logindDest, logindPath).Call(
		logindInterface+".Inhibit", 0,
		"sleep", "Preloop runner", "Report sleep to Preloop before suspending", "delay",
	).Store(&fd)
	if err != nil {
		return
	}
	l.fd, l.held = int(fd), true
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
			if l.held && l.gen == gen {
				_ = syscall.Close(l.fd)
				l.held = false
			}
		})
	}
}
