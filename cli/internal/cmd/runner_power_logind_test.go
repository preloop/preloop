package cmd

import (
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

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

func TestLogindCancelledSleepKeepsNextInhibitor(t *testing.T) {
	for _, wakeFirst := range []bool{false, true} {
		name := "queued sleep then wake"
		if wakeFirst {
			name = "wake drains queued sleep"
		}
		t.Run(name, func(t *testing.T) {
			var closes []*atomic.Int32
			inhibitor := &logindInhibitor{acquire: func() (func(), error) {
				closed := &atomic.Int32{}
				closes = append(closes, closed)
				return func() { closed.Add(1) }, nil
			}}
			inhibitor.take()
			t.Cleanup(func() { inhibitor.releaser()() })
			// logind may cancel a suspend before the session handles its
			// queued sleep event, so the original delay is still held.
			sleep := inhibitor.powerEvent(true)
			wake := inhibitor.powerEvent(false)
			events := make(chan runnerPowerEvent, 2)
			if wakeFirst {
				events <- wake
				events <- sleep
			} else {
				events <- sleep
				events <- wake
			}
			if ok, woke := waitForReconnect(nil, events, time.Minute); !ok || !woke {
				t.Fatalf("waitForReconnect = (%v, %v), want (true, true)", ok, woke)
			}
			if len(closes) != 2 {
				t.Fatalf("acquired %d inhibitors, want a fresh inhibitor after wake", len(closes))
			}
			if closes[0].Load() != 1 || closes[1].Load() != 0 {
				t.Fatal("queued sleep cleanup must close only the pre-wake inhibitor")
			}
			// The next sleep must retain its delay until its own event is
			// handled, even when the previous event's deadline also fires.
			next := inhibitor.powerEvent(true)
			sleep.release()
			if len(closes) != 2 || closes[1].Load() != 0 {
				t.Fatal("next sleep did not retain the inhibitor acquired on wake")
			}
			next.release()
			if closes[1].Load() != 1 {
				t.Fatal("next sleep event did not release its inhibitor")
			}
		})
	}
}

func TestLogindSleepRetriesFailedInhibitor(t *testing.T) {
	attempts := 0
	closed := 0
	inhibitor := &logindInhibitor{acquire: func() (func(), error) {
		attempts++
		if attempts == 1 {
			return nil, errors.New("system bus temporarily unavailable")
		}
		return func() { closed++ }, nil
	}}
	inhibitor.take()
	t.Cleanup(func() { inhibitor.releaser()() })
	sleep := inhibitor.powerEvent(true)
	if attempts != 2 {
		t.Fatalf("acquisition attempts = %d, want retry before sleep", attempts)
	}
	if closed != 0 {
		t.Fatal("sleep inhibitor released before the event was handled")
	}
	sleep.release()
	if closed != 1 {
		t.Fatalf("released %d inhibitors, want 1", closed)
	}
}

func TestLogindConcurrentOldReleasePreservesWakeInhibitor(t *testing.T) {
	for range 100 {
		var closes []*atomic.Int32
		inhibitor := &logindInhibitor{acquire: func() (func(), error) {
			closed := &atomic.Int32{}
			closes = append(closes, closed)
			return func() { closed.Add(1) }, nil
		}}
		inhibitor.take()
		sleep := inhibitor.powerEvent(true)
		// Both a session and the fallback timer may release the old
		// event while logind's wake notification refreshes the delay.
		var wg sync.WaitGroup
		for range 2 {
			wg.Add(1)
			go func() {
				defer wg.Done()
				sleep.release()
			}()
		}
		inhibitor.powerEvent(false)
		wg.Wait()
		if len(closes) != 2 || closes[0].Load() != 1 || closes[1].Load() != 0 {
			t.Fatal("concurrent old releases dropped the post-wake inhibitor")
		}
		inhibitor.releaser()()
		if closes[1].Load() != 1 {
			t.Fatal("current inhibitor was not released exactly once")
		}
	}
}
