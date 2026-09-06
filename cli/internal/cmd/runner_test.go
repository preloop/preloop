package cmd

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestRunnerWebsocketURL(t *testing.T) {
	testenv.SetTempHome(t)
	oldToken, oldURL := FlagToken, FlagURL
	FlagURL = "http://localhost:8000"
	FlagToken = "tok"
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })
	got, err := runnerWebsocketURL("rid")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(got, "ws://localhost:8000/api/v1/runners/rid/ws") {
		t.Fatalf("url = %s", got)
	}
	if strings.Contains(got, "token=") {
		t.Fatalf("token must not be in query: %s", got)
	}
}

func TestLeasedJobFailureReason(t *testing.T) {
	if got := leasedJobFailureReason(map[string]any{}, true); got == "" {
		t.Fatal("empty job should fail")
	}
	if got := leasedJobFailureReason(map[string]any{
		"agent_config": map[string]any{"image": "preloop/agent:dev"},
	}, false); !strings.Contains(got, "docker") {
		t.Fatalf("missing docker = %q", got)
	}
	if got := leasedJobFailureReason(map[string]any{
		"agent_config": map[string]any{"image": "preloop/agent:dev"},
	}, true); got != "" {
		t.Fatalf("runnable job = %q", got)
	}
}

func TestRunnerImageFromJob(t *testing.T) {
	if got := runnerImageFromJob(map[string]any{}); got != "" {
		t.Fatalf("empty job image = %q", got)
	}
	if got := runnerImageFromJob(map[string]any{
		"agent_config": map[string]any{"image": "preloop/agent:dev"},
	}); got != "preloop/agent:dev" {
		t.Fatalf("image = %q", got)
	}
}

func TestRunnerJobEnvMatchesHostedContract(t *testing.T) {
	job := map[string]any{
		"execution_id":      "exec-1",
		"flow_id":           "flow-1",
		"prompt":            "review the PR",
		"model_identifier":  "claude-sonnet-4-5",
		"model_provider":    "anthropic",
		"account_api_token": "secret-token",
		"agent_config":      map[string]any{"image": "preloop/agent:dev"},
	}
	env := runnerJobEnv(job, "https://review.preloop.ai")
	want := map[string]string{
		"EXECUTION_ID":         "exec-1",
		"FLOW_ID":              "flow-1",
		"AGENT_PROMPT":         "review the PR",
		"AI_MODEL":             "claude-sonnet-4-5",
		"AI_MODEL_PROVIDER":    "anthropic",
		"PRELOOP_API_TOKEN":    "secret-token",
		"PRELOOP_URL":          "https://review.preloop.ai",
		"AGENT_CONFIG":         `{"image":"preloop/agent:dev"}`,
		"COMPOSE_PROJECT_NAME": "preloop-exec1",
	}
	if len(env) != len(want) {
		t.Fatalf("env = %v, want %v", env, want)
	}
	for key, value := range want {
		if env[key] != value {
			t.Fatalf("env[%s] = %q, want %q", key, env[key], value)
		}
	}
}

func TestRunnerJobEnvSkipsMissingFields(t *testing.T) {
	env := runnerJobEnv(map[string]any{"execution_id": "exec-1"}, "")
	if env["EXECUTION_ID"] != "exec-1" || env["COMPOSE_PROJECT_NAME"] != "preloop-exec1" {
		t.Fatalf("env = %v", env)
	}
	if len(env) != 2 {
		t.Fatalf("env = %v", env)
	}
}

func TestDockerRunArgsUsesBareEnvFlags(t *testing.T) {
	args := dockerRunArgs("preloop/agent:dev", map[string]string{
		"PRELOOP_API_TOKEN": "secret-token",
		"EXECUTION_ID":      "exec-1",
	}, runnerDockerOpts{})
	want := []string{
		"run", "--rm",
		"-e", "EXECUTION_ID",
		"-e", "PRELOOP_API_TOKEN",
		"preloop/agent:dev",
	}
	if len(args) != len(want) {
		t.Fatalf("args = %v, want %v", args, want)
	}
	for i := range want {
		if args[i] != want[i] {
			t.Fatalf("args[%d] = %q, want %q", i, args[i], want[i])
		}
	}
	for _, arg := range args {
		if strings.Contains(arg, "secret-token") {
			t.Fatalf("secret leaked into argv: %v", args)
		}
	}
}

func TestFormatJobEnv(t *testing.T) {
	pairs := formatJobEnv(map[string]string{"B": "2", "A": "1"})
	if len(pairs) != 2 || pairs[0] != "A=1" || pairs[1] != "B=2" {
		t.Fatalf("pairs = %v", pairs)
	}
}

func TestRunnerStateRoundTrip(t *testing.T) {
	testenv.SetTempHome(t)
	state := &runnerState{ID: "abc", Token: "tok", Name: "box"}
	if err := writeRunnerState(state); err != nil {
		t.Fatal(err)
	}
	got, err := readRunnerState()
	if err != nil {
		t.Fatal(err)
	}
	if got.ID != "abc" || got.Token != "tok" || got.Name != "box" {
		t.Fatalf("state = %#v", got)
	}
}

func TestLoadOrRegisterRunnerCreates(t *testing.T) {
	testenv.SetTempHome(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/runners/register" {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":                    "11111111-1111-4111-8111-111111111111",
			"account_id":            "22222222-2222-4222-8222-222222222222",
			"name":                  "box",
			"status":                "online",
			"labels":                []string{"local"},
			"token":                 "runner-token",
			"created_at":            "2026-08-17T00:00:00Z",
			"updated_at":            "2026-08-17T00:00:00Z",
			"registered_by_user_id": nil,
		})
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	state, err := loadOrRegisterRunner(client, "box", "host", []string{"local"})
	if err != nil {
		t.Fatal(err)
	}
	if state.ID == "" || state.Token != "runner-token" {
		t.Fatalf("state = %#v", state)
	}
}

func TestRunnerFgLeasesJob(t *testing.T) {
	testenv.SetTempHome(t)
	var completed atomic.Bool
	upgrader := websocket.Upgrader{CheckOrigin: func(r *http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/runners/register" {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"id":         "11111111-1111-4111-8111-111111111111",
				"account_id": "22222222-2222-4222-8222-222222222222",
				"name":       "box",
				"status":     "online",
				"token":      "runner-token",
				"created_at": "2026-08-17T00:00:00Z",
				"updated_at": "2026-08-17T00:00:00Z",
			})
			return
		}
		if !strings.HasSuffix(r.URL.Path, "/ws") {
			http.NotFound(w, r)
			return
		}
		if r.URL.Query().Get("token") != "" {
			http.Error(w, "token must not be in query", http.StatusBadRequest)
			return
		}
		if r.Header.Get("X-Runner-Token") != "runner-token" {
			http.Error(w, "missing x-runner-token", http.StatusUnauthorized)
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close() //nolint:errcheck
		_ = conn.WriteJSON(map[string]any{
			"type": "hello",
			"job":  map[string]any{"execution_id": "exec-1"},
		})
		for {
			var msg map[string]any
			if err := conn.ReadJSON(&msg); err != nil {
				return
			}
			if msg["type"] == "complete" && msg["execution_id"] == "exec-1" {
				if msg["status"] != "FAILED" {
					return
				}
				completed.Store(true)
				return
			}
		}
	}))
	defer server.Close()

	oldToken, oldURL := FlagToken, FlagURL
	FlagURL = server.URL
	FlagToken = "tok"
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })
	client := api.NewClientWithToken(server.URL, "tok")
	state, err := loadOrRegisterRunner(client, "box", "host", nil)
	if err != nil {
		t.Fatal(err)
	}

	interrupt := make(chan os.Signal, 1)
	done := make(chan error, 1)
	go func() {
		done <- runnerForegroundLoop(state, interrupt, io.Discard)
	}()

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if completed.Load() {
			interrupt <- os.Interrupt
			select {
			case <-done:
			case <-time.After(time.Second):
			}
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	interrupt <- os.Interrupt
	t.Fatal("runner did not complete the leased job")
}

func TestRequestJobHaltLatch(t *testing.T) {
	halted := &atomic.Bool{}

	if requestJobHalt(halted, nil) {
		t.Fatal("idle halt should report nothing killed")
	}
	if halted.Load() {
		t.Fatal("idle halt must not latch")
	}

	halted.Store(true)
	if requestJobHalt(halted, &exec.Cmd{}) {
		t.Fatal("finished job with no process should report nothing killed")
	}
	if halted.Load() {
		t.Fatal("kill with nothing running must reset halted")
	}

	cmd := exec.Command("sleep", "30")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
			_, _ = cmd.Process.Wait()
		}
	})
	if !requestJobHalt(halted, cmd) {
		t.Fatal("running job should be killed")
	}
	if !halted.Load() {
		t.Fatal("halted must latch only while a job is running")
	}
}

func TestRunnerFgStartsLeaseAfterIdleHalt(t *testing.T) {
	testenv.SetTempHome(t)
	var completed atomic.Bool
	upgrader := websocket.Upgrader{CheckOrigin: func(r *http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/runners/register" {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"id":         "11111111-1111-4111-8111-111111111111",
				"account_id": "22222222-2222-4222-8222-222222222222",
				"name":       "box",
				"status":     "online",
				"token":      "runner-token",
				"created_at": "2026-08-17T00:00:00Z",
				"updated_at": "2026-08-17T00:00:00Z",
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
		_ = conn.WriteJSON(map[string]any{
			"type":              "halt",
			"halt":              true,
			"halt_execution_id": "already-finished",
		})
		_ = conn.WriteJSON(map[string]any{
			"type": "hello",
			"job":  map[string]any{"execution_id": "exec-after-halt"},
		})
		for {
			var msg map[string]any
			if err := conn.ReadJSON(&msg); err != nil {
				return
			}
			if msg["type"] == "complete" && msg["execution_id"] == "exec-after-halt" {
				if msg["status"] == "STOPPED" {
					return
				}
				if msg["status"] != "FAILED" {
					return
				}
				completed.Store(true)
				return
			}
		}
	}))
	defer server.Close()

	oldToken, oldURL := FlagToken, FlagURL
	FlagURL = server.URL
	FlagToken = "tok"
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })
	client := api.NewClientWithToken(server.URL, "tok")
	state, err := loadOrRegisterRunner(client, "box", "host", nil)
	if err != nil {
		t.Fatal(err)
	}

	interrupt := make(chan os.Signal, 1)
	done := make(chan error, 1)
	go func() {
		done <- runnerForegroundLoop(state, interrupt, io.Discard)
	}()

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if completed.Load() {
			interrupt <- os.Interrupt
			select {
			case <-done:
			case <-time.After(time.Second):
			}
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	interrupt <- os.Interrupt
	t.Fatal("idle halt latched; later lease did not start")
}

func useFastRunnerReconnect(t *testing.T) {
	t.Helper()
	oldMin, oldMax := runnerReconnectMin, runnerReconnectMax
	runnerReconnectMin = 10 * time.Millisecond
	runnerReconnectMax = 50 * time.Millisecond
	t.Cleanup(func() {
		runnerReconnectMin, runnerReconnectMax = oldMin, oldMax
	})
}

func TestNextRunnerBackoffCaps(t *testing.T) {
	oldMin, oldMax := runnerReconnectMin, runnerReconnectMax
	runnerReconnectMin = time.Second
	runnerReconnectMax = 30 * time.Second
	t.Cleanup(func() {
		runnerReconnectMin, runnerReconnectMax = oldMin, oldMax
	})
	if got := nextRunnerBackoff(time.Second); got != 2*time.Second {
		t.Fatalf("backoff = %s", got)
	}
	if got := nextRunnerBackoff(30 * time.Second); got != 30*time.Second {
		t.Fatalf("capped backoff = %s", got)
	}
}

func TestRunnerFgReconnectsAfterAbnormalClose(t *testing.T) {
	testenv.SetTempHome(t)
	useFastRunnerReconnect(t)
	var completed atomic.Bool
	var conns atomic.Int32
	upgrader := websocket.Upgrader{CheckOrigin: func(r *http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/runners/register" {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"id":         "11111111-1111-4111-8111-111111111111",
				"account_id": "22222222-2222-4222-8222-222222222222",
				"name":       "box",
				"status":     "online",
				"token":      "runner-token",
				"created_at": "2026-08-17T00:00:00Z",
				"updated_at": "2026-08-17T00:00:00Z",
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
		n := conns.Add(1)
		if n == 1 {
			_ = conn.Close()
			return
		}
		defer conn.Close() //nolint:errcheck
		_ = conn.WriteJSON(map[string]any{
			"type": "hello",
			"job":  map[string]any{"execution_id": "exec-after-drop"},
		})
		for {
			var msg map[string]any
			if err := conn.ReadJSON(&msg); err != nil {
				return
			}
			if msg["type"] == "complete" && msg["execution_id"] == "exec-after-drop" {
				completed.Store(true)
				return
			}
		}
	}))
	defer server.Close()

	oldToken, oldURL := FlagToken, FlagURL
	FlagURL = server.URL
	FlagToken = "tok"
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })
	client := api.NewClientWithToken(server.URL, "tok")
	state, err := loadOrRegisterRunner(client, "box", "host", nil)
	if err != nil {
		t.Fatal(err)
	}

	interrupt := make(chan os.Signal, 1)
	done := make(chan error, 1)
	go func() {
		done <- runnerForegroundLoop(state, interrupt, io.Discard)
	}()

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if completed.Load() {
			interrupt <- os.Interrupt
			select {
			case err := <-done:
				if err != nil {
					t.Fatalf("runner exited with %v", err)
				}
			case <-time.After(time.Second):
			}
			if conns.Load() < 2 {
				t.Fatalf("expected reconnect, conns=%d", conns.Load())
			}
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	interrupt <- os.Interrupt
	t.Fatal("runner did not reconnect after websocket drop")
}

func TestRunnerFgResendsCompleteOnJobReplay(t *testing.T) {
	testenv.SetTempHome(t)
	useFastRunnerReconnect(t)
	var secondComplete atomic.Bool
	var conns atomic.Int32
	upgrader := websocket.Upgrader{CheckOrigin: func(r *http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/runners/register" {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"id":         "11111111-1111-4111-8111-111111111111",
				"account_id": "22222222-2222-4222-8222-222222222222",
				"name":       "box",
				"status":     "online",
				"token":      "runner-token",
				"created_at": "2026-08-17T00:00:00Z",
				"updated_at": "2026-08-17T00:00:00Z",
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
		n := conns.Add(1)
		defer conn.Close() //nolint:errcheck
		_ = conn.WriteJSON(map[string]any{
			"type": "hello",
			"job":  map[string]any{"execution_id": "exec-replay"},
		})
		for {
			var msg map[string]any
			if err := conn.ReadJSON(&msg); err != nil {
				return
			}
			if msg["type"] == "complete" && msg["execution_id"] == "exec-replay" {
				if n == 1 {
					return
				}
				secondComplete.Store(true)
				return
			}
		}
	}))
	defer server.Close()

	oldToken, oldURL := FlagToken, FlagURL
	FlagURL = server.URL
	FlagToken = "tok"
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })
	client := api.NewClientWithToken(server.URL, "tok")
	state, err := loadOrRegisterRunner(client, "box", "host", nil)
	if err != nil {
		t.Fatal(err)
	}

	interrupt := make(chan os.Signal, 1)
	done := make(chan error, 1)
	go func() {
		done <- runnerForegroundLoop(state, interrupt, io.Discard)
	}()

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if secondComplete.Load() {
			interrupt <- os.Interrupt
			select {
			case <-done:
			case <-time.After(time.Second):
			}
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	interrupt <- os.Interrupt
	t.Fatal("runner did not resend complete after reconnect job replay")
}

func TestRunnerFgFatalServerErrorDoesNotReconnect(t *testing.T) {
	testenv.SetTempHome(t)
	useFastRunnerReconnect(t)
	var conns atomic.Int32
	upgrader := websocket.Upgrader{CheckOrigin: func(r *http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/runners/register" {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"id":         "11111111-1111-4111-8111-111111111111",
				"account_id": "22222222-2222-4222-8222-222222222222",
				"name":       "box",
				"status":     "online",
				"token":      "runner-token",
				"created_at": "2026-08-17T00:00:00Z",
				"updated_at": "2026-08-17T00:00:00Z",
			})
			return
		}
		if !strings.HasSuffix(r.URL.Path, "/ws") {
			http.NotFound(w, r)
			return
		}
		conns.Add(1)
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close() //nolint:errcheck
		_ = conn.WriteJSON(map[string]any{"type": "error", "error": "unauthorized"})
		time.Sleep(200 * time.Millisecond)
	}))
	defer server.Close()

	oldToken, oldURL := FlagToken, FlagURL
	FlagURL = server.URL
	FlagToken = "tok"
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })
	client := api.NewClientWithToken(server.URL, "tok")
	state, err := loadOrRegisterRunner(client, "box", "host", nil)
	if err != nil {
		t.Fatal(err)
	}

	err = runnerForegroundLoop(state, make(chan os.Signal, 1), io.Discard)
	if err == nil || !strings.Contains(err.Error(), "unauthorized") {
		t.Fatalf("err = %v", err)
	}
	time.Sleep(50 * time.Millisecond)
	if conns.Load() != 1 {
		t.Fatalf("fatal error reconnected, conns=%d", conns.Load())
	}
}

func TestStopForegroundOnInterruptKillsJobAndUnregisters(t *testing.T) {
	testenv.SetTempHome(t)
	var unregistered atomic.Bool
	upgrader := websocket.Upgrader{CheckOrigin: func(r *http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/runners/register" {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"id":         "11111111-1111-4111-8111-111111111111",
				"account_id": "22222222-2222-4222-8222-222222222222",
				"name":       "box",
				"status":     "online",
				"token":      "runner-token",
				"created_at": "2026-08-17T00:00:00Z",
				"updated_at": "2026-08-17T00:00:00Z",
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
		for {
			var msg map[string]any
			if err := conn.ReadJSON(&msg); err != nil {
				return
			}
			if msg["type"] == "unregister" {
				unregistered.Store(true)
				return
			}
		}
	}))
	defer server.Close()

	oldToken, oldURL := FlagToken, FlagURL
	FlagURL = server.URL
	FlagToken = "tok"
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })
	client := api.NewClientWithToken(server.URL, "tok")
	state, err := loadOrRegisterRunner(client, "box", "host", nil)
	if err != nil {
		t.Fatal(err)
	}

	cmd := exec.Command("sleep", "30")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
			_, _ = cmd.Process.Wait()
		}
	})
	halted := &atomic.Bool{}
	halt := true
	waited := make(chan error, 1)
	go func() { waited <- cmd.Wait() }()
	stopForegroundOnInterrupt(state, cmd, &halt, halted, io.Discard)
	if !halted.Load() {
		t.Fatal("running job must latch halted")
	}
	select {
	case <-waited:
	case <-time.After(2 * time.Second):
		t.Fatal("interrupt must kill the in-flight job")
	}
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if unregistered.Load() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("interrupt must send unregister")
}

func TestHelperSleepJob(t *testing.T) {
	if os.Getenv("PRELOOP_HELPER_SLEEP_JOB") != "1" {
		t.Skip("spawned by TestRunnerFgInterruptDuringBackoffKillsJob")
	}
	path := os.Getenv("PRELOOP_HELPER_PID_PATH")
	if path == "" {
		t.Fatal("PRELOOP_HELPER_PID_PATH is required")
	}
	if err := os.WriteFile(path, []byte(strconv.Itoa(os.Getpid())), 0o644); err != nil {
		t.Fatal(err)
	}
	time.Sleep(30 * time.Second)
}

func processAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	if runtime.GOOS == "windows" {
		// Signal(0) is not implemented on Windows (the GitHub CLI job
		// failed with "leased job did not start" for that reason).
		out, err := exec.Command(
			"tasklist",
			"/FI",
			"PID eq "+strconv.Itoa(pid),
			"/FO",
			"CSV",
			"/NH",
		).CombinedOutput()
		if err != nil {
			return false
		}
		return strings.Contains(string(out), `"`+strconv.Itoa(pid)+`"`)
	}
	proc, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	return proc.Signal(syscall.Signal(0)) == nil
}

func TestRunnerFgInterruptDuringBackoffKillsJob(t *testing.T) {
	testenv.SetTempHome(t)
	oldMin, oldMax := runnerReconnectMin, runnerReconnectMax
	runnerReconnectMin = 2 * time.Second
	runnerReconnectMax = 2 * time.Second
	t.Cleanup(func() {
		runnerReconnectMin, runnerReconnectMax = oldMin, oldMax
	})
	oldDocker, oldCmd := runnerHasDocker, newRunnerJobCmd
	pidPath := filepath.Join(t.TempDir(), "job.pid")
	runnerHasDocker = func() bool { return true }
	newRunnerJobCmd = func(image string, env map[string]string, opts runnerDockerOpts) *exec.Cmd {
		// Pid file instead of sharing *exec.Cmd: the runner calls Start/Wait
		// on that Cmd, and -race flags unsynchronized reads of Process /
		// ProcessState from the test goroutine (GitLab test:unit:cli).
		cmd := exec.Command(os.Args[0], "-test.run=^TestHelperSleepJob$", "--")
		cmd.Env = append(
			os.Environ(),
			"PRELOOP_HELPER_SLEEP_JOB=1",
			"PRELOOP_HELPER_PID_PATH="+pidPath,
		)
		return cmd
	}
	readJobPID := func() int {
		raw, err := os.ReadFile(pidPath)
		if err != nil {
			return 0
		}
		pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
		if err != nil {
			return 0
		}
		return pid
	}
	t.Cleanup(func() {
		runnerHasDocker, newRunnerJobCmd = oldDocker, oldCmd
		if pid := readJobPID(); processAlive(pid) {
			proc, err := os.FindProcess(pid)
			if err == nil {
				_ = proc.Kill()
			}
		}
	})

	var unregistered atomic.Bool
	var started atomic.Bool
	var dropped atomic.Bool
	upgrader := websocket.Upgrader{CheckOrigin: func(r *http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/runners/register" {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"id":         "11111111-1111-4111-8111-111111111111",
				"account_id": "22222222-2222-4222-8222-222222222222",
				"name":       "box",
				"status":     "online",
				"token":      "runner-token",
				"created_at": "2026-08-17T00:00:00Z",
				"updated_at": "2026-08-17T00:00:00Z",
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
		if started.Swap(true) {
			defer conn.Close() //nolint:errcheck
			for {
				var msg map[string]any
				if err := conn.ReadJSON(&msg); err != nil {
					return
				}
				if msg["type"] == "unregister" {
					unregistered.Store(true)
					return
				}
			}
		}
		_ = conn.WriteJSON(map[string]any{
			"type": "hello",
			"job": map[string]any{
				"execution_id": "exec-backoff",
				"agent_config": map[string]any{"image": "preloop/agent:dev"},
			},
		})
		time.Sleep(300 * time.Millisecond)
		_ = conn.Close()
		dropped.Store(true)
	}))
	defer server.Close()

	oldToken, oldURL := FlagToken, FlagURL
	FlagURL = server.URL
	FlagToken = "tok"
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })
	client := api.NewClientWithToken(server.URL, "tok")
	state, err := loadOrRegisterRunner(client, "box", "host", nil)
	if err != nil {
		t.Fatal(err)
	}

	interrupt := make(chan os.Signal, 1)
	done := make(chan error, 1)
	go func() {
		done <- runnerForegroundLoop(state, interrupt, io.Discard)
	}()

	var jobPID int
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		jobPID = readJobPID()
		if processAlive(jobPID) {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !processAlive(jobPID) {
		interrupt <- os.Interrupt
		t.Fatal("leased job did not start")
	}
	deadline = time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) && !dropped.Load() {
		time.Sleep(10 * time.Millisecond)
	}
	if !dropped.Load() {
		interrupt <- os.Interrupt
		t.Fatal("control-plane socket did not drop")
	}
	time.Sleep(50 * time.Millisecond)
	interrupt <- os.Interrupt
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("runner exited with %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("runner did not exit after interrupt during backoff")
	}
	deadline = time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if unregistered.Load() && !processAlive(jobPID) {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !unregistered.Load() {
		t.Fatal("interrupt during backoff must unregister")
	}
	if processAlive(jobPID) {
		t.Fatal("interrupt during backoff must kill the in-flight job")
	}
}

func TestRunnerCommandsExist(t *testing.T) {
	names := map[string]bool{}
	for _, child := range runnerCmd.Commands() {
		names[child.Name()] = true
	}
	for _, want := range []string{"fg", "enable", "disable", "start", "stop", "restart", "status"} {
		if !names[want] {
			t.Fatalf("missing runner %s", want)
		}
	}
}

func TestRunnerJobEnvStripsCredentialShapedAgentConfig(t *testing.T) {
	job := map[string]any{
		"execution_id": "exec-1",
		"agent_config": map[string]any{
			"image":          "preloop/agent:dev",
			"max_tokens":     128,
			"token_limit":    4096,
			"api_key":        "provider-secret",
			"token":          "also-secret",
			"openai_api_key": "sk-test",
			"provider": map[string]any{
				"api_key": "nested-secret",
				"model":   "gpt-4",
			},
		},
	}
	env := runnerJobEnv(job, "https://review.preloop.ai")
	got := env["AGENT_CONFIG"]
	if !strings.Contains(got, `"image":"preloop/agent:dev"`) {
		t.Fatalf("AGENT_CONFIG = %q", got)
	}
	if !strings.Contains(got, `"max_tokens":128`) {
		t.Fatalf("max_tokens should survive sanitization: %q", got)
	}
	if !strings.Contains(got, `"token_limit":4096`) {
		t.Fatalf("token_limit should survive sanitization: %q", got)
	}
	if !strings.Contains(got, `"model":"gpt-4"`) {
		t.Fatalf("nested non-secret should survive: %q", got)
	}
	for _, leaked := range []string{"provider-secret", "also-secret", "sk-test", "nested-secret"} {
		if strings.Contains(got, leaked) {
			t.Fatalf("credential leaked into AGENT_CONFIG: %s in %q", leaked, got)
		}
	}
}

func TestRunnerControlPlaneURLFailsClosedOnBadConfig(t *testing.T) {
	home := testenv.SetTempHome(t)
	cfgDir := filepath.Join(home, ".preloop")
	if err := os.MkdirAll(cfgDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cfgDir, "config.yaml"), []byte(":\n  - not: yaml: ["), 0o600); err != nil {
		t.Fatal(err)
	}
	oldToken, oldURL := FlagToken, FlagURL
	FlagToken, FlagURL = "", ""
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })
	if _, err := runnerControlPlaneURL(); err == nil {
		t.Fatal("expected resolve failure")
	}
}

func TestDockerRunArgsHonorTrustedRunnerFlags(t *testing.T) {
	args := dockerRunArgs("preloop/agent:dev", map[string]string{
		"COMPOSE_PROJECT_NAME": "preloop-abcd1234",
		"EXECUTION_ID":         "abcd1234-0000-4000-8000-000000000001",
	}, runnerDockerOpts{
		MountDockerSocket: true,
		PersistWorkspace:  true,
		WorkspaceHostDir:  "/tmp/preloop-workspaces/exec-1",
		ExtraMounts:       []string{"/var/cache/builds:/cache:ro"},
		Network:           "preloop-trusted",
	})
	joined := strings.Join(args, " ")
	if args[0] != "run" || args[1] != "--rm" {
		t.Fatalf("jobs must keep docker run --rm: %v", args)
	}
	if !containsPair(args, "-v", "/var/run/docker.sock:/var/run/docker.sock") {
		t.Fatalf("missing docker.sock mount: %v", args)
	}
	if !containsPair(args, "-v", "/tmp/preloop-workspaces/exec-1:/workspace") {
		t.Fatalf("missing workspace mount: %v", args)
	}
	if !containsPair(args, "-v", "/var/cache/builds:/cache:ro") {
		t.Fatalf("missing extra mount: %v", args)
	}
	if !containsPair(args, "--network", "preloop-trusted") {
		t.Fatalf("missing network: %v", args)
	}
	if !containsPair(args, "-e", "COMPOSE_PROJECT_NAME") {
		t.Fatalf("missing compose project env flag: %v", args)
	}
	if strings.Contains(joined, "secret") {
		t.Fatalf("unexpected secret in argv: %v", args)
	}
}

func TestDockerRunArgsOmitsTrustMountsWhenDisabled(t *testing.T) {
	args := dockerRunArgs("preloop/agent:dev", map[string]string{"EXECUTION_ID": "e1"}, runnerDockerOpts{})
	joined := strings.Join(args, " ")
	if !strings.HasPrefix(joined, "run --rm ") {
		t.Fatalf("default job must use docker run --rm: %v", args)
	}
	if strings.Contains(joined, "/var/run/docker.sock") || strings.Contains(joined, "/workspace") {
		t.Fatalf("trust mounts must default off: %v", args)
	}
}

func TestValidateExtraMountRequiresAbsoluteHost(t *testing.T) {
	if _, err := validateExtraMount("/var/cache:/cache:ro"); err != nil {
		t.Fatal(err)
	}
	if _, err := validateExtraMount("cache:/cache"); err == nil {
		t.Fatal("relative host path must be rejected")
	}
	if _, err := validateExtraMount("/var/cache"); err == nil {
		t.Fatal("container path is required")
	}
	if _, err := validateExtraMount("/var/cache:/cache:shared"); err == nil {
		t.Fatal("unknown mode must be rejected")
	}
}

func TestValidateWorkspaceIDRejectsTraversal(t *testing.T) {
	if _, err := validateWorkspaceID("../../.."); err == nil {
		t.Fatal("parent traversal must be rejected")
	}
	if _, err := validateWorkspaceID("exec-prior"); err == nil {
		t.Fatal("non-UUID workspace id must be rejected")
	}
	if _, err := validateWorkspaceID("11111111-1111-4111-8111-111111111111"); err != nil {
		t.Fatal(err)
	}
}

func TestRunnerDockerOptsFromJobDefaultsOff(t *testing.T) {
	opts, err := runnerDockerOptsFromJob(map[string]any{
		"execution_id": "11111111-1111-4111-8111-111111111111",
		"agent_config": map[string]any{"image": "preloop/agent:dev"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if opts.MountDockerSocket || opts.PersistWorkspace || opts.Network != "" || len(opts.ExtraMounts) != 0 {
		t.Fatalf("opts = %#v", opts)
	}
	if opts.ComposeProject != "preloop-11111111111141118111111111111111" {
		t.Fatalf("compose project = %q", opts.ComposeProject)
	}
}

func TestPreparePersistWorkspaceReusesResumeFrom(t *testing.T) {
	testenv.SetTempHome(t)
	prior := "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
	current := "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
	src, err := runnerWorkspaceDir(prior)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(src, 0o700); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(src, "notes.txt")
	if err := os.WriteFile(marker, []byte("kept"), 0o600); err != nil {
		t.Fatal(err)
	}
	dest, err := preparePersistWorkspace(current, prior)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(src); !os.IsNotExist(err) {
		t.Fatalf("resume_from dir should be moved, stat err = %v", err)
	}
	got, err := os.ReadFile(filepath.Join(dest, "notes.txt"))
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "kept" {
		t.Fatalf("workspace contents = %q", got)
	}
	info, err := os.Stat(dest)
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" && info.Mode().Perm() != 0o700 {
		t.Fatalf("workspace mode = %o", info.Mode().Perm())
	}
}

func TestCleanupStaleWorkspacesHonorsTTLAndKeep(t *testing.T) {
	root := t.TempDir()
	stale := filepath.Join(root, "old-exec")
	fresh := filepath.Join(root, "fresh-exec")
	current := filepath.Join(root, "current-exec")
	for _, dir := range []string{stale, fresh, current} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	old := time.Now().Add(-48 * time.Hour)
	if err := os.Chtimes(stale, old, old); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(current, old, old); err != nil {
		t.Fatal(err)
	}
	if err := cleanupStaleWorkspacesAt(root, 24*time.Hour, time.Now(), map[string]bool{"current-exec": true}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(stale); !os.IsNotExist(err) {
		t.Fatal("stale workspace older than TTL should be removed")
	}
	if _, err := os.Stat(fresh); err != nil {
		t.Fatalf("fresh workspace should remain: %v", err)
	}
	if _, err := os.Stat(current); err != nil {
		t.Fatalf("current job workspace should remain: %v", err)
	}
}

func TestWorkspaceTTLReadsEnv(t *testing.T) {
	t.Setenv(workspaceTTLHoursEnv, "6")
	if workspaceTTL() != 6*time.Hour {
		t.Fatalf("ttl = %s", workspaceTTL())
	}
	t.Setenv(workspaceTTLHoursEnv, "nope")
	if workspaceTTL() != 24*time.Hour {
		t.Fatalf("invalid ttl should default, got %s", workspaceTTL())
	}
}

func containsPair(args []string, flag, value string) bool {
	for i := 0; i < len(args)-1; i++ {
		if args[i] == flag && args[i+1] == value {
			return true
		}
	}
	return false
}

func TestWorkspaceZeroRetentionPurgesIdleButKeepsActive(t *testing.T) {
	root := t.TempDir()
	for _, name := range []string{"idle", "active"} {
		if err := os.Mkdir(filepath.Join(root, name), 0700); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv(workspaceTTLHoursEnv, "0")
	if workspaceTTL() != 0 {
		t.Fatal("zero must disable retention")
	}
	if err := cleanupStaleWorkspacesAt(root, workspaceTTL(), time.Now(), map[string]bool{"active": true}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, "idle")); !os.IsNotExist(err) {
		t.Fatal("idle state retained with zero TTL")
	}
	if _, err := os.Stat(filepath.Join(root, "active")); err != nil {
		t.Fatal("active workspace removed")
	}
}

func TestRunnerForwardsRepositorySetupContract(t *testing.T) {
	env := runnerJobEnv(map[string]any{"git_clone_config": map[string]any{"setup_commands": []string{"echo setup"}}}, "https://example.com")
	var config map[string]any
	if err := json.Unmarshal([]byte(env["GIT_CLONE_CONFIG"]), &config); err != nil {
		t.Fatal(err)
	}
	if config["setup_commands"] == nil {
		t.Fatal("repository setup missing from image contract")
	}
}

func TestWorkspaceCleanupProtectsAnotherRunnerLease(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "other-runner")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(dir, ".preloop-runner-lease")
	if err := os.WriteFile(marker, []byte("active"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := cleanupStaleWorkspacesAt(root, 0, time.Now(), nil); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatal("active peer deleted", err)
	}
	if err := cleanupStaleWorkspacesAt(root, 0, time.Now().Add(3*time.Minute), nil); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatal("expired lease retained")
	}
}

func TestWorkspaceQuotaProtectsActiveAndRecordsLoss(t *testing.T) {
	root := t.TempDir()
	for _, name := range []string{"old", "active"} {
		dir := filepath.Join(root, name)
		if err := os.Mkdir(dir, 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "source"), make([]byte, 128), 0600); err != nil {
			t.Fatal(err)
		}
	}
	if err := enforceWorkspaceQuota(root, 128, time.Now(), map[string]bool{"active": true}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, "old")); !os.IsNotExist(err) {
		t.Fatal("quota did not remove old state")
	}
	if _, err := os.Stat(filepath.Join(root, "old.expired")); err != nil {
		t.Fatal("loss metadata missing")
	}
	if _, err := os.Stat(filepath.Join(root, "active", "source")); err != nil {
		t.Fatal("active state removed")
	}
}
