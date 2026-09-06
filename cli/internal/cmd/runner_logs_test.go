package cmd

import (
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestRunnerLogBufferSeparatesEnvelopeAndBoundsUnterminatedOutput(t *testing.T) {
	var buffer runnerLogBuffer
	line := resultLine(`{"status":"success"}`, 0)
	_, _ = buffer.Write([]byte("early\n" + line[:20]))
	if got := buffer.batch(); len(got) != 1 || got[0] != "early" {
		t.Fatalf("batch=%v", got)
	}
	buffer.acknowledge(1)
	_, _ = buffer.Write([]byte(line[20:] + "\nlast partial"))
	buffer.finish()
	if got := buffer.batch(); len(got) != 1 || got[0] != "last partial" {
		t.Fatalf("batch=%v", got)
	}
	if buffer.String() != line {
		t.Fatal("missing result envelope")
	}
	var tooLarge runnerLogBuffer
	_, _ = tooLarge.Write([]byte(strings.Repeat("x", runnerLogPartialLimit+1)))
	if len(tooLarge.partial) != 0 || !tooLarge.overflow {
		t.Fatal("unterminated output was not bounded")
	}
}

func TestRunnerLogQueueOverflowCannotReportSuccess(t *testing.T) {
	var buffer runnerLogBuffer
	for i := 0; i < 100; i++ {
		_, _ = buffer.Write([]byte(strings.Repeat("x", runnerLogLineLimit) + "\n"))
	}
	_, _ = buffer.Write([]byte(resultLine(`{"status":"success"}`, 0) + "\n"))
	if buffer.pendingBytes > runnerLogQueueLimit {
		t.Fatal("queue exceeded limit")
	}
	if _, _, err := runnerStructuredResult(splitNonEmptyLines(buffer.String())); err == nil {
		t.Fatal("lost execution markers accepted as success")
	}
}

func TestRunnerStreamsLogsBeforeAgentCanFinish(t *testing.T) {
	testenv.SetTempHome(t)
	release := filepath.Join(t.TempDir(), "release")
	originalURL := FlagURL
	originalDocker, originalCmd := runnerHasDocker, newRunnerJobCmd
	t.Cleanup(func() { FlagURL = originalURL; runnerHasDocker = originalDocker; newRunnerJobCmd = originalCmd })
	runnerHasDocker = func() bool { return true }
	newRunnerJobCmd = func(string, map[string]string, runnerDockerOpts) *exec.Cmd {
		cmd := exec.Command("sh", "-c", `echo early-agent-log; while [ ! -f "$RELEASE" ]; do sleep 0.05; done; printf '%s\n' "$REPORT"; echo final-agent-log`)
		cmd.Env = append(os.Environ(), "RELEASE="+release, "REPORT="+resultLine(`{"status":"success"}`, 0))
		return cmd
	}
	messages := make(chan map[string]any, 20)
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close() //nolint:errcheck
		_ = conn.WriteJSON(map[string]any{"type": "hello", "job": map[string]any{
			"execution_id": "11111111-1111-4111-8111-111111111111", "agent_type": "codex",
			"agent_config": map[string]any{"image": "example/fake:1"},
			"launch":       map[string]any{"version": 1, "script": "fake script", "env": map[string]any{}},
		}})
		for {
			var message map[string]any
			if conn.ReadJSON(&message) != nil {
				return
			}
			messages <- message
			if message["type"] == "unregister" {
				return
			}
		}
	}))
	defer server.Close()
	FlagURL = server.URL
	conn, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close() //nolint:errcheck
	interrupt := make(chan os.Signal, 1)
	var running *exec.Cmd
	var executionID string
	var done <-chan leasedJobOutcome
	var last *leasedJobOutcome
	halt := false
	stopped := make(chan error, 1)
	go func() {
		stopped <- runRunnerSession(conn, interrupt, io.Discard, &running, &executionID, &done, &halt, &atomic.Bool{}, &last)
	}()
	t.Cleanup(func() {
		_ = os.WriteFile(release, nil, 0600)
		select {
		case interrupt <- os.Interrupt:
		default:
		}
	})
	seen := map[string]int{}
	deadline := time.After(5 * time.Second)
	for {
		select {
		case message := <-messages:
			if message["type"] == "logs" {
				for _, raw := range message["lines"].([]any) {
					line := raw.(string)
					if strings.HasPrefix(line, runnerResultPrefix) {
						t.Fatal("result envelope streamed as a log")
					}
					seen[line]++
					if line == "early-agent-log" {
						if err = os.WriteFile(release, nil, 0600); err != nil {
							t.Fatal(err)
						}
					}
				}
			}
			if message["type"] == "complete" {
				if message["status"] != "SUCCEEDED" {
					t.Fatalf("completion=%v", message)
				}
				if seen["early-agent-log"] != 1 || seen["final-agent-log"] != 1 {
					t.Fatalf("lost or duplicated logs: %v", seen)
				}
				interrupt <- os.Interrupt
				select {
				case <-stopped:
				case <-time.After(time.Second):
					t.Fatal("session did not stop")
				}
				return
			}
		case <-deadline:
			t.Fatal("agent could not finish because its early log was never streamed")
		}
	}
}
