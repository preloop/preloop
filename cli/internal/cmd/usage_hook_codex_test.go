package cmd

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestUsageHookCodexRolloutMapsSessionAndTokens(t *testing.T) {
	var gotBody map[string]interface{}
	withUsageHookServer(t, usageHookOKHandler(t, &gotBody))

	stdin := strings.Join([]string{
		`{"timestamp":"2026-08-30T23:17:57.219Z","ordinal":0,"type":"session_meta","payload":{"session_id":"sess-1","id":"sess-1","originator":"codex-tui","cli_version":"0.151.0","source":"cli","thread_source":"user","model_provider":"openai"}}`,
		`{"timestamp":"2026-08-30T23:17:58.585Z","ordinal":7,"type":"turn_context","payload":{"turn_id":"turn-1","model":"openai/gpt-5.6-sol"}}`,
		`{"timestamp":"2026-08-30T23:17:58.590Z","ordinal":8,"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"SECRET_PROMPT"}]}}`,
		`{"timestamp":"2026-08-30T23:18:02.226Z","ordinal":12,"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":11869,"cached_input_tokens":0,"cache_write_input_tokens":3,"output_tokens":17,"reasoning_output_tokens":2,"total_tokens":11886},"last_token_usage":{"input_tokens":11869,"cached_input_tokens":4,"cache_write_input_tokens":3,"output_tokens":17,"reasoning_output_tokens":2,"total_tokens":11886},"model_context_window":258400}}}`,
		`{"timestamp":"2026-08-30T23:18:02.300Z","ordinal":13,"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":11869,"cached_input_tokens":0,"cache_write_input_tokens":3,"output_tokens":17,"reasoning_output_tokens":2,"total_tokens":11886},"last_token_usage":{"input_tokens":11869,"cached_input_tokens":4,"cache_write_input_tokens":3,"output_tokens":17,"reasoning_output_tokens":2,"total_tokens":11886}}}}`,
		`{"timestamp":"2026-08-30T23:18:03.000Z","ordinal":14,"type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-1"}}`,
	}, "\n")

	cmd, stdout, stderr := newUsageHookTestCmd(stdin)
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	if stdout.String() != "" {
		t.Fatalf("stdin must be silent, got %q", stdout.String())
	}
	if stderr.String() != "" {
		t.Fatalf("unexpected stderr: %q", stderr.String())
	}
	if gotBody["source"] != "codex" {
		t.Fatalf("codex default source, got %#v", gotBody["source"])
	}

	records := decodeIngestRecords(t, gotBody)
	if len(records) != 3 {
		t.Fatalf("expected session_start + usage + response, got %d: %#v", len(records), records)
	}

	start := records[0]
	if start["event_type"] != "session_start" || start["conversation_id"] != "sess-1" {
		t.Fatalf("session_meta mapping: %#v", start)
	}
	if start["cost_basis"] != "estimated" {
		t.Fatalf("session_start must be estimated, got %#v", start["cost_basis"])
	}

	usage := records[1]
	if usage["event_type"] != "usage" || usage["model"] != "openai/gpt-5.6-sol" {
		t.Fatalf("token_count mapping: %#v", usage)
	}
	if usage["input_tokens"] != float64(11869) || usage["output_tokens"] != float64(17) {
		t.Fatalf("must use last_token_usage, got %#v", usage)
	}
	if usage["cache_read_tokens"] != float64(4) {
		t.Fatalf("cached_input_tokens -> cache_read_tokens, got %#v", usage)
	}
	if _, present := usage["charged_cost"]; present {
		t.Fatalf("Codex logs have no billed USD; must not fabricate cost: %#v", usage)
	}
	metadata, _ := usage["metadata"].(map[string]interface{})
	if metadata["cache_write_input_tokens"] != float64(3) || metadata["reasoning_output_tokens"] != float64(2) {
		t.Fatalf("non-ingest token fields stay in metadata: %#v", metadata)
	}

	response := records[2]
	if response["event_type"] != "response" {
		t.Fatalf("task_complete mapping: %#v", response)
	}

	encoded, _ := json.Marshal(gotBody)
	if strings.Contains(string(encoded), "SECRET_PROMPT") {
		t.Fatal("response_item content must never leave the machine")
	}
}

func TestUsageHookCodexSubagentUsesThreadIdAndParent(t *testing.T) {
	var gotBody map[string]interface{}
	withUsageHookServer(t, usageHookOKHandler(t, &gotBody))

	stdin := strings.Join([]string{
		`{"timestamp":"2026-07-27T11:14:14.370Z","type":"session_meta","payload":{"session_id":"parent-thread","id":"child-thread","forked_from_id":"parent-thread","parent_thread_id":"parent-thread","source":{"subagent":{"thread_spawn":{"parent_thread_id":"parent-thread","depth":1,"agent_path":"/secret/path","agent_nickname":"Godel"}}},"thread_source":"subagent","agent_nickname":"Godel"}}`,
		`{"timestamp":"2026-07-27T10:58:12.945Z","type":"session_meta","payload":{"session_id":"parent-thread","id":"parent-thread","source":"exec","thread_source":"user"}}`,
		`{"timestamp":"2026-07-27T11:14:15.000Z","type":"turn_context","payload":{"model":"openai/gpt-5"}}`,
		`{"timestamp":"2026-07-27T11:14:16.000Z","type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6},"total_token_usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6}}}}`,
	}, "\n")

	cmd, _, stderr := newUsageHookTestCmd(stdin)
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	if stderr.String() != "" {
		t.Fatalf("unexpected stderr: %q", stderr.String())
	}
	records := decodeIngestRecords(t, gotBody)
	start := records[0]
	if start["event_type"] != "subagent_start" {
		t.Fatalf("child session_meta with parent must be subagent_start, got %#v", start)
	}
	if start["conversation_id"] != "child-thread" {
		t.Fatalf("conversation_id must be payload.id, not session_id, got %#v", start["conversation_id"])
	}
	if start["parent_conversation_id"] != "parent-thread" {
		t.Fatalf("parent_conversation_id: %#v", start["parent_conversation_id"])
	}
	encoded, _ := json.Marshal(gotBody)
	if strings.Contains(string(encoded), "/secret/path") {
		t.Fatal("agent_path must not be shipped")
	}
	for _, record := range records {
		if record["conversation_id"] != "child-thread" {
			t.Fatalf("inherited parent session_meta must not retarget the file: %#v", record)
		}
	}
}

func TestUsageHookCodexFileImport(t *testing.T) {
	var gotBody map[string]interface{}
	withUsageHookServer(t, usageHookOKHandler(t, &gotBody))

	dir := t.TempDir()
	path := filepath.Join(dir, "rollout-2026-09-01T00-00-00-sess.jsonl")
	content := `{"timestamp":"2026-09-01T00:00:00Z","type":"session_meta","payload":{"id":"sess-file","session_id":"sess-file","source":"cli"}}` + "\n"
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}

	cmd, stdout, stderr := newUsageHookTestCmd("")
	if err := cmd.Flags().Set("from", "codex"); err != nil {
		t.Fatalf("set from: %v", err)
	}
	if err := cmd.Flags().Set("file", path); err != nil {
		t.Fatalf("set file: %v", err)
	}
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	if stderr.String() != "" {
		t.Fatalf("unexpected stderr: %q", stderr.String())
	}
	if !strings.Contains(stdout.String(), "1 usage record") {
		t.Fatalf("expected summary, got %q", stdout.String())
	}
	record := decodeSingleIngestRecord(t, gotBody)
	if record["conversation_id"] != "sess-file" {
		t.Fatalf("file import: %#v", record)
	}
	if gotBody["source"] != "codex" {
		t.Fatalf("source: %#v", gotBody["source"])
	}
}
