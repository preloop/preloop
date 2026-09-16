package cmd

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/testenv"
)

// concurrencyJob is a minimal leased docker job payload.
func concurrencyJob(executionID string) map[string]any {
	return map[string]any{
		"execution_id": executionID,
		"agent_type":   "codex",
		"agent_config": map[string]any{"image": "example/fake:1"},
		"launch": map[string]any{
			"version": 1, "script": "fake script", "env": map[string]any{},
		},
	}
}

// blockingJobCommands makes every leased job write a marker file and then wait
// for a release file. A job that has written its marker and not been released
// is provably still running, which is how these tests observe overlap rather
// than guessing from timing.
func blockingJobCommands(t *testing.T) (markers string, release string) {
	t.Helper()
	dir := t.TempDir()
	release = filepath.Join(dir, "release")
	oldDocker, oldCmd := runnerHasDocker, newRunnerJobCmd
	t.Cleanup(func() { runnerHasDocker, newRunnerJobCmd = oldDocker, oldCmd })
	runnerHasDocker = func() bool { return true }
	newRunnerJobCmd = func(_ string, env map[string]string, _ runnerDockerOpts) *exec.Cmd {
		cmd := exec.Command(
			"sh", "-c",
			`touch "$MARKER"; while [ ! -f "$RELEASE" ]; do sleep 0.02; done; printf '%s\n' "$REPORT"`,
		)
		cmd.Env = append(
			os.Environ(),
			"MARKER="+filepath.Join(dir, env["EXECUTION_ID"]),
			"RELEASE="+release,
			"REPORT="+resultLine(`{"status":"success"}`, 0),
		)
		return cmd
	}
	return dir, release
}

func waitForFile(t *testing.T, path string, within time.Duration) bool {
	t.Helper()
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(path); err == nil {
			return true
		}
		time.Sleep(10 * time.Millisecond)
	}
	return false
}

// jobServer serves one runner websocket that sends hello with the given
// frames and reports every message the runner sends back.
func jobServer(t *testing.T, hello map[string]any) (*httptest.Server, chan map[string]any, chan map[string]any) {
	t.Helper()
	messages := make(chan map[string]any, 64)
	send := make(chan map[string]any, 8)
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close() //nolint:errcheck
		_ = conn.WriteJSON(hello)
		go func() {
			for message := range send {
				if conn.WriteJSON(message) != nil {
					return
				}
			}
		}()
		for {
			var message map[string]any
			if conn.ReadJSON(&message) != nil {
				return
			}
			select {
			case messages <- message:
			default:
			}
			if message["type"] == "unregister" {
				return
			}
		}
	}))
	t.Cleanup(server.Close)
	return server, messages, send
}

func dialJobServer(t *testing.T, server *httptest.Server) *websocket.Conn {
	t.Helper()
	conn, _, err := websocket.DefaultDialer.Dial(
		"ws"+strings.TrimPrefix(server.URL, "http"), nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}

func TestRunnerRunsTwoLeasedJobsAtOnce(t *testing.T) {
	testenv.SetTempHome(t)
	markers, release := blockingJobCommands(t)
	first, second := "exec-one", "exec-two"
	server, messages, _ := jobServer(t, map[string]any{
		"type": "hello",
		"job":  concurrencyJob(first),
		"jobs": []map[string]any{concurrencyJob(first), concurrencyJob(second)},
	})
	oldURL := FlagURL
	FlagURL = server.URL
	t.Cleanup(func() { FlagURL = oldURL })

	interrupt := make(chan os.Signal, 1)
	jobs := newRunnerJobs(2)
	stopped := make(chan error, 1)
	go func() {
		stopped <- runRunnerSession(dialJobServer(t, server), interrupt, io.Discard, jobs)
	}()
	t.Cleanup(func() {
		_ = os.WriteFile(release, nil, 0600)
		select {
		case interrupt <- os.Interrupt:
		default:
		}
	})

	if !waitForFile(t, filepath.Join(markers, first), 5*time.Second) {
		t.Fatal("first job never started")
	}
	if !waitForFile(t, filepath.Join(markers, second), 5*time.Second) {
		t.Fatal("second job did not start while the first one was still running")
	}
	if err := os.WriteFile(release, nil, 0600); err != nil {
		t.Fatal(err)
	}

	completed := map[string]string{}
	deadline := time.After(10 * time.Second)
	for len(completed) < 2 {
		select {
		case message := <-messages:
			if message["type"] == "complete" {
				id, _ := message["execution_id"].(string)
				status, _ := message["status"].(string)
				completed[id] = status
			}
		case <-deadline:
			t.Fatalf("only completed %v", completed)
		}
	}
	if completed[first] != "SUCCEEDED" || completed[second] != "SUCCEEDED" {
		t.Fatalf("completions = %v", completed)
	}
}

func TestRunnerIgnoresJobsBeyondItsSlots(t *testing.T) {
	testenv.SetTempHome(t)
	markers, release := blockingJobCommands(t)
	first, second := "exec-slot", "exec-overflow"
	server, messages, _ := jobServer(t, map[string]any{
		"type": "hello",
		"jobs": []map[string]any{concurrencyJob(first), concurrencyJob(second)},
	})
	oldURL := FlagURL
	FlagURL = server.URL
	t.Cleanup(func() { FlagURL = oldURL })

	interrupt := make(chan os.Signal, 1)
	jobs := newRunnerJobs(1)
	stopped := make(chan error, 1)
	go func() {
		stopped <- runRunnerSession(dialJobServer(t, server), interrupt, io.Discard, jobs)
	}()
	t.Cleanup(func() {
		_ = os.WriteFile(release, nil, 0600)
		select {
		case interrupt <- os.Interrupt:
		default:
		}
	})

	if !waitForFile(t, filepath.Join(markers, first), 5*time.Second) {
		t.Fatal("first job never started")
	}
	if waitForFile(t, filepath.Join(markers, second), time.Second) {
		t.Fatal("a one slot runner started a second job")
	}
	if err := os.WriteFile(release, nil, 0600); err != nil {
		t.Fatal(err)
	}
	deadline := time.After(10 * time.Second)
	for {
		select {
		case message := <-messages:
			if message["type"] != "complete" {
				continue
			}
			if message["execution_id"] != first {
				t.Fatalf("unexpected completion %v", message)
			}
			return
		case <-deadline:
			t.Fatal("the accepted job never completed")
		}
	}
}

func TestHaltStopsOnlyTheNamedExecution(t *testing.T) {
	testenv.SetTempHome(t)
	markers, release := blockingJobCommands(t)
	halted, kept := "exec-halted", "exec-kept"
	server, messages, send := jobServer(t, map[string]any{
		"type": "hello",
		"jobs": []map[string]any{concurrencyJob(halted), concurrencyJob(kept)},
	})
	oldURL := FlagURL
	FlagURL = server.URL
	t.Cleanup(func() { FlagURL = oldURL })

	interrupt := make(chan os.Signal, 1)
	jobs := newRunnerJobs(2)
	stopped := make(chan error, 1)
	go func() {
		stopped <- runRunnerSession(dialJobServer(t, server), interrupt, io.Discard, jobs)
	}()
	t.Cleanup(func() {
		_ = os.WriteFile(release, nil, 0600)
		select {
		case interrupt <- os.Interrupt:
		default:
		}
	})

	if !waitForFile(t, filepath.Join(markers, halted), 5*time.Second) ||
		!waitForFile(t, filepath.Join(markers, kept), 5*time.Second) {
		t.Fatal("both jobs must be running before the halt")
	}
	send <- map[string]any{
		"type": "halt", "halt": true,
		"halt_execution_id":  halted,
		"halt_execution_ids": []string{halted},
	}

	completions := map[string]string{}
	deadline := time.After(10 * time.Second)
	released := false
	for len(completions) < 2 {
		select {
		case message := <-messages:
			if message["type"] != "complete" {
				continue
			}
			id, _ := message["execution_id"].(string)
			status, _ := message["status"].(string)
			completions[id] = status
			if id == halted && !released {
				// Only once the halted job is finished: the other job must
				// still be running at that point, not killed alongside it.
				if _, err := os.Stat(filepath.Join(markers, kept)); err != nil {
					t.Fatal(err)
				}
				released = true
				if err := os.WriteFile(release, nil, 0600); err != nil {
					t.Fatal(err)
				}
			}
		case <-deadline:
			t.Fatalf("completions = %v", completions)
		}
	}
	if completions[halted] != "STOPPED" {
		t.Fatalf("halted execution reported %q", completions[halted])
	}
	if completions[kept] != "SUCCEEDED" {
		t.Fatalf("halting one execution disturbed the other: %v", completions)
	}
}

func TestRunnerReportsItsConcurrency(t *testing.T) {
	testenv.SetTempHome(t)
	registered := make(chan int, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		concurrency, _ := body["concurrency"].(float64)
		registered <- int(concurrency)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":         "11111111-1111-4111-8111-111111111111",
			"account_id": "22222222-2222-4222-8222-222222222222",
			"name":       "box",
			"status":     "online",
			"token":      "runner-token",
			"created_at": "2026-08-17T00:00:00Z",
			"updated_at": "2026-08-17T00:00:00Z",
		})
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	if _, err := loadOrRegisterRunner(client, "box", "host", nil, 4); err != nil {
		t.Fatal(err)
	}
	if got := <-registered; got != 4 {
		t.Fatalf("registered concurrency = %d", got)
	}
	if got := runnerHeartbeatMessage(4)["concurrency"]; got != 4 {
		t.Fatalf("heartbeat concurrency = %v", got)
	}
	if _, reported := runnerHeartbeatMessage(0)["concurrency"]; reported {
		t.Fatal("an unset concurrency must not be reported as a ceiling")
	}
}

func TestResolveRunnerConcurrency(t *testing.T) {
	testenv.SetTempHome(t)
	t.Setenv(config.EnvRunnerConcurrency, "")
	if got := resolveRunnerConcurrency(3); got != 3 {
		t.Fatalf("flag value = %d", got)
	}
	if got := resolveRunnerConcurrency(0); got != config.DefaultRunnerConcurrency {
		t.Fatalf("default = %d", got)
	}
	if got := resolveRunnerConcurrency(-4); got != config.DefaultRunnerConcurrency {
		t.Fatalf("negative flag = %d", got)
	}
	if got := resolveRunnerConcurrency(9000); got != config.MaxRunnerConcurrency {
		t.Fatalf("clamped = %d", got)
	}
	t.Setenv(config.EnvRunnerConcurrency, "5")
	if got := resolveRunnerConcurrency(0); got != 5 {
		t.Fatalf("environment value = %d", got)
	}
	if got := resolveRunnerConcurrency(1); got != 1 {
		t.Fatal("the flag must win over the environment")
	}
}

func TestRunnerJobsSlotAccounting(t *testing.T) {
	jobs := newRunnerJobs(2)
	if jobs.freeSlots() != 2 {
		t.Fatalf("free slots = %d", jobs.freeSlots())
	}
	jobs.start(&runnerJob{executionID: "a"})
	jobs.start(&runnerJob{executionID: "b"})
	if jobs.freeSlots() != 0 {
		t.Fatalf("free slots = %d", jobs.freeSlots())
	}
	if keep := jobs.keepSet(); !keep["a"] || !keep["b"] {
		t.Fatalf("workspace retention must keep every running job: %v", keep)
	}
	jobs.finish("a")
	if jobs.freeSlots() != 1 || jobs.job("a") != nil {
		t.Fatal("finishing one job must free exactly one slot")
	}
	// A halt for an execution this process has not started yet is recorded,
	// so the lease cannot start work the operator already stopped.
	jobs.haltOne("c")
	if !jobs.pendingHalt["c"] {
		t.Fatal("halt of an unknown execution must be remembered")
	}
	jobs.remember(leasedJobOutcome{executionID: "b", status: "SUCCEEDED"})
	jobs.remember(leasedJobOutcome{executionID: "b", status: "FAILED"})
	if outcome := jobs.completedOutcome("b"); outcome == nil || outcome.status != "FAILED" {
		t.Fatalf("outcome = %#v", jobs.completedOutcome("b"))
	}
	for index := 0; index < runnerRetainedOutcomes+4; index++ {
		jobs.remember(leasedJobOutcome{executionID: string(rune('m' + index))})
	}
	if len(jobs.completed) > runnerRetainedOutcomes {
		t.Fatalf("retained %d outcomes", len(jobs.completed))
	}
}
