package cmd

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestRecordsFromCopilotHookEventMapping(t *testing.T) {
	now := time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC)
	cases := []struct {
		name     string
		payload  string
		wantType string
	}{
		{
			name:     "sessionStart camelCase",
			payload:  `{"sessionId":"s1","timestamp":1720000000000,"cwd":"/tmp","source":"startup"}`,
			wantType: "session_start",
		},
		{
			name:     "sessionEnd camelCase",
			payload:  `{"sessionId":"s1","timestamp":1720000001000,"cwd":"/tmp","reason":"complete"}`,
			wantType: "session_end",
		},
		{
			name:     "subagentStart camelCase",
			payload:  `{"sessionId":"s1","timestamp":1720000002000,"cwd":"/tmp","transcriptPath":"/t","agentName":"explore"}`,
			wantType: "subagent_start",
		},
		{
			name:     "subagentStop camelCase",
			payload:  `{"sessionId":"s1","timestamp":1720000003000,"cwd":"/tmp","transcriptPath":"/t","agentId":"a1","agentType":"explore","agentName":"explore","response":"done","stopReason":"end_turn"}`,
			wantType: "subagent_stop",
		},
		{
			name:     "agentStop camelCase",
			payload:  `{"sessionId":"s1","timestamp":1720000004000,"cwd":"/tmp","transcriptPath":"/t","stopReason":"end_turn","stop_hook_active":false}`,
			wantType: "response",
		},
		{
			name:     "agentStop with a final message is not a subagent",
			payload:  `{"sessionId":"s1","timestamp":1720000004500,"cwd":"/tmp","stopReason":"end_turn","response":"finished the edit"}`,
			wantType: "response",
		},
		{
			name:     "SessionStart VS Code form",
			payload:  `{"hook_event_name":"SessionStart","session_id":"s2","timestamp":"2026-09-24T12:00:00Z","cwd":"/tmp","source":"startup"}`,
			wantType: "session_start",
		},
		{
			name:     "Stop VS Code form",
			payload:  `{"hook_event_name":"Stop","session_id":"s2","timestamp":"2026-09-24T12:00:01Z","cwd":"/tmp","transcript_path":"/t","stop_reason":"end_turn","stop_hook_active":false}`,
			wantType: "response",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			records, err := recordsFromCopilotHook(json.RawMessage(tc.payload), "", now)
			if err != nil {
				t.Fatalf("recordsFromCopilotHook: %v", err)
			}
			if len(records) != 1 {
				t.Fatalf("got %d records, want 1", len(records))
			}
			rec := records[0]
			if rec["event_type"] != tc.wantType {
				t.Errorf("event_type=%v want %s", rec["event_type"], tc.wantType)
			}
			if rec["cost_basis"] != "estimated" {
				t.Errorf("cost_basis=%v", rec["cost_basis"])
			}
			for _, key := range []string{"input_tokens", "output_tokens", "charged_cost"} {
				if _, present := rec[key]; present {
					t.Errorf("%s must be omitted when unreported, got %#v", key, rec[key])
				}
			}
			conv, _ := rec["conversation_id"].(string)
			if conv == "" {
				t.Errorf("missing conversation_id")
			}
			ext, _ := rec["external_id"].(string)
			if !strings.Contains(ext, tc.wantType) || !strings.Contains(ext, conv) {
				t.Errorf("external_id=%q should be event+session+timestamp", ext)
			}
		})
	}
}

func TestRecordsFromCopilotHookSkipsToolEvents(t *testing.T) {
	now := time.Now().UTC()
	records, err := recordsFromCopilotHook(json.RawMessage(
		`{"sessionId":"s1","toolName":"bash","toolArgs":{"command":"ls"}}`,
	), "", now)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 0 {
		t.Fatalf("tool events must not ship usage rows: %#v", records)
	}
}

func TestDetectUsageHookFormatCopilotNotCursor(t *testing.T) {
	camel := json.RawMessage(`{"sessionId":"s1","source":"startup","timestamp":1}`)
	if got := detectUsageHookFormat(camel); got != usageHookFormatCopilot {
		t.Errorf("camelCase detect=%q want copilot", got)
	}
	vscode := json.RawMessage(`{"hook_event_name":"SessionStart","session_id":"s1"}`)
	if got := detectUsageHookFormat(vscode); got != usageHookFormatCopilot {
		t.Errorf("VS Code detect=%q want copilot", got)
	}
	cursor := json.RawMessage(`{"hook_event_name":"sessionStart","conversation_id":"c1"}`)
	if got := detectUsageHookFormat(cursor); got != usageHookFormatCursor {
		t.Errorf("Cursor detect=%q want cursor", got)
	}
}

func TestParseUsageHookFormatCopilot(t *testing.T) {
	got, err := parseUsageHookFormat("copilot")
	if err != nil || got != usageHookFormatCopilot {
		t.Fatalf("parse=%q err=%v", got, err)
	}
}

func TestResolveUsageHookSourceCopilot(t *testing.T) {
	if got := resolveUsageHookSource(usageHookFormatCopilot, "cursor", false); got != "copilot_cli" {
		t.Errorf("default source=%q want copilot_cli", got)
	}
}

func TestCopilotUsageHookSourceMatchesManagedKind(t *testing.T) {
	got := resolveUsageHookSource(usageHookFormatCopilot, "", false)
	want := managedAgentKindForAgent(copilotCLIAgentName)
	if got != want {
		t.Fatalf("usage hook source %q != managed kind %q", got, want)
	}
}
