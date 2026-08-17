package cmd

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestParseTriggerPayload(t *testing.T) {
	got, err := parseTriggerPayload(`{"ref":"main"}`, nil)
	if err != nil {
		t.Fatal(err)
	}
	if got["ref"] != "main" {
		t.Fatalf("got %#v", got)
	}

	empty, err := parseTriggerPayload("", nil)
	if err != nil || empty != nil {
		t.Fatalf("empty payload: %#v %v", empty, err)
	}

	fromStdin, err := parseTriggerPayload("-", strings.NewReader(`{"n":1}`))
	if err != nil {
		t.Fatal(err)
	}
	if fromStdin["n"].(float64) != 1 {
		t.Fatalf("stdin payload: %#v", fromStdin)
	}

	if _, err := parseTriggerPayload(`["not","object"]`, nil); err == nil {
		t.Fatal("expected error for non-object JSON")
	}
}

func TestShouldWaitDefault(t *testing.T) {
	if !shouldWaitDefault(false, false, false) {
		t.Fatal("CI (no TTY, flag unset) should wait")
	}
	if shouldWaitDefault(false, false, true) {
		t.Fatal("interactive TTY should not wait by default")
	}
	if shouldWaitDefault(true, false, false) {
		t.Fatal("explicit --wait=false must win in CI")
	}
	if !shouldWaitDefault(true, true, true) {
		t.Fatal("explicit --wait must win on a TTY")
	}
}

func TestExtractAndDedupeLogLines(t *testing.T) {
	entries := []flowLogEntry{
		{Payload: map[string]any{"line": "one"}},
		{Payload: map[string]any{"message": "two"}},
		{Payload: map[string]any{}},
	}
	if got := newLogLines("database", 0, entries); strings.Join(got, ",") != "one,two" {
		t.Fatalf("database lines = %#v", got)
	}
	if got := newLogLines("container", 1, entries); strings.Join(got, ",") != "two" {
		t.Fatalf("container dedupe = %#v", got)
	}
}

func TestResolveFlowIDByNameAndUUID(t *testing.T) {
	const flowID = "11111111-2222-4333-8444-555555555555"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/v1/flows/"+flowID:
			_ = json.NewEncoder(w).Encode(flowSummaryResponse{ID: flowID, Name: "Nightly Review"})
		case r.URL.Path == "/api/v1/flows":
			_ = json.NewEncoder(w).Encode([]flowSummaryResponse{
				{ID: flowID, Name: "Nightly Review"},
				{ID: "22222222-2222-4333-8444-555555555555", Name: "Other"},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	client := api.NewClientWithToken(server.URL, "tok")

	got, err := resolveFlowID(client, "Nightly Review")
	if err != nil || got != flowID {
		t.Fatalf("by name: %q %v", got, err)
	}
	got, err = resolveFlowID(client, flowID)
	if err != nil || got != flowID {
		t.Fatalf("by id: %q %v", got, err)
	}
	if _, err := resolveFlowID(client, "missing"); err == nil {
		t.Fatal("expected not found")
	}
}

func TestFlowTriggerPostsAndPrintsID(t *testing.T) {
	const flowID = "11111111-2222-4333-8444-555555555555"
	var gotPath string
	var gotBody map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.Method + " " + r.URL.Path
		switch {
		case r.URL.Path == "/api/v1/flows":
			_ = json.NewEncoder(w).Encode([]flowSummaryResponse{
				{ID: flowID, Name: "Nightly Review"},
			})
		case r.URL.Path == "/api/v1/flows/"+flowID+"/trigger":
			_ = json.NewDecoder(r.Body).Decode(&gotBody)
			_ = json.NewEncoder(w).Encode(flowTriggerResult{
				ID:     "exec-1",
				Status: "PENDING",
				FlowID: flowID,
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	testenv.SetHome(t, t.TempDir())
	oldToken, oldURL := FlagToken, FlagURL
	FlagToken, FlagURL = "tok", server.URL
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })

	if err := flowTriggerCmd.Flags().Set("payload", `{"ref":"main"}`); err != nil {
		t.Fatal(err)
	}
	if err := flowTriggerCmd.Flags().Set("wait", "false"); err != nil {
		t.Fatal(err)
	}
	if err := flowTriggerCmd.Flags().Set("runner", ""); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = flowTriggerCmd.Flags().Set("payload", "")
		_ = flowTriggerCmd.Flags().Set("wait", "false")
		_ = flowTriggerCmd.Flags().Set("runner", "")
	})

	var out bytes.Buffer
	flowTriggerCmd.SetOut(&out)
	if err := runFlowTrigger(flowTriggerCmd, []string{"Nightly Review"}); err != nil {
		t.Fatalf("runFlowTrigger: %v", err)
	}
	if gotPath != "POST /api/v1/flows/"+flowID+"/trigger" {
		t.Fatalf("path = %q", gotPath)
	}
	if gotBody["ref"] != "main" {
		t.Fatalf("body = %#v", gotBody)
	}
	if !strings.Contains(out.String(), "exec-1") {
		t.Fatalf("output = %q", out.String())
	}
}

func TestFlowTriggerRunnerSetsPayload(t *testing.T) {
	const flowID = "11111111-2222-4333-8444-555555555555"
	var gotBody map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/v1/flows":
			_ = json.NewEncoder(w).Encode([]flowSummaryResponse{
				{ID: flowID, Name: "Nightly Review"},
			})
		case strings.HasSuffix(r.URL.Path, "/trigger"):
			_ = json.NewDecoder(r.Body).Decode(&gotBody)
			_ = json.NewEncoder(w).Encode(flowTriggerResult{
				ID: "exec-r", Status: "PENDING", FlowID: flowID,
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	testenv.SetHome(t, t.TempDir())
	oldToken, oldURL := FlagToken, FlagURL
	FlagToken, FlagURL = "tok", server.URL
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })
	if err := flowTriggerCmd.Flags().Set("runner", "local"); err != nil {
		t.Fatal(err)
	}
	if err := flowTriggerCmd.Flags().Set("wait", "false"); err != nil {
		t.Fatal(err)
	}
	if err := flowTriggerCmd.Flags().Set("payload", ""); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = flowTriggerCmd.Flags().Set("runner", "")
		_ = flowTriggerCmd.Flags().Set("wait", "false")
	})
	flowTriggerCmd.SetOut(io.Discard)
	if err := runFlowTrigger(flowTriggerCmd, []string{"Nightly Review"}); err != nil {
		t.Fatalf("runFlowTrigger: %v", err)
	}
	if gotBody["_runner"] != "local" {
		t.Fatalf("payload = %#v", gotBody)
	}
}

func TestWaitForExecutionStreamsLogsAndFails(t *testing.T) {
	polls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasPrefix(r.URL.Path, "/api/v1/flows/executions/exec-1/logs"):
			line := "hello from agent"
			if polls > 1 {
				line = "done"
			}
			_ = json.NewEncoder(w).Encode(flowLogsResponse{
				Source: "database",
				Logs:   []flowLogEntry{{Payload: map[string]any{"line": line}}},
			})
		case r.URL.Path == "/api/v1/flows/executions/exec-1":
			polls++
			status := "RUNNING"
			if polls >= 2 {
				status = "FAILED"
			}
			_ = json.NewEncoder(w).Encode(flowExecutionStatus{ID: "exec-1", Status: status})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	oldSleep := flowSleep
	flowSleep = func(time.Duration) {}
	t.Cleanup(func() { flowSleep = oldSleep })

	var out bytes.Buffer
	client := api.NewClientWithToken(server.URL, "tok")
	err := waitForExecution(client, "exec-1", time.Minute, &out)
	if err == nil || !strings.Contains(err.Error(), "FAILED") {
		t.Fatalf("expected FAILED, got %v", err)
	}
	if !strings.Contains(out.String(), "hello from agent") {
		t.Fatalf("logs not streamed: %q", out.String())
	}
}

func TestWaitForExecutionSucceeds(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/logs") || strings.Contains(r.URL.Path, "/logs") {
			_ = json.NewEncoder(w).Encode(flowLogsResponse{
				Source: "database",
				Logs:   []flowLogEntry{{Payload: map[string]any{"message": "ok"}}},
			})
			return
		}
		_ = json.NewEncoder(w).Encode(flowExecutionStatus{ID: "exec-2", Status: "SUCCEEDED"})
	}))
	defer server.Close()

	oldSleep := flowSleep
	flowSleep = func(time.Duration) {}
	t.Cleanup(func() { flowSleep = oldSleep })

	var out bytes.Buffer
	if err := waitForExecution(api.NewClientWithToken(server.URL, "tok"), "exec-2", time.Minute, &out); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "ok") {
		t.Fatalf("output = %q", out.String())
	}
}

func TestWaitForExecutionTimeout(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/logs") {
			_ = json.NewEncoder(w).Encode(flowLogsResponse{Logs: []flowLogEntry{}})
			return
		}
		_ = json.NewEncoder(w).Encode(flowExecutionStatus{ID: "exec-3", Status: "RUNNING"})
	}))
	defer server.Close()

	oldSleep := flowSleep
	oldNow := flowNow
	calls := 0
	start := time.Unix(0, 0)
	flowNow = func() time.Time {
		calls++
		if calls > 2 {
			return start.Add(2 * time.Hour)
		}
		return start
	}
	flowSleep = func(time.Duration) {}
	t.Cleanup(func() {
		flowSleep = oldSleep
		flowNow = oldNow
	})

	err := waitForExecution(api.NewClientWithToken(server.URL, "tok"), "exec-3", time.Minute, io.Discard)
	if err == nil || !strings.Contains(err.Error(), "timed out") {
		t.Fatalf("expected timeout, got %v", err)
	}
}
