package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/spf13/pflag"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// onceServer is a control plane just complete enough to register a runner,
// hand it one job and observe what it sends back.
type onceServer struct {
	mu              sync.Mutex
	registerBodies  []map[string]any
	completions     []map[string]any
	unregisters     int
	connections     int
	job             map[string]any
	handshakeFrames []map[string]any
	// legacyControlPlane drops `ephemeral` from the hello frame, the way a
	// control plane without one-shot support does.
	legacyControlPlane bool
}

func (s *onceServer) snapshot() ([]map[string]any, []map[string]any, int, int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]map[string]any(nil), s.registerBodies...),
		append([]map[string]any(nil), s.completions...),
		s.unregisters, s.connections
}

func (s *onceServer) handshakes() []map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]map[string]any(nil), s.handshakeFrames...)
}

func newOnceServer(t *testing.T, job map[string]any) (*onceServer, *httptest.Server) {
	t.Helper()
	state := &onceServer{job: job}
	upgrader := websocket.Upgrader{CheckOrigin: func(r *http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/runners/register" {
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			state.mu.Lock()
			state.registerBodies = append(state.registerBodies, body)
			state.mu.Unlock()
			_ = json.NewEncoder(w).Encode(map[string]any{
				"id":         "11111111-1111-4111-8111-111111111111",
				"account_id": "22222222-2222-4222-8222-222222222222",
				"name":       "ci-box",
				"status":     "online",
				"token":      "runner-token",
				"created_at": "2026-09-16T00:00:00Z",
				"updated_at": "2026-09-16T00:00:00Z",
			})
			return
		}
		if !strings.HasSuffix(r.URL.Path, "/ws") {
			http.NotFound(w, r)
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close() //nolint:errcheck
		state.mu.Lock()
		state.connections++
		first := state.connections == 1
		state.mu.Unlock()
		hello := map[string]any{"type": "hello", "log_acknowledgements": true}
		state.mu.Lock()
		legacy := state.legacyControlPlane
		state.mu.Unlock()
		if !legacy {
			hello["ephemeral"] = true
		}
		if first && state.job != nil {
			hello["job"] = state.job
		}
		_ = conn.WriteJSON(hello)
		for {
			var msg map[string]any
			if err := conn.ReadJSON(&msg); err != nil {
				return
			}
			state.mu.Lock()
			switch msg["type"] {
			case "heartbeat":
				state.handshakeFrames = append(state.handshakeFrames, msg)
			case "complete":
				state.completions = append(state.completions, msg)
			case "unregister":
				state.unregisters++
			}
			state.mu.Unlock()
			if msg["type"] == "unregister" {
				_ = conn.WriteJSON(map[string]any{"type": "ack"})
				return
			}
		}
	}))
	t.Cleanup(server.Close)
	return state, server
}

// runFgFlags drives the real `runner fg` command with explicit flag values
// and restores the shared cobra command afterwards.
func runFgFlags(t *testing.T, out *bytes.Buffer, flags map[string]string) error {
	t.Helper()
	cmd := runnerFgCmd
	cmd.SetOut(out)
	t.Cleanup(func() {
		cmd.SetOut(nil)
		cmd.Flags().VisitAll(func(f *pflag.Flag) {
			if slice, ok := f.Value.(pflag.SliceValue); ok {
				_ = slice.Replace(nil)
			} else {
				_ = f.Value.Set(f.DefValue)
			}
			f.Changed = false
		})
		runnerOnce = nil
	})
	for name, value := range flags {
		if err := cmd.Flags().Set(name, value); err != nil {
			t.Fatalf("set --%s=%s: %v", name, value, err)
		}
	}
	return runRunnerFg(cmd, nil)
}

func TestRunnerOnceFlagsParseAndValidate(t *testing.T) {
	cmd := runnerFgCmd
	t.Cleanup(func() {
		cmd.Flags().VisitAll(func(f *pflag.Flag) {
			if _, ok := f.Value.(pflag.SliceValue); !ok {
				_ = f.Value.Set(f.DefValue)
			}
			f.Changed = false
		})
	})

	mode, err := runnerOnceFromFlags(cmd)
	if err != nil {
		t.Fatal(err)
	}
	if mode.once || mode.ephemeral {
		t.Fatalf("defaults = %+v, want both off", mode)
	}
	if mode.waitForJob != defaultRunnerWaitForJob {
		t.Fatalf("wait-for-job = %s, want %s", mode.waitForJob, defaultRunnerWaitForJob)
	}

	if err := cmd.Flags().Set("wait-for-job", "30s"); err != nil {
		t.Fatal(err)
	}
	if _, err := runnerOnceFromFlags(cmd); err == nil ||
		!strings.Contains(err.Error(), "requires --once") {
		t.Fatalf("wait-for-job without --once error = %v", err)
	}

	if err := cmd.Flags().Set("once", "true"); err != nil {
		t.Fatal(err)
	}
	mode, err = runnerOnceFromFlags(cmd)
	if err != nil {
		t.Fatal(err)
	}
	if !mode.once || mode.waitForJob != 30*time.Second {
		t.Fatalf("mode = %+v", mode)
	}

	if err := cmd.Flags().Set("wait-for-job", "0s"); err != nil {
		t.Fatal(err)
	}
	if _, err := runnerOnceFromFlags(cmd); err == nil ||
		!strings.Contains(err.Error(), "must be positive") {
		t.Fatalf("zero wait-for-job error = %v", err)
	}
}

func TestEphemeralRunnerLabelsDefaultAndOverride(t *testing.T) {
	got := ephemeralRunnerLabels(nil, "ci-host", 4242)
	if len(got) != 1 || got[0] != "ci-ci-host-4242" {
		t.Fatalf("default labels = %v", got)
	}
	if got := ephemeralRunnerLabels([]string{"  "}, "ci-host", 7); got[0] != "ci-ci-host-7" {
		t.Fatalf("blank labels = %v", got)
	}
	if got := ephemeralRunnerLabels([]string{"mine"}, "ci-host", 7); len(got) != 1 ||
		got[0] != "mine" {
		t.Fatalf("explicit labels = %v", got)
	}
	if got := ephemeralRunnerLabels(nil, "", 9); got[0] != "ci-runner-9" {
		t.Fatalf("empty hostname labels = %v", got)
	}
}

func TestRunnerOnceResultMapsStatusToExitCode(t *testing.T) {
	cases := []struct {
		name    string
		prepare func(m *runnerOnceMode)
		code    int
		message string
	}{
		{
			name:    "no job leased",
			prepare: func(m *runnerOnceMode) {},
			code:    runnerOnceExitNoJob,
			message: "no execution was leased within 30s",
		},
		{
			name:    "leased but never finished",
			prepare: func(m *runnerOnceMode) { m.markLeased("exec-1") },
			code:    runnerOnceExitFailed,
			message: "runner stopped before execution exec-1 finished",
		},
		{
			name: "failed",
			prepare: func(m *runnerOnceMode) {
				m.record(leasedJobOutcome{executionID: "exec-1", status: "FAILED"})
			},
			code:    runnerOnceExitFailed,
			message: "execution exec-1 FAILED",
		},
		{
			name: "stopped",
			prepare: func(m *runnerOnceMode) {
				m.record(leasedJobOutcome{executionID: "exec-1", status: "STOPPED"})
			},
			code:    runnerOnceExitFailed,
			message: "execution exec-1 STOPPED",
		},
		{
			name: "timeout",
			prepare: func(m *runnerOnceMode) {
				m.record(leasedJobOutcome{executionID: "exec-1", status: "TIMEOUT"})
			},
			code:    runnerOnceExitFailed,
			message: "execution exec-1 TIMEOUT",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			mode := &runnerOnceMode{once: true, waitForJob: 30 * time.Second}
			tc.prepare(mode)
			err := mode.result()
			if err == nil {
				t.Fatal("want a non-zero outcome")
			}
			if got := ProcessExitCode(err); got != tc.code {
				t.Fatalf("exit code = %d, want %d", got, tc.code)
			}
			if err.Error() != tc.message {
				t.Fatalf("message = %q, want %q", err.Error(), tc.message)
			}
		})
	}

	success := &runnerOnceMode{once: true, waitForJob: 30 * time.Second}
	success.record(leasedJobOutcome{executionID: "exec-1", status: "SUCCEEDED"})
	if err := success.result(); err != nil {
		t.Fatalf("succeeded result = %v", err)
	}
	if got := ProcessExitCode(success.result()); got != 0 {
		t.Fatalf("succeeded exit code = %d", got)
	}

	// Without --once the mode never changes the process status.
	off := &runnerOnceMode{waitForJob: time.Second}
	if err := off.result(); err != nil {
		t.Fatalf("non-once result = %v", err)
	}
	if off.record(leasedJobOutcome{executionID: "exec-1", status: "FAILED"}) {
		t.Fatal("non-once mode must not stop the loop")
	}
}

func TestRunnerOnceRecordsOnlyTheFirstTerminalReport(t *testing.T) {
	mode := &runnerOnceMode{once: true, waitForJob: time.Minute}
	if !mode.record(leasedJobOutcome{executionID: "exec-1", status: "FAILED"}) {
		t.Fatal("first terminal report should stop the loop")
	}
	// A replayed complete after a reconnect must not overwrite the verdict
	// or stop the loop twice.
	if mode.record(leasedJobOutcome{executionID: "exec-1", status: "SUCCEEDED"}) {
		t.Fatal("replayed report should not stop the loop again")
	}
	if got := mode.result().Error(); got != "execution exec-1 FAILED" {
		t.Fatalf("result = %q", got)
	}
	if mode.record(leasedJobOutcome{status: "FAILED"}) {
		t.Fatal("an outcome without an execution id is not a verdict")
	}
}

func TestRunnerFgOnceEphemeralRunsOneJobAndUnregisters(t *testing.T) {
	home := testenv.SetTempHome(t)
	persistent := filepath.Join(home, ".preloop", "runner.json")
	if err := os.MkdirAll(filepath.Dir(persistent), 0o700); err != nil {
		t.Fatal(err)
	}
	stored := []byte(`{"id":"persistent-id","token":"persistent-token","name":"desk"}`)
	if err := os.WriteFile(persistent, stored, 0o600); err != nil {
		t.Fatal(err)
	}

	state, server := newOnceServer(t, map[string]any{"execution_id": "exec-1"})
	oldToken, oldURL := FlagToken, FlagURL
	FlagURL, FlagToken = server.URL, "tok"
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })

	var out bytes.Buffer
	err := runFgFlags(t, &out, map[string]string{
		"once": "true", "ephemeral": "true", "wait-for-job": "20s",
	})
	if err == nil {
		t.Fatal("a FAILED execution must not exit 0")
	}
	if got := ProcessExitCode(err); got != runnerOnceExitFailed {
		t.Fatalf("exit code = %d, want %d (%v)", got, runnerOnceExitFailed, err)
	}
	if err.Error() != "execution exec-1 FAILED" {
		t.Fatalf("error = %q", err.Error())
	}

	// The control-plane handler records frames on its own goroutine. The
	// command can return as soon as the socket write completes, before that
	// handler has appended the completion.
	deadline := time.Now().Add(2 * time.Second)
	var registers []map[string]any
	var completions []map[string]any
	var unregisters int
	for {
		registers, completions, unregisters, _ = state.snapshot()
		if len(completions) > 0 && unregisters > 0 {
			break
		}
		if time.Now().After(deadline) {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	if len(registers) != 1 {
		t.Fatalf("register calls = %d", len(registers))
	}
	if registers[0]["ephemeral"] != true {
		t.Fatalf("register body = %v, want ephemeral true", registers[0])
	}
	if _, resumed := registers[0]["runner_id"]; resumed {
		t.Fatalf("ephemeral register must not resume a stored runner: %v", registers[0])
	}
	labels, _ := registers[0]["labels"].([]any)
	if len(labels) != 1 || !strings.HasPrefix(labels[0].(string), "ci-") {
		t.Fatalf("labels = %v, want one generated ci- label", labels)
	}
	if len(completions) != 1 || completions[0]["status"] != "FAILED" {
		t.Fatalf("completions = %v", completions)
	}
	if unregisters == 0 {
		t.Fatal("ephemeral runner never unregistered")
	}

	// The persistent runner identity on this machine is untouched.
	after, err := os.ReadFile(persistent)
	if err != nil {
		t.Fatal(err)
	}
	if string(after) != string(stored) {
		t.Fatalf("runner.json = %s, want it unchanged", after)
	}

	for _, frame := range state.handshakes() {
		if frame["ephemeral"] != true {
			t.Fatalf("heartbeat = %v, want ephemeral true", frame)
		}
	}
	if !strings.Contains(out.String(), "/console/flows/executions/exec-1") {
		t.Fatalf("stdout = %q, want the execution URL", out.String())
	}
}

func TestRunnerFgOnceExitsWhenNoJobArrives(t *testing.T) {
	testenv.SetTempHome(t)
	state, server := newOnceServer(t, nil)
	oldToken, oldURL := FlagToken, FlagURL
	FlagURL, FlagToken = server.URL, "tok"
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })

	var out bytes.Buffer
	err := runFgFlags(t, &out, map[string]string{
		"once": "true", "ephemeral": "true", "wait-for-job": "150ms",
	})
	if err == nil {
		t.Fatal("an idle one-shot runner must exit non-zero")
	}
	if got := ProcessExitCode(err); got != runnerOnceExitNoJob {
		t.Fatalf("exit code = %d, want %d (%v)", got, runnerOnceExitNoJob, err)
	}
	if !strings.Contains(err.Error(), "no execution was leased within 150ms") {
		t.Fatalf("error = %q", err.Error())
	}
	if _, _, unregisters, _ := state.snapshot(); unregisters == 0 {
		t.Fatal("an idle one-shot runner must leave no row behind")
	}
}

func TestRunnerExecutionURLUsesControlPlane(t *testing.T) {
	testenv.SetTempHome(t)
	oldToken, oldURL := FlagToken, FlagURL
	FlagURL, FlagToken = "https://preloop.example.com/", "tok"
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })
	want := "https://preloop.example.com/console/flows/executions/exec-1"
	if got := runnerExecutionURL("exec-1"); got != want {
		t.Fatalf("url = %q, want %q", got, want)
	}
}

func TestRunnerOnceWarnsWhenControlPlaneDoesNotConfirmEphemeral(t *testing.T) {
	// A control plane older than one-shot support ignores `ephemeral` on
	// register and never deletes the row. The hello echo is the only way
	// the CI job can find out, so it has to reach the log.
	const warning = "did not confirm ephemeral registration"
	for _, tc := range []struct {
		name   string
		legacy bool
		warns  bool
	}{
		{name: "current control plane", legacy: false, warns: false},
		{name: "control plane without one-shot support", legacy: true, warns: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			testenv.SetTempHome(t)
			state, server := newOnceServer(t, nil)
			state.legacyControlPlane = tc.legacy
			oldToken, oldURL := FlagToken, FlagURL
			FlagURL, FlagToken = server.URL, "tok"
			t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })

			var out bytes.Buffer
			_ = runFgFlags(t, &out, map[string]string{
				"once": "true", "ephemeral": "true", "wait-for-job": "150ms",
			})
			if got := strings.Contains(out.String(), warning); got != tc.warns {
				t.Fatalf("warning present = %v, want %v (output %q)",
					got, tc.warns, out.String())
			}
		})
	}
}

func TestRunnerOnceEphemeralWarningIsSilentOutsideEphemeralMode(t *testing.T) {
	var out bytes.Buffer
	mode := &runnerOnceMode{once: true, out: &out}
	mode.noteHelloEphemeral(false)
	if out.Len() != 0 {
		t.Fatalf("a persistent runner must not warn: %q", out.String())
	}
	// And an ephemeral one warns exactly once, however many times it
	// reconnects to the same old control plane.
	ephemeral := &runnerOnceMode{once: true, ephemeral: true, out: &out}
	ephemeral.noteHelloEphemeral(false)
	ephemeral.noteHelloEphemeral(false)
	if got := strings.Count(out.String(), "did not confirm"); got != 1 {
		t.Fatalf("warned %d times, want 1 (%q)", got, out.String())
	}
}
