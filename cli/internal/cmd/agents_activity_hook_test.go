package cmd

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestOnboardInstallsPostToolUseAndNotificationHooks(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	agent := AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, "config.json")}
	for i := 0; i < 2; i++ { // re-onboarding replaces rather than duplicates
		if err := installApprovalHooks(agent, "http://127.0.0.1:1", "agt_synthetic", nil); err != nil {
			t.Fatal(err)
		}
	}
	path, err := approvalHookConfigPath(permissionSourceClaudeCode)
	if err != nil {
		t.Fatal(err)
	}
	hooks := readJSONDoc(t, path)["hooks"].(map[string]interface{})
	for _, want := range activityHookEvents {
		list, ok := hooks[want.event].([]interface{})
		if !ok || len(list) != 1 {
			t.Fatalf("%s not installed once: %v", want.event, hooks[want.event])
		}
		entry := list[0].(map[string]interface{})
		if want.matcher != "" && entry["matcher"] != want.matcher {
			t.Fatalf("%s matcher = %v", want.event, entry["matcher"])
		}
		inner := entry["hooks"].([]interface{})[0].(map[string]interface{})
		if !strings.Contains(inner["command"].(string), "agents permission-hook activity --source claude_code") {
			t.Fatalf("%s command = %v", want.event, inner["command"])
		}
		if timeout, _ := inner["timeout"].(float64); int(timeout) != activityHookTimeoutSeconds {
			t.Fatalf("%s timeout = %v", want.event, inner["timeout"])
		}
	}
	if err := removeApprovalHooks(agent, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	if remaining, ok := readJSONDoc(t, path)["hooks"].(map[string]interface{}); ok {
		for _, event := range activityHookEventNames() {
			if _, present := remaining[event]; present {
				t.Fatalf("%s left behind: %v", event, remaining)
			}
		}
	}
}

func TestActivityHookBuildsToolEndAndIdleNotification(t *testing.T) {
	raw := []byte(`{"hook_event_name":"PostToolUse","session_id":"ext-1","tool_name":"Bash","tool_use_id":"toolu_1","tool_response":{"stdout":"` + strings.Repeat("a", 900) + `","exit_code":2}}`)
	req, ok := buildSessionEventRequest("claude_code", raw)
	if !ok || req.ToolName != "Bash" || req.ToolUseID != "toolu_1" {
		t.Fatalf("unexpected %+v %v", req, ok)
	}
	if req.ExitCode == nil || *req.ExitCode != 2 || len(req.OutputPreview) != activityOutputPreviewRunes {
		t.Fatalf("exit/preview wrong: %+v", req)
	}
	fail, ok := buildSessionEventRequest("claude_code", []byte(`{"hook_event_name":"PostToolUseFailure","session_id":"ext-1","tool_name":"Read","error":"no such file"}`))
	if !ok || fail.IsError == nil || !*fail.IsError || fail.OutputPreview != "no such file" {
		t.Fatalf("failure wrong: %+v", fail)
	}
	idle, ok := buildSessionEventRequest("claude_code", []byte(`{"hook_event_name":"Notification","session_id":"ext-1","message":"Claude is waiting for your input"}`))
	if !ok || idle.NotificationType != "idle_prompt" {
		t.Fatalf("idle wrong: %+v", idle)
	}
	if _, ok := buildSessionEventRequest("claude_code", []byte(`{"hook_event_name":"Notification","session_id":"ext-1","message":"Claude needs your permission"}`)); ok {
		t.Fatal("a permission notification is not a wait for input")
	}
	if _, ok := buildSessionEventRequest("claude_code", []byte(`{"hook_event_name":"PostToolUse","session_id":"../x"}`)); ok {
		t.Fatal("path-like session id accepted")
	}
}

func TestActivityHookPostsAndFailsOpen(t *testing.T) {
	var got map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != agentSessionEventPath || r.Header.Get("Authorization") != "Bearer agt_x" {
			t.Errorf("unexpected request %s", r.URL.Path)
		}
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	req, _ := buildSessionEventRequest("claude_code", []byte(`{"hook_event_name":"PostToolUse","session_id":"ext-1","tool_name":"Bash"}`))
	if err := postSessionEvent(permissionHookCredential{BaseURL: server.URL, Token: "agt_x"}, req); err != nil {
		t.Fatal(err)
	}
	if got["hook_event_name"] != "PostToolUse" || got["tool_name"] != "Bash" {
		t.Fatalf("body = %v", got)
	}
	if err := postSessionEvent(permissionHookCredential{BaseURL: "http://127.0.0.1:1", Token: "agt_x"}, req); err == nil {
		t.Fatal("expected an error from an unreachable server")
	}
}
