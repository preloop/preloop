package cmd

import (
	"fmt"
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
	early, err := buffer.nextBatch()
	if err != nil || early == nil || len(early.lines) != 1 || early.lines[0] != "early" {
		t.Fatalf("batch=%v err=%v", early, err)
	}
	buffer.acknowledgeBatch(early.id)
	_, _ = buffer.Write([]byte(line[20:] + "\nlast partial"))
	buffer.finish()
	last, err := buffer.nextBatch()
	if err != nil || last == nil || len(last.lines) != 1 || last.lines[0] != "last partial" {
		t.Fatalf("batch=%v err=%v", last, err)
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
	jobs := newRunnerJobs(2)
	stopped := make(chan error, 1)
	go func() {
		stopped <- runRunnerSession(conn, interrupt, io.Discard, jobs)
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

func TestRunnerLogsRetainBatchUntilServerAcknowledges(t *testing.T) {
	var buffer runnerLogBuffer
	buffer.setLogAcknowledgements(true)
	_, _ = buffer.Write([]byte("native session\nPR created\n"))
	batch, err := buffer.nextBatch()
	if err != nil {
		t.Fatal(err)
	}
	if batch == nil || len(batch.lines) != 2 {
		t.Fatal("missing initial batch")
	}
	buffer.markBatchSent(batch.id)
	if next, err := buffer.nextBatch(); err != nil || next != nil {
		t.Fatal("sent batch should await acknowledgment")
	}
	buffer.resetDelivery()
	replay, err := buffer.nextBatch()
	if err != nil {
		t.Fatal(err)
	}
	if replay == nil || replay.id != batch.id {
		t.Fatal("reconnect lost stable batch identity")
	}
	buffer.acknowledgeBatch(batch.id)
	if next, err := buffer.nextBatch(); err != nil || next != nil || buffer.pendingBytes != 0 {
		t.Fatal("acknowledged batch was retained")
	}
}

func TestRejectedLogBatchReclaimsInflight(t *testing.T) {
	var buffer runnerLogBuffer
	buffer.setLogAcknowledgements(true)
	_, _ = buffer.Write([]byte("native session\n"))
	batch, err := buffer.nextBatch()
	if err != nil || batch == nil {
		t.Fatal(err)
	}
	buffer.markBatchSent(batch.id)
	// Same drop the receive loop uses when the server rejects the batch.
	buffer.acknowledgeBatch(batch.id)
	if buffer.pendingBytes != 0 || len(buffer.inflight) != 0 {
		t.Fatal("rejected batch still consumed queue budget")
	}
}

func TestRunnerWriteDeadlineInterruptsBackpressure(t *testing.T) {
	oldWait := runnerWriteWait
	runnerWriteWait = 50 * time.Millisecond
	t.Cleanup(func() { runnerWriteWait = oldWait })
	release := make(chan struct{})
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close() //nolint:errcheck
		<-release
	}))
	defer server.Close()
	defer close(release)
	conn, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close() //nolint:errcheck
	start := time.Now()
	err = writeRunnerJSON(conn, map[string]any{"lines": strings.Repeat("x", 16*1024*1024)})
	if err == nil || time.Since(start) > time.Second {
		t.Fatalf("blocked writer did not time out promptly: %v", err)
	}
}

func TestRunnerFinalLogFloodReplaysAfterLostAcknowledgements(t *testing.T) {
	var buffer runnerLogBuffer
	buffer.setLogAcknowledgements(true)
	for i := 0; i < 4096; i++ {
		_, _ = buffer.Write([]byte(fmt.Sprintf("line-%04d\n", i)))
	}
	completed := make(chan map[string]int, 1)
	var connections atomic.Int32
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close() //nolint:errcheck
		n := connections.Add(1)
		_ = conn.WriteJSON(map[string]any{"type": "hello", "log_acknowledgements": true})
		seen := map[string]int{}
		batches := 0
		for {
			var message map[string]any
			if conn.ReadJSON(&message) != nil {
				return
			}
			if message["type"] == "logs" {
				batches++
				if n == 1 { // Lose the connection before acknowledging its first frame.
					return
				}
				for _, line := range message["lines"].([]any) {
					seen[line.(string)]++
				}
				if err := conn.WriteJSON(map[string]any{"type": "logs_ack", "execution_id": "execution-flood", "batch_id": message["batch_id"]}); err != nil {
					return
				}
			}
			if message["type"] == "complete" {
				if n == 2 && batches > 8 {
					completed <- seen
				}
				return
			}
		}
	}))
	defer server.Close()
	jobs := newRunnerJobs(2)
	jobs.outcomes <- leasedJobOutcome{executionID: "execution-flood", status: "SUCCEEDED", result: map[string]any{"status": "success"}, logBuffer: &buffer}
	interrupt := make(chan os.Signal, 1)
	for i := 0; i < 2; i++ {
		conn, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), nil)
		if err != nil {
			t.Fatal(err)
		}
		err = runRunnerSession(conn, interrupt, io.Discard, jobs)
		_ = conn.Close()
		if err == nil {
			t.Fatal("expected disconnect")
		}
	}
	select {
	case seen := <-completed:
		if len(seen) != 4096 {
			t.Fatalf("reconnect recovered %d of 4096 log lines", len(seen))
		}
		for line, count := range seen {
			if count != 1 {
				t.Fatalf("duplicate %s: %d", line, count)
			}
		}
	case <-time.After(time.Second):
		t.Fatal("terminal result did not survive final-log backlog and reconnect")
	}
}
