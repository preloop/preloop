package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"
)

// newUsageHookTestCmd builds a fresh command wired like usageHookCmd so
// flag state never leaks between tests.
func newUsageHookTestCmd(stdin string) (*cobra.Command, *bytes.Buffer, *bytes.Buffer) {
	cmd := &cobra.Command{Use: "hook", RunE: runUsageHook}
	cmd.Flags().String("agent-id", "", "")
	cmd.Flags().String("source", "cursor", "")
	cmd.Flags().String("parent-conversation-id", "", "")

	stdout := &bytes.Buffer{}
	stderr := &bytes.Buffer{}
	cmd.SetIn(strings.NewReader(stdin))
	cmd.SetOut(stdout)
	cmd.SetErr(stderr)
	return cmd, stdout, stderr
}

// withUsageHookServer points the global CLI connection flags at a test
// server for the duration of one test.
func withUsageHookServer(t *testing.T, handler http.HandlerFunc) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(handler)
	prevURL, prevToken := FlagURL, FlagToken
	FlagURL, FlagToken = server.URL, "test-token"
	t.Cleanup(func() {
		FlagURL, FlagToken = prevURL, prevToken
		server.Close()
	})
	return server
}

func usageHookOKHandler(t *testing.T, gotBody *map[string]interface{}) http.HandlerFunc {
	t.Helper()
	return func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(gotBody); err != nil {
			t.Errorf("decode body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"accepted": 1}`))
	}
}

func decodeSingleIngestRecord(t *testing.T, body map[string]interface{}) map[string]interface{} {
	t.Helper()
	records, ok := body["records"].([]interface{})
	if !ok || len(records) != 1 {
		t.Fatalf("expected exactly one record, got %#v", body["records"])
	}
	record, ok := records[0].(map[string]interface{})
	if !ok {
		t.Fatalf("record is not an object: %#v", records[0])
	}
	return record
}

func TestUsageHookShipsStopEventAsResponse(t *testing.T) {
	var gotBody map[string]interface{}
	var gotPath string
	withUsageHookServer(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		usageHookOKHandler(t, &gotBody)(w, r)
	})

	cmd, stdout, stderr := newUsageHookTestCmd(
		`{"conversation_id":"conv-1","generation_id":"gen-9",` +
			`"hook_event_name":"stop","status":"completed","loop_count":0,` +
			`"model":"composer","workspace_roots":["/repo"]}`,
	)
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}

	if gotPath != usageIngestPath {
		t.Fatalf("unexpected path %q", gotPath)
	}
	if gotBody["source"] != "cursor" {
		t.Fatalf("unexpected source: %#v", gotBody["source"])
	}
	record := decodeSingleIngestRecord(t, gotBody)
	if record["event_type"] != "response" {
		t.Fatalf("stop must map to a response event, got %#v", record["event_type"])
	}
	if record["conversation_id"] != "conv-1" {
		t.Fatalf("unexpected conversation_id: %#v", record["conversation_id"])
	}
	if record["external_id"] != "stop:gen-9:0" {
		t.Fatalf("unexpected external_id: %#v", record["external_id"])
	}
	if record["cost_basis"] != "estimated" {
		t.Fatalf("hook records must be estimated basis, got %#v", record["cost_basis"])
	}
	if record["model"] != "composer" {
		t.Fatalf("reported model must pass through, got %#v", record["model"])
	}
	// Honesty rail: hook payloads carry no tokens or spend, so the record
	// must not fabricate any.
	for _, forbidden := range []string{
		"charged_cost", "input_tokens", "output_tokens", "cache_read_tokens",
	} {
		if _, present := record[forbidden]; present {
			t.Fatalf("record must not fabricate %q: %#v", forbidden, record)
		}
	}
	metadata, _ := record["metadata"].(map[string]interface{})
	if metadata["hook_event_name"] != "stop" || metadata["hook_status"] != "completed" {
		t.Fatalf("unexpected metadata: %#v", record["metadata"])
	}
	if _, err := time.Parse(time.RFC3339Nano, record["timestamp"].(string)); err != nil {
		t.Fatalf("timestamp not RFC3339: %v", err)
	}
	// Shipped hook events are observational for Cursor: no stdout output.
	if stdout.String() != "" {
		t.Fatalf("hook must not write to stdout, got %q", stdout.String())
	}
	if stderr.String() != "" {
		t.Fatalf("unexpected stderr: %q", stderr.String())
	}
}

func TestUsageHookEventTypeMapping(t *testing.T) {
	testCases := []struct {
		hookEvent string
		eventType string
	}{
		{hookEvent: "sessionStart", eventType: "session_start"},
		{hookEvent: "sessionEnd", eventType: "session_end"},
		{hookEvent: "subagentStart", eventType: "subagent_start"},
		{hookEvent: "subagentStop", eventType: "subagent_stop"},
		{hookEvent: "stop", eventType: "response"},
		{hookEvent: "preCompact", eventType: "compaction"},
	}

	for _, tc := range testCases {
		t.Run(tc.hookEvent, func(t *testing.T) {
			var gotBody map[string]interface{}
			withUsageHookServer(t, usageHookOKHandler(t, &gotBody))

			payload, _ := json.Marshal(map[string]interface{}{
				"conversation_id": "conv-1",
				"generation_id":   "gen-1",
				"hook_event_name": tc.hookEvent,
			})
			cmd, _, _ := newUsageHookTestCmd(string(payload))
			if err := cmd.Execute(); err != nil {
				t.Fatalf("execute: %v", err)
			}
			record := decodeSingleIngestRecord(t, gotBody)
			if record["event_type"] != tc.eventType {
				t.Fatalf("expected %q, got %#v", tc.eventType, record["event_type"])
			}
		})
	}
}

func TestUsageHookSubagentStartCarriesDocumentedParent(t *testing.T) {
	var gotBody map[string]interface{}
	withUsageHookServer(t, usageHookOKHandler(t, &gotBody))

	cmd, _, _ := newUsageHookTestCmd(
		`{"conversation_id":"conv-worker","generation_id":"gen-1",` +
			`"hook_event_name":"subagentStart","subagent_id":"sub-1",` +
			`"subagent_type":"explore","parent_conversation_id":"conv-parent",` +
			`"is_parallel_worker":true,"task":"Explore the auth flow"}`,
	)
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}

	record := decodeSingleIngestRecord(t, gotBody)
	if record["parent_conversation_id"] != "conv-parent" {
		t.Fatalf("documented parent must pass through, got %#v", record["parent_conversation_id"])
	}
	if record["external_id"] != "subagentStart:sub-1" {
		t.Fatalf("subagent_id must key the record, got %#v", record["external_id"])
	}
	metadata, _ := record["metadata"].(map[string]interface{})
	if metadata["subagent_type"] != "explore" || metadata["is_parallel_worker"] != true {
		t.Fatalf("unexpected metadata: %#v", record["metadata"])
	}
	// Task descriptions are content, not cost signal; they must not ship.
	if _, present := metadata["task"]; present {
		t.Fatalf("task text must not leave the machine: %#v", metadata)
	}
}

func TestUsageHookSubagentStopShipsGrowthTripwires(t *testing.T) {
	var gotBody map[string]interface{}
	withUsageHookServer(t, usageHookOKHandler(t, &gotBody))

	cmd, _, _ := newUsageHookTestCmd(
		`{"conversation_id":"conv-worker","generation_id":"gen-1",` +
			`"hook_event_name":"subagentStop","subagent_type":"generalPurpose",` +
			`"status":"completed","message_count":12,"tool_call_count":8,` +
			`"summary":"secret summary text"}`,
	)
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}

	record := decodeSingleIngestRecord(t, gotBody)
	if record["message_count"] != float64(12) || record["tool_call_count"] != float64(8) {
		t.Fatalf("documented counters must pass through, got %#v", record)
	}
	metadata, _ := record["metadata"].(map[string]interface{})
	if metadata["hook_status"] != "completed" {
		t.Fatalf("unexpected metadata: %#v", metadata)
	}
	if _, present := metadata["summary"]; present {
		t.Fatalf("summary text must not leave the machine: %#v", metadata)
	}
	// No per-fire id is documented for subagentStop, so the key must stay
	// unique across parallel workers via the receive-time suffix.
	externalID, _ := record["external_id"].(string)
	if !strings.HasPrefix(externalID, "subagentStop:gen-1:") {
		t.Fatalf("unexpected external_id: %q", externalID)
	}
}

func TestUsageHookSessionEventsAreKeyedByConversation(t *testing.T) {
	for hookEvent, wantPrefix := range map[string]string{
		"sessionStart": "sessionStart:conv-1",
		"sessionEnd":   "sessionEnd:conv-1",
	} {
		var gotBody map[string]interface{}
		withUsageHookServer(t, usageHookOKHandler(t, &gotBody))

		payload, _ := json.Marshal(map[string]interface{}{
			"conversation_id": "conv-1",
			"generation_id":   "gen-1",
			"hook_event_name": hookEvent,
			"session_id":      "conv-1",
		})
		cmd, _, _ := newUsageHookTestCmd(string(payload))
		if err := cmd.Execute(); err != nil {
			t.Fatalf("execute: %v", err)
		}
		record := decodeSingleIngestRecord(t, gotBody)
		if record["external_id"] != wantPrefix {
			t.Fatalf("unexpected external_id for %s: %#v", hookEvent, record["external_id"])
		}
	}
}

func TestUsageHookFallsBackToSessionID(t *testing.T) {
	var gotBody map[string]interface{}
	withUsageHookServer(t, usageHookOKHandler(t, &gotBody))

	cmd, _, _ := newUsageHookTestCmd(
		`{"session_id":"conv-sess","hook_event_name":"sessionEnd","reason":"completed"}`,
	)
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}

	record := decodeSingleIngestRecord(t, gotBody)
	if record["conversation_id"] != "conv-sess" {
		t.Fatalf("session_id must back-fill conversation_id, got %#v", record["conversation_id"])
	}
	metadata, _ := record["metadata"].(map[string]interface{})
	if metadata["session_end_reason"] != "completed" {
		t.Fatalf("unexpected metadata: %#v", metadata)
	}
}

func TestUsageHookSkipsNonLifecycleEventsWithoutPosting(t *testing.T) {
	for _, hookEvent := range []string{
		"beforeShellExecution", "beforeMCPExecution", "beforeReadFile",
		"afterFileEdit", "beforeSubmitPrompt", "afterAgentResponse",
		"preToolUse", "postToolUse", "workspaceOpen",
	} {
		posted := false
		withUsageHookServer(t, func(w http.ResponseWriter, r *http.Request) {
			posted = true
			w.WriteHeader(http.StatusOK)
		})

		payload, _ := json.Marshal(map[string]interface{}{
			"conversation_id": "conv-1",
			"hook_event_name": hookEvent,
		})
		cmd, stdout, stderr := newUsageHookTestCmd(string(payload))
		if err := cmd.Execute(); err != nil {
			t.Fatalf("execute %s: %v", hookEvent, err)
		}
		if posted {
			t.Fatalf("%s must not be shipped", hookEvent)
		}
		if stdout.String() != "" || stderr.String() != "" {
			t.Fatalf("expected silence for %s, got stdout=%q stderr=%q",
				hookEvent, stdout.String(), stderr.String())
		}
	}
}

func TestUsageHookParentConversationPrecedence(t *testing.T) {
	testCases := []struct {
		name           string
		payloadParent  string
		flagParent     string
		envParent      string
		expectedParent string // "" means the field must be absent
	}{
		{
			name:           "documented payload field wins",
			payloadParent:  "conv-from-payload",
			flagParent:     "conv-from-flag",
			envParent:      "conv-from-env",
			expectedParent: "conv-from-payload",
		},
		{
			name:           "flag beats env",
			flagParent:     "conv-from-flag",
			envParent:      "conv-from-env",
			expectedParent: "conv-from-flag",
		},
		{
			name:           "env is the last resort",
			envParent:      "conv-from-env",
			expectedParent: "conv-from-env",
		},
		{
			name:           "absent stays absent - never fabricated",
			expectedParent: "",
		},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			var gotBody map[string]interface{}
			withUsageHookServer(t, usageHookOKHandler(t, &gotBody))
			t.Setenv("PRELOOP_PARENT_CONVERSATION_ID", tc.envParent)

			payload := map[string]interface{}{
				"conversation_id": "conv-child",
				"generation_id":   "gen-3",
				"hook_event_name": "stop",
				"loop_count":      0,
			}
			if tc.payloadParent != "" {
				payload["parent_conversation_id"] = tc.payloadParent
			}
			encoded, _ := json.Marshal(payload)

			cmd, _, _ := newUsageHookTestCmd(string(encoded))
			if tc.flagParent != "" {
				if err := cmd.Flags().Set("parent-conversation-id", tc.flagParent); err != nil {
					t.Fatalf("set flag: %v", err)
				}
			}
			if err := cmd.Execute(); err != nil {
				t.Fatalf("execute: %v", err)
			}

			record := decodeSingleIngestRecord(t, gotBody)
			parent, present := record["parent_conversation_id"]
			if tc.expectedParent == "" {
				if present {
					t.Fatalf("parent must be absent, got %#v", parent)
				}
				return
			}
			if parent != tc.expectedParent {
				t.Fatalf("expected parent %q, got %#v", tc.expectedParent, parent)
			}
		})
	}
}

func TestUsageHookFailsOpenWithoutConversationID(t *testing.T) {
	posted := false
	withUsageHookServer(t, func(w http.ResponseWriter, r *http.Request) {
		posted = true
		w.WriteHeader(http.StatusOK)
	})

	cmd, _, stderr := newUsageHookTestCmd(
		`{"generation_id":"gen-1","hook_event_name":"stop"}`,
	)
	if err := cmd.Execute(); err != nil {
		t.Fatalf("hook must fail open, got error: %v", err)
	}

	if posted {
		t.Fatal("a record without a conversation_id must not be shipped")
	}
	if !strings.Contains(stderr.String(), "conversation_id") {
		t.Fatalf("expected a conversation_id warning, got %q", stderr.String())
	}
}

func TestUsageHookFailsOpenOnServerError(t *testing.T) {
	withUsageHookServer(t, func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, `{"detail":"boom"}`, http.StatusInternalServerError)
	})

	cmd, stdout, stderr := newUsageHookTestCmd(
		`{"conversation_id":"conv-1","generation_id":"gen-1",` +
			`"hook_event_name":"stop","loop_count":0}`,
	)
	if err := cmd.Execute(); err != nil {
		t.Fatalf("hook must fail open, got error: %v", err)
	}

	if !strings.Contains(stderr.String(), "not recorded") {
		t.Fatalf("expected a shipment warning, got %q", stderr.String())
	}
	if stdout.String() != "" {
		t.Fatalf("no stdout output expected, got %q", stdout.String())
	}
}

func TestUsageHookFailsOpenOnMalformedPayload(t *testing.T) {
	posted := false
	withUsageHookServer(t, func(w http.ResponseWriter, r *http.Request) {
		posted = true
		w.WriteHeader(http.StatusOK)
	})

	cmd, _, stderr := newUsageHookTestCmd(`this is not json`)
	if err := cmd.Execute(); err != nil {
		t.Fatalf("hook must fail open, got error: %v", err)
	}

	if posted {
		t.Fatal("malformed payloads must not be shipped")
	}
	if !strings.Contains(stderr.String(), "parse hook payload") {
		t.Fatalf("expected a parse warning, got %q", stderr.String())
	}
}

func TestUsageHookForwardsAgentIDAndSource(t *testing.T) {
	var gotBody map[string]interface{}
	withUsageHookServer(t, usageHookOKHandler(t, &gotBody))

	cmd, _, _ := newUsageHookTestCmd(
		`{"conversation_id":"conv-1","generation_id":"gen-1",` +
			`"hook_event_name":"stop","loop_count":0}`,
	)
	if err := cmd.Flags().Set("agent-id", "agent-42"); err != nil {
		t.Fatalf("set flag: %v", err)
	}
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}

	if gotBody["agent_id"] != "agent-42" {
		t.Fatalf("unexpected agent_id: %#v", gotBody["agent_id"])
	}
	if gotBody["source"] != "cursor" {
		t.Fatalf("unexpected source: %#v", gotBody["source"])
	}
}
