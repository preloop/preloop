package cmd

import (
	"testing"

	"github.com/godbus/dbus/v5"
)

func TestLogindPrepareForSleepSignal(t *testing.T) {
	const owner = ":1.42"
	name := logindInterface + ".PrepareForSleep"
	for _, tc := range []struct {
		name     string
		signal   *dbus.Signal
		owner    string
		sleeping bool
		ok       bool
	}{
		{"sleep", &dbus.Signal{Sender: owner, Name: name, Body: []any{true}}, owner, true, true},
		{"wake", &dbus.Signal{Sender: owner, Name: name, Body: []any{false}}, owner, false, true},
		{"forged sleep", &dbus.Signal{Sender: ":1.99", Name: name, Body: []any{true}}, owner, false, false},
		{"forged wake", &dbus.Signal{Sender: ":1.99", Name: name, Body: []any{false}}, owner, false, false},
		{"missing sender", &dbus.Signal{Name: name, Body: []any{true}}, owner, false, false},
		{"missing owner", &dbus.Signal{Name: name, Body: []any{true}}, "", false, false},
		{"previous owner", &dbus.Signal{Sender: owner, Name: name, Body: []any{true}}, ":1.43", false, false},
		{"other signal", &dbus.Signal{Sender: owner, Name: "x.Other", Body: []any{true}}, owner, false, false},
		{"malformed body", &dbus.Signal{Sender: owner, Name: name, Body: []any{"yes"}}, owner, false, false},
		{"missing body", &dbus.Signal{Sender: owner, Name: name}, owner, false, false},
		{"nil signal", nil, owner, false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			sleeping, ok := logindPrepareForSleep(tc.signal, tc.owner)
			if sleeping != tc.sleeping || ok != tc.ok {
				t.Fatalf("logindPrepareForSleep = (%v, %v), want (%v, %v)", sleeping, ok, tc.sleeping, tc.ok)
			}
		})
	}
}
