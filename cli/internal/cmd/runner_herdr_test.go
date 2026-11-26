package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/preloop/preloop/cli/internal/testenv"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/herdr"
)

func testHerdrCommand() herdrPromptCommand {
	return herdrPromptCommand{CommandID: "example-command", RuntimeSessionID: "example-runtime", PaneID: "example-pane", HarnessKind: "codex_cli", HarnessSessionID: "example-native", Text: "Private test prompt never persist this", ExpiresAt: time.Now().Add(time.Minute).UTC().Format(time.RFC3339Nano)}
}
func testHerdrManager(t *testing.T, dir string, calls *atomic.Int32) *runnerHerdrPrompts {
	t.Helper()
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	m := newRunnerHerdrPrompts()
	m.directory = func() (string, error) { return dir, nil }
	m.prompt = func(ctx context.Context, dir string, target herdr.PromptTarget, text string) herdr.PromptResult {
		calls.Add(1)
		if _, ok := ctx.Deadline(); !ok {
			t.Error("missing command expiry deadline")
		}
		return herdr.PromptResult{Code: "delivered", Delivery: "sent", Reason: "Submitted"}
	}
	return m
}
func TestRunnerHerdrDurableReplay(t *testing.T) {
	dir := t.TempDir()
	var calls atomic.Int32
	c := testHerdrCommand()
	m := testHerdrManager(t, dir, &calls)
	for i := 0; i < 3; i++ {
		if i == 2 {
			m = testHerdrManager(t, dir, &calls)
		}
		r := m.execute(c)
		if r.Delivery != "sent" {
			t.Fatal(r)
		}
	}
	if calls.Load() != 1 {
		t.Fatal(calls.Load())
	}
	entries, _ := os.ReadDir(filepath.Join(dir, "herdr-commands"))
	for _, entry := range entries {
		b, _ := os.ReadFile(filepath.Join(dir, "herdr-commands", entry.Name()))
		if strings.Contains(string(b), c.Text) || strings.Contains(string(b), c.HarnessSessionID) {
			t.Fatal("journal leaked prompt or native session")
		}
	}
	c.Text = "changed command"
	r := m.execute(c)
	if r.Code != "command_conflict" || calls.Load() != 1 {
		t.Fatal(r, calls.Load())
	}
}
func TestRunnerHerdrAcceptedCrashNeverRetries(t *testing.T) {
	dir := t.TempDir()
	var calls atomic.Int32
	m := testHerdrManager(t, dir, &calls)
	c := testHerdrCommand()
	if r := m.execute(c); r.Delivery != "sent" {
		t.Fatal(r)
	}
	entries, _ := os.ReadDir(filepath.Join(dir, "herdr-commands"))
	for _, entry := range entries {
		if !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		path := filepath.Join(dir, "herdr-commands", entry.Name())
		b, _ := os.ReadFile(path)
		var record herdrJournalRecord
		json.Unmarshal(b, &record)
		record.Result = nil
		if e := writeHerdrJournal(path, record, false); e != nil {
			t.Fatal(e)
		}
	}
	m = testHerdrManager(t, dir, &calls)
	r := m.execute(c)
	if r.Code != "delivery_unknown" || r.Delivery != "unknown" || calls.Load() != 1 {
		t.Fatal(r, calls.Load())
	}
	// A torn result write also fails closed and never reinjects.
	for _, entry := range entries {
		if strings.HasSuffix(entry.Name(), ".json") {
			os.WriteFile(filepath.Join(dir, "herdr-commands", entry.Name()), []byte("{"), 0600)
		}
	}
	if r = m.execute(c); r.Delivery != "unknown" || calls.Load() != 1 {
		t.Fatal(r, calls.Load())
	}
}
func TestRunnerHerdrExpiryAndJournalFailure(t *testing.T) {
	dir := t.TempDir()
	var calls atomic.Int32
	m := testHerdrManager(t, dir, &calls)
	c := testHerdrCommand()
	c.ExpiresAt = time.Now().Add(-time.Second).UTC().Format(time.RFC3339)
	if r := m.execute(c); r.Code != "command_expired" {
		t.Fatal(r)
	}
	c.ExpiresAt = "invalid"
	if r := m.execute(c); r.Code != "invalid_command" {
		t.Fatal(r)
	}
	c = testHerdrCommand()
	file := filepath.Join(dir, "file")
	os.WriteFile(file, nil, 0600)
	m.directory = func() (string, error) { return file, nil }
	if r := m.execute(c); r.Code != "journal_unavailable" {
		t.Fatal(r)
	}
	if calls.Load() != 0 {
		t.Fatal("failed guard invoked transport")
	}
}
func TestRunnerHerdrAsyncAndConcurrentReplay(t *testing.T) {
	dir := t.TempDir()
	var calls atomic.Int32
	m := testHerdrManager(t, dir, &calls)
	entered := make(chan struct{})
	release := make(chan struct{})
	m.prompt = func(context.Context, string, herdr.PromptTarget, string) herdr.PromptResult {
		calls.Add(1)
		close(entered)
		<-release
		return herdr.PromptResult{Code: "delivered", Delivery: "sent"}
	}
	c := testHerdrCommand()
	m.submit(c)
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("worker did not start")
	}
	m.submit(c)
	// Another process/manager sees the durable acceptance before injection ends.
	other := testHerdrManager(t, dir, &calls)
	if r := other.execute(c); r.Delivery != "unknown" {
		t.Fatal(r)
	}
	close(release)
	select {
	case frame := <-m.results:
		if frame["type"] != "herdr_prompt_result" || frame["command_id"] != c.CommandID || frame["delivery"] != "sent" {
			t.Fatal(frame)
		}
		b, _ := json.Marshal(frame)
		if strings.Contains(string(b), c.Text) {
			t.Fatal("result leaked text")
		}
	case <-time.After(time.Second):
		t.Fatal("missing result")
	}
	if calls.Load() != 1 {
		t.Fatal(calls.Load())
	}
}
func TestRunnerHerdrBoundedJournal(t *testing.T) {
	dir := t.TempDir()
	var calls atomic.Int32
	m := testHerdrManager(t, dir, &calls)
	journal := filepath.Join(dir, "herdr-commands")
	os.MkdirAll(journal, 0700)
	for i := 0; i < herdrJournalLimit; i++ {
		name := filepath.Join(journal, fmt.Sprintf("entry-%04d.json", i))
		b, _ := json.Marshal(herdrJournalRecord{ExpiresAt: time.Now().Add(time.Hour)})
		if e := os.WriteFile(name, b, 0600); e != nil {
			t.Fatal(e)
		}
	}
	c := testHerdrCommand()
	if r := m.execute(c); r.Code != "journal_full" || calls.Load() != 0 {
		t.Fatal(r, calls.Load())
	}
	b, _ := json.Marshal(herdrJournalRecord{ExpiresAt: time.Now().Add(-time.Hour)})
	os.WriteFile(filepath.Join(journal, "entry-0000.json"), b, 0600)
	if r := m.execute(c); r.Delivery != "sent" || calls.Load() != 1 {
		t.Fatal(r, calls.Load())
	}
}
func TestRunnerHerdrWireDecode(t *testing.T) {
	var msg runnerWSMessage
	raw := `{"type":"herdr_prompt","command_id":"example-command","runtime_session_id":"example-runtime","pane_id":"pane-example","harness_kind":"codex_cli","harness_session_id":"native-example","text":"hello","expires_at":"2026-01-01T00:00:00Z"}`
	if e := json.Unmarshal([]byte(raw), &msg); e != nil {
		t.Fatal(e)
	}
	if msg.CommandID != "example-command" || msg.Text != "hello" || msg.PaneID != "pane-example" {
		t.Fatalf("%+v", msg)
	}
}

func TestRunnerHerdrWebsocketDispatchAndReplay(t *testing.T) {
	dir := testenv.SetTempHome(t)
	var calls atomic.Int32
	m := testHerdrManager(t, dir, &calls)
	server, messages, send := jobServer(t, map[string]any{"type": "hello"})
	defer server.Close()
	defer close(send)
	jobs := newRunnerJobs(1)
	jobs.herdrPrompts = m
	interrupt := make(chan os.Signal, 1)
	stopped := make(chan error, 1)
	conn := dialJobServer(t, server)
	go func() {
		stopped <- runRunnerSession(conn, interrupt, io.Discard, jobs, map[string]any{"type": "heartbeat"})
	}()
	defer func() {
		interrupt <- os.Interrupt
		select {
		case <-stopped:
		case <-time.After(3 * time.Second):
			t.Error("runner failed to stop")
		}
	}()
	c := testHerdrCommand()
	raw, _ := json.Marshal(c)
	var frame map[string]any
	json.Unmarshal(raw, &frame)
	frame["type"] = "herdr_prompt"
	for attempt := 0; attempt < 2; attempt++ {
		send <- frame
		timeout := time.After(3 * time.Second)
		done := false
		for !done {
			select {
			case result := <-messages:
				if result["type"] == "herdr_prompt_result" {
					if result["command_id"] != c.CommandID || result["delivery"] != "sent" {
						t.Fatal(result)
					}
					done = true
				}
			case <-timeout:
				t.Fatal("runner did not return prompt result")
			}
		}
	}
	if calls.Load() != 1 {
		t.Fatal("runner replay injected twice", calls.Load())
	}
}

func TestRunnerHerdrWorkerFailureDoesNotBreakRunner(t *testing.T) {
	dir := t.TempDir()
	var calls atomic.Int32
	m := testHerdrManager(t, dir, &calls)
	m.prompt = func(context.Context, string, herdr.PromptTarget, string) herdr.PromptResult {
		calls.Add(1)
		panic("synthetic adapter failure")
	}
	c := testHerdrCommand()
	m.submit(c)
	select {
	case result := <-m.results:
		if result["delivery"] != "unknown" {
			t.Fatal(result)
		}
	case <-time.After(time.Second):
		t.Fatal("worker did not report failure")
	}
	m = testHerdrManager(t, dir, &calls)
	if r := m.execute(c); r.Delivery != "unknown" || calls.Load() != 1 {
		t.Fatal(r, calls.Load())
	}
}
