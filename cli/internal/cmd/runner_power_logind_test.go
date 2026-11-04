package cmd

import (
	"errors"
	"io"
	"os"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/godbus/dbus/v5"
	"github.com/preloop/preloop/cli/internal/testenv"
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

func TestLogindDelayedSleepDoesNotReplayAfterWake(t *testing.T) {
	for _, lookup := range []string{"owner", "state", "after wake"} {
		blockedLookup := lookup != "after wake"
		name := lookup + " lookup returns after wake drain"
		t.Run(name, func(t *testing.T) {
			testenv.SetTempHome(t)
			withHeartbeatEvery(t, 25*time.Millisecond)
			epoch := &runnerPowerEpoch{}
			inhibitor := &logindInhibitor{acquire: func() (func(), error) {
				return func() {}, nil
			}}
			inhibitor.take()
			t.Cleanup(func() { inhibitor.releaser()() })
			entered := make(chan struct{})
			unblock := make(chan struct{})
			var unblockOnce sync.Once
			resume := func() { unblockOnce.Do(func() { close(unblock) }) }
			t.Cleanup(resume)
			var preparing atomic.Bool
			preparing.Store(blockedLookup)
			var enterOnce sync.Once
			source := &logindPowerSource{
				inhibitor: inhibitor,
				epoch:     epoch,
				owner: func() (string, error) {
					if lookup == "owner" {
						enterOnce.Do(func() { close(entered) })
						<-unblock
					}
					return ":1.42", nil
				},
				preparingForSleep: func(owner string) (bool, error) {
					if owner != ":1.42" {
						t.Errorf("state queried from unauthenticated owner %q", owner)
					}
					if lookup == "state" {
						enterOnce.Do(func() { close(entered) })
						<-unblock
					}
					return preparing.Load(), nil
				},
			}
			signal := &dbus.Signal{Sender: ":1.42", Name: logindInterface + ".PrepareForSleep", Body: []any{true}}
			type result struct {
				event runnerPowerEvent
				ok    bool
			}
			pending := make(chan result, 1)
			if blockedLookup {
				go func() {
					event, ok := source.event(signal)
					pending <- result{event, ok}
				}()
				select {
				case <-entered:
				case <-time.After(2 * time.Second):
					t.Fatal("bus lookup did not start")
				}
			}
			events := make(chan runnerPowerEvent, 4)
			jobs := newRunnerJobs(1)
			jobs.power = events
			interrupt := make(chan os.Signal, 1)
			wokeAt := time.Now()
			events <- epoch.wakeEvent()
			server, _, _ := jobServer(t, map[string]any{"type": "hello"})
			conn := dialJobServer(t, server)
			if err := runRunnerSession(conn, interrupt, io.Discard, jobs); !errors.Is(err, errRunnerWoke) {
				t.Fatalf("session ended with %v, want errRunnerWoke", err)
			}
			_ = conn.Close()
			if len(events) != 0 {
				t.Fatal("wake did not finish draining the power queue")
			}
			resume()
			if !blockedLookup {
				event, ok := source.event(signal)
				pending <- result{event, ok}
			}
			var delayed result
			select {
			case delayed = <-pending:
			case <-time.After(2 * time.Second):
				t.Fatal("delayed signal handling did not finish")
			}
			if blockedLookup {
				if !delayed.ok {
					t.Fatal("expected the delayed observation to reach the session")
				}
				if delayed.event.at.After(wokeAt) {
					t.Fatal("sleep timestamp was reset after the blocking lookup")
				}
				select {
				case <-delayed.event.released:
					t.Fatal("regression must deliver an unreleased sleep event")
				default:
				}
			} else if delayed.ok {
				t.Error("sleep signal first dequeued while awake was accepted")
			}
			if delayed.ok {
				events <- delayed.event
			}
			server, messages, _ := jobServer(t, map[string]any{"type": "hello"})
			conn = dialJobServer(t, server)
			stopped := make(chan error, 1)
			go func() { stopped <- runRunnerSession(conn, interrupt, io.Discard, jobs) }()
			t.Cleanup(func() {
				interrupt <- os.Interrupt
				select {
				case <-stopped:
				case <-time.After(2 * time.Second):
					t.Error("session did not stop")
				}
			})
			deadline := time.After(2 * time.Second)
			for heartbeats := 0; heartbeats < 3; {
				select {
				case message := <-messages:
					if message["type"] == "going_to_sleep" {
						t.Fatal("delayed sleep stopped the reconnected session's heartbeats")
					}
					if message["type"] == "heartbeat" {
						heartbeats++
					}
				case <-deadline:
					t.Fatal("heartbeats did not continue after wake")
				}
			}
			// A genuine later suspend belongs to the current generation.
			preparing.Store(true)
			next, ok := source.event(signal)
			if !ok || next.stale(time.Now()) {
				t.Fatal("a subsequent real suspend was discarded")
			}
			events <- next
			waitForMessageType(t, messages, "going_to_sleep")
		})
	}
}

func TestLogindFailedInhibitorExpiresSleepEvent(t *testing.T) {
	inhibitor := &logindInhibitor{acquire: func() (func(), error) {
		return nil, errors.New("delay unavailable")
	}}
	event := inhibitor.powerEvent(true)
	if !event.stale(time.Now()) {
		t.Fatal("sleep without an inhibitor could be reported after suspend")
	}
}

func TestLogindPowerSourceRequiresAuthenticatedSleepState(t *testing.T) {
	for _, scenario := range []string{"awake", "state unavailable", "forged sender"} {
		t.Run(scenario, func(t *testing.T) {
			queries := 0
			source := &logindPowerSource{
				epoch: &runnerPowerEpoch{},
				inhibitor: &logindInhibitor{acquire: func() (func(), error) {
					t.Fatal("unverified sleep should not acquire a new delay")
					return nil, nil
				}},
				owner: func() (string, error) { return ":1.42", nil },
				preparingForSleep: func(string) (bool, error) {
					queries++
					if scenario == "state unavailable" {
						return false, errors.New("logind unavailable")
					}
					return false, nil
				},
			}
			signal := &dbus.Signal{Sender: ":1.42", Name: logindInterface + ".PrepareForSleep", Body: []any{true}}
			if scenario == "forged sender" {
				signal.Sender = ":1.99"
			}
			if _, ok := source.event(signal); ok {
				t.Fatal("unverified sleep signal was accepted")
			}
			if scenario == "forged sender" && queries != 0 {
				t.Fatal("forged signal reached the state lookup")
			}
		})
	}
}
