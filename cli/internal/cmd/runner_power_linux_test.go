//go:build linux

package cmd

import (
	"testing"

	"github.com/godbus/dbus/v5"
)

func TestLogindPrepareForSleepSignal(t *testing.T) {
	name := logindInterface + ".PrepareForSleep"
	if sleeping, ok := logindPrepareForSleep(&dbus.Signal{Name: name, Body: []any{true}}); !ok || !sleeping {
		t.Fatal("PrepareForSleep(true) is a sleep")
	}
	if sleeping, ok := logindPrepareForSleep(&dbus.Signal{Name: name, Body: []any{false}}); !ok || sleeping {
		t.Fatal("PrepareForSleep(false) is a wake")
	}
	if _, ok := logindPrepareForSleep(&dbus.Signal{Name: "x.Other", Body: []any{true}}); ok {
		t.Fatal("other signals are ignored")
	}
	if _, ok := logindPrepareForSleep(&dbus.Signal{Name: name, Body: []any{"yes"}}); ok {
		t.Fatal("malformed body is ignored")
	}
}
