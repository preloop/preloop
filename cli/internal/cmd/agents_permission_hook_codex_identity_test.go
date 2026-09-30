package cmd

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPermissionHookParallelCodexModels(t *testing.T) {
	dir := t.TempDir()
	for _, tc := range []struct{ session, model string }{{"session-one", "gpt-alpha"}, {"session-two", "gpt-beta"}} {
		path := filepath.Join(dir, tc.session+".jsonl")
		data := `{"type":"session_meta","payload":{"id":"` + tc.session + `"}}` + "\n" +
			`{"type":"turn_context","payload":{"turn_id":"old-turn","model":"old-model"}}` + "\n" +
			`{"type":"response_item","payload":{"secret":"private tool output"}}` + "\n" +
			`{"type":"turn_context","payload":{"turn_id":"current-turn","model":"` + tc.model + `"}}` + "\n"
		if err := os.WriteFile(path, []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
		raw, _ := json.Marshal(map[string]interface{}{"session_id": tc.session, "transcript_path": path, "turn_id": "current-turn", "tool_name": "Bash", "tool_input": map[string]interface{}{"model": "forged"}})
		req, err := buildPermissionRequest(permissionSourceCodexCLI, raw, permissionHookCredential{})
		if err != nil || req.SessionID != tc.session || req.Model != tc.model {
			t.Fatalf("wrong origin: %+v, %v", req, err)
		}
		if got := codexPermissionModel("different-session", path, ""); got != "" {
			t.Fatalf("model from another session: %q", got)
		}
		if got := codexPermissionModel(tc.session, path, "absent-turn"); got != "" {
			t.Fatalf("model from another turn: %q", got)
		}
	}
}

func TestCodexPermissionModelUnknownAndExplicit(t *testing.T) {
	req, err := buildPermissionRequest(permissionSourceCodexCLI, []byte(`{"session_id":"missing","transcript_path":"/nonexistent","model":"explicit-model","tool_name":"Bash"}`), permissionHookCredential{})
	if err != nil || req.Model != "explicit-model" {
		t.Fatalf("explicit model lost: %+v, %v", req, err)
	}
	req, err = buildPermissionRequest(permissionSourceCodexCLI, []byte(`{"turn_id":"turn-only","tool_name":"Bash","tool_input":{"model":"forged"}}`), permissionHookCredential{})
	if err != nil || req.Model != "" || req.SessionID != "" {
		t.Fatalf("invented session/model: %+v, %v", req, err)
	}
}

func TestApprovalOriginLabels(t *testing.T) {
	session, model := approvalOriginLabels(ApprovalRequest{RuntimeSessionID: "shared-session", ToolArgs: map[string]interface{}{"_preloop_origin": map[string]interface{}{"session_id": "origin-session", "model": "gpt-alpha"}}})
	if session != "origin-session" || model != "gpt-alpha" {
		t.Fatalf("wrong labels %q %q", session, model)
	}
	session, model = approvalOriginLabels(ApprovalRequest{})
	if session != "Unknown" || model != "Unknown" {
		t.Fatalf("missing labels %q %q", session, model)
	}
}

func TestCodexPermissionModelBoundedTailAndMissingLatestModel(t *testing.T) {
	path := filepath.Join(t.TempDir(), "rollout.jsonl")
	header := `{"type":"session_meta","payload":{"id":"session-one"}}` + "\n"
	// A giant history item must not be decoded or hide a later identity envelope.
	data := header + `{"type":"response_item","payload":{"text":"` + strings.Repeat("x", 2*1024*1024) + `"}}` + "\n" +
		`{"type":"turn_context","payload":{"turn_id":"current-turn","model":"gpt-alpha"}}` + "\n"
	if err := os.WriteFile(path, []byte(data), 0600); err != nil {
		t.Fatal(err)
	}
	if got := codexPermissionModel("session-one", path, "current-turn"); got != "gpt-alpha" {
		t.Fatalf("tail model: %q", got)
	}
	data += `{"type":"turn_context","payload":{"turn_id":"new-turn"}}` + "\n"
	if err := os.WriteFile(path, []byte(data), 0600); err != nil {
		t.Fatal(err)
	}
	if got := codexPermissionModel("session-one", path, ""); got != "" {
		t.Fatalf("borrowed previous model: %q", got)
	}
}

func TestApprovalOriginLabelsDistinguishMCPRuntimeFromNativeOrigin(t *testing.T) {
	session, model := approvalOriginLabels(ApprovalRequest{RuntimeSessionID: "mcp-session"})
	if session != "mcp-session" || model != "Unknown" {
		t.Fatalf("lost authenticated MCP linkage: %q %q", session, model)
	}
	session, model = approvalOriginLabels(ApprovalRequest{RuntimeSessionID: "shared-native", ToolArgs: map[string]interface{}{"_preloop_source": "codex_cli"}})
	if session != "Unknown" || model != "Unknown" {
		t.Fatalf("invented native origin: %q %q", session, model)
	}
}
