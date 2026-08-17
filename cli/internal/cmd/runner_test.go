package cmd

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
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
	got, err := runnerWebsocketURL("rid", "secret")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(got, "ws://localhost:8000/api/v1/runners/rid/ws") {
		t.Fatalf("url = %s", got)
	}
	if !strings.Contains(got, "token=secret") {
		t.Fatalf("missing token: %s", got)
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
