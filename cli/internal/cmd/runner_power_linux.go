//go:build linux

package cmd

import (
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
	inhibitor := &logindInhibitor{acquire: func() (func(), error) {
		var fd dbus.UnixFD
		err := conn.Object(logindDest, logindPath).Call(
			logindInterface+".Inhibit", 0,
			"sleep", "Preloop runner", "Report sleep to Preloop before suspending", "delay",
		).Store(&fd)
		if err != nil {
			return nil, err
		}
		return func() { _ = syscall.Close(int(fd)) }, nil
	}}
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
				event := inhibitor.powerEvent(sleeping)
				if sleeping {
					// Never hold the host awake past the bound, even if the
					// session is between connections. Releasing also expires
					// the event so it cannot be replayed after a short sleep.
					time.AfterFunc(runnerSleepReleaseAfter, event.release)
				}
				deliverRunnerPowerEvent(events, event)
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
