package cmd

import "github.com/godbus/dbus/v5"

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
