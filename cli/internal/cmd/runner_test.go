package cmd

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strings"
	"sync/atomic"
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
		"EXECUTION_ID":      "exec-1",
		"FLOW_ID":           "flow-1",
		"AGENT_PROMPT":      "review the PR",
		"AI_MODEL":          "claude-sonnet-4-5",
		"AI_MODEL_PROVIDER": "anthropic",
		"PRELOOP_API_TOKEN": "secret-token",
		"PRELOOP_URL":       "https://review.preloop.ai",
		"AGENT_CONFIG":      `{"image":"preloop/agent:dev"}`,
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
	if len(env) != 1 || env["EXECUTION_ID"] != "exec-1" {
		t.Fatalf("env = %v", env)
	}
}

func TestDockerRunArgsUsesBareEnvFlags(t *testing.T) {
	args := dockerRunArgs("preloop/agent:dev", map[string]string{
		"PRELOOP_API_TOKEN": "secret-token",
		"EXECUTION_ID":      "exec-1",
	})
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
