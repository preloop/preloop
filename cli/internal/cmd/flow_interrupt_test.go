package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

const interruptFlowID = "11111111-2222-4333-8444-555555555555"

// fakeFlowAPI serves trigger, status, logs, and the stop command. The status
// stays RUNNING until a stop arrives, and onStatus runs on every status read
// so a test can deliver a signal mid-wait.
type fakeFlowAPI struct {
	mu          sync.Mutex
	stopCalls   int
	stopBodies  []map[string]any
	stopReply   map[string]any
	stopStatus  int
	finalStatus string
	onStatus    func(reads int)
	statusReads int
}

func (f *fakeFlowAPI) handler(t *testing.T) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		switch {
		case r.URL.Path == "/api/v1/flows":
			_ = json.NewEncoder(w).Encode([]flowSummaryResponse{{ID: interruptFlowID, Name: "PR Review"}})
		case r.URL.Path == "/api/v1/flows/"+interruptFlowID+"/trigger":
			_ = json.NewEncoder(w).Encode(flowTriggerResult{ID: "exec-9", Status: "PENDING", FlowID: interruptFlowID})
		case strings.HasPrefix(r.URL.Path, "/api/v1/flows/executions/exec-9/logs"):
			_ = json.NewEncoder(w).Encode(flowLogsResponse{Source: "database"})
		case r.URL.Path == "/api/v1/flows/executions/exec-9/command":
			if r.Method != http.MethodPost {
				t.Errorf("stop method = %s", r.Method)
			}
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			f.stopCalls++
			f.stopBodies = append(f.stopBodies, body)
			if f.stopStatus != 0 {
				w.WriteHeader(f.stopStatus)
				_, _ = w.Write([]byte(`{"detail":"boom"}`))
				return
			}
			if f.finalStatus == "" {
				f.finalStatus = "STOPPED"
			}
			reply := f.stopReply
			if reply == nil {
				reply = map[string]any{"status": "stopped"}
			}
			_ = json.NewEncoder(w).Encode(reply)
		case r.URL.Path == "/api/v1/flows/executions/exec-9":
			f.statusReads++
			status := "RUNNING"
			if f.finalStatus != "" {
				status = f.finalStatus
			}
			if f.statusReads > 20 {
				// Nothing ended the wait: finish it so the test fails on its
				// assertions instead of polling until the go test timeout.
				status = "FAILED"
			}
			if f.onStatus != nil {
				f.onStatus(f.statusReads)
			}
			_ = json.NewEncoder(w).Encode(flowExecutionStatus{ID: "exec-9", Status: status})
		default:
			http.NotFound(w, r)
		}
	}
}

// setupInterruptTrigger points the CLI at the fake API, resets the trigger
// flags to their defaults (pflag keeps Changed from earlier tests), and
// replaces signal delivery with a channel the test controls.
func setupInterruptTrigger(t *testing.T, fake *fakeFlowAPI, tty bool) (chan os.Signal, *int) {
	t.Helper()
	server := httptest.NewServer(fake.handler(t))
	t.Cleanup(server.Close)

	testenv.SetHome(t, t.TempDir())
	oldToken, oldURL := FlagToken, FlagURL
	FlagToken, FlagURL = "tok", server.URL

	oldTerminal := stdinIsTerminal
	stdinIsTerminal = func() bool { return tty }

	signals := make(chan os.Signal, 4)
	subscriptions := 0
	oldNotify := flowNotifyInterrupts
	flowNotifyInterrupts = func() (<-chan os.Signal, func()) {
		subscriptions++
		return signals, func() {}
	}
	oldSleep, oldAfter := flowSleep, flowAfter
	flowSleep = func(time.Duration) {}
	// Polls never time out on their own in these tests: the wait ends on a
	// terminal status or on a signal, so select cannot pick the timer over a
	// queued signal by chance.
	flowAfter = func(time.Duration) <-chan time.Time { return make(chan time.Time) }

	resetFlowTriggerFlags(t)
	t.Cleanup(func() {
		FlagToken, FlagURL = oldToken, oldURL
		stdinIsTerminal = oldTerminal
		flowNotifyInterrupts = oldNotify
		flowSleep, flowAfter = oldSleep, oldAfter
		resetFlowTriggerFlags(t)
	})
	return signals, &subscriptions
}

func resetFlowTriggerFlags(t *testing.T) {
	t.Helper()
	for name, value := range map[string]string{
		"payload": "", "wait": "false", "runner": "", "stop-on-interrupt": "false",
	} {
		if err := flowTriggerCmd.Flags().Set(name, value); err != nil {
			t.Fatal(err)
		}
		flowTriggerCmd.Flags().Lookup(name).Changed = false
	}
}

func TestFlowTriggerInterruptInCIStopsExecutionOnce(t *testing.T) {
	fake := &fakeFlowAPI{}
	signals, subscriptions := setupInterruptTrigger(t, fake, false)
	fake.onStatus = func(reads int) {
		if reads == 1 {
			// A runner cancelling a job often sends SIGINT and then SIGTERM.
			signals <- os.Interrupt
			signals <- syscall.SIGTERM
		}
	}

	var out bytes.Buffer
	flowTriggerCmd.SetOut(&out)
	err := runFlowTrigger(flowTriggerCmd, []string{"PR Review"})

	if *subscriptions != 1 {
		t.Fatalf("signal subscriptions = %d, want 1 (default on without a TTY)", *subscriptions)
	}
	if fake.stopCalls != 1 {
		t.Fatalf("stop calls = %d, want exactly 1", fake.stopCalls)
	}
	if fake.stopBodies[0]["command"] != "stop" {
		t.Fatalf("stop body = %#v", fake.stopBodies[0])
	}
	if err == nil {
		t.Fatal("expected a non-zero exit after an interrupt")
	}
	if code := ProcessExitCode(err); code != 130 {
		t.Fatalf("exit code = %d, want 130", code)
	}
	if !strings.Contains(out.String(), "Stopped execution exec-9 (final status STOPPED)") {
		t.Fatalf("output = %q", out.String())
	}
	if !strings.Contains(err.Error(), "exec-9") || !strings.Contains(err.Error(), "STOPPED") {
		t.Fatalf("error = %v", err)
	}
}

func TestFlowTriggerSIGTERMReportsAlreadyFinishedRun(t *testing.T) {
	fake := &fakeFlowAPI{
		stopReply:   map[string]any{"status": "not_running", "execution_status": "SUCCEEDED"},
		finalStatus: "",
	}
	signals, _ := setupInterruptTrigger(t, fake, false)
	fake.onStatus = func(reads int) {
		if reads == 1 {
			signals <- syscall.SIGTERM
		}
	}

	var out bytes.Buffer
	flowTriggerCmd.SetOut(&out)
	err := runFlowTrigger(flowTriggerCmd, []string{"PR Review"})

	if fake.stopCalls != 1 {
		t.Fatalf("stop calls = %d, want exactly 1", fake.stopCalls)
	}
	if code := ProcessExitCode(err); code != 143 {
		t.Fatalf("exit code = %d, want 143 (err %v)", code, err)
	}
	if !strings.Contains(out.String(), "Execution exec-9 had already finished (final status SUCCEEDED)") {
		t.Fatalf("output = %q", out.String())
	}
}

func TestFlowTriggerStopFailureStillExitsNonZero(t *testing.T) {
	fake := &fakeFlowAPI{stopStatus: http.StatusInternalServerError}
	signals, _ := setupInterruptTrigger(t, fake, false)
	fake.onStatus = func(reads int) {
		if reads == 1 {
			signals <- os.Interrupt
		}
	}

	flowTriggerCmd.SetOut(&bytes.Buffer{})
	err := runFlowTrigger(flowTriggerCmd, []string{"PR Review"})

	if fake.stopCalls != 1 {
		t.Fatalf("stop calls = %d, want exactly 1", fake.stopCalls)
	}
	var coded *processExitError
	if !errors.As(err, &coded) || coded.ExitCode() != 130 {
		t.Fatalf("err = %v, want exit 130", err)
	}
	if !strings.Contains(err.Error(), "may still be running") {
		t.Fatalf("error = %v", err)
	}
}

func TestFlowTriggerOnTTYLeavesSignalsAloneByDefault(t *testing.T) {
	fake := &fakeFlowAPI{}
	_, subscriptions := setupInterruptTrigger(t, fake, true)
	fake.onStatus = func(reads int) {
		if reads == 2 {
			fake.finalStatus = "SUCCEEDED"
		}
	}
	if err := flowTriggerCmd.Flags().Set("wait", "true"); err != nil {
		t.Fatal(err)
	}

	flowTriggerCmd.SetOut(&bytes.Buffer{})
	if err := runFlowTrigger(flowTriggerCmd, []string{"PR Review"}); err != nil {
		t.Fatalf("runFlowTrigger: %v", err)
	}
	if *subscriptions != 0 {
		t.Fatalf("signal subscriptions = %d, want 0 on a TTY", *subscriptions)
	}
	if fake.stopCalls != 0 {
		t.Fatalf("stop calls = %d, want 0", fake.stopCalls)
	}
}

func TestFlowTriggerStopOnInterruptFlagOverridesDefaults(t *testing.T) {
	t.Run("on for a TTY", func(t *testing.T) {
		fake := &fakeFlowAPI{}
		signals, subscriptions := setupInterruptTrigger(t, fake, true)
		fake.onStatus = func(reads int) {
			if reads == 1 {
				signals <- os.Interrupt
			}
		}
		for name, value := range map[string]string{"wait": "true", "stop-on-interrupt": "true"} {
			if err := flowTriggerCmd.Flags().Set(name, value); err != nil {
				t.Fatal(err)
			}
		}
		flowTriggerCmd.SetOut(&bytes.Buffer{})
		err := runFlowTrigger(flowTriggerCmd, []string{"PR Review"})
		if *subscriptions != 1 || fake.stopCalls != 1 || ProcessExitCode(err) != 130 {
			t.Fatalf("subscriptions=%d stops=%d err=%v", *subscriptions, fake.stopCalls, err)
		}
	})
	t.Run("off in CI", func(t *testing.T) {
		fake := &fakeFlowAPI{}
		_, subscriptions := setupInterruptTrigger(t, fake, false)
		fake.onStatus = func(reads int) {
			if reads == 2 {
				fake.finalStatus = "SUCCEEDED"
			}
		}
		if err := flowTriggerCmd.Flags().Set("stop-on-interrupt", "false"); err != nil {
			t.Fatal(err)
		}
		flowTriggerCmd.SetOut(&bytes.Buffer{})
		if err := runFlowTrigger(flowTriggerCmd, []string{"PR Review"}); err != nil {
			t.Fatalf("runFlowTrigger: %v", err)
		}
		if *subscriptions != 0 || fake.stopCalls != 0 {
			t.Fatalf("subscriptions=%d stops=%d", *subscriptions, fake.stopCalls)
		}
	})
}

func TestWaitForExecutionUntilSignalDuringBackoff(t *testing.T) {
	fake := &fakeFlowAPI{}
	signals, _ := setupInterruptTrigger(t, fake, false)
	// The signal lands while the CLI sleeps between polls, not before a read.
	flowAfter = func(time.Duration) <-chan time.Time {
		signals <- os.Interrupt
		return make(chan time.Time)
	}

	var out bytes.Buffer
	client := newTestFlowClient(FlagURL)
	err := waitForExecutionUntil(client, "exec-9", time.Minute, &out, signals)
	if fake.stopCalls != 1 || fake.statusReads != 2 {
		t.Fatalf("stops=%d status reads=%d, want 1 and 2", fake.stopCalls, fake.statusReads)
	}
	if ProcessExitCode(err) != 130 {
		t.Fatalf("err = %v", err)
	}
}

func newTestFlowClient(url string) *api.Client {
	return api.NewClientWithToken(url, "tok")
}
