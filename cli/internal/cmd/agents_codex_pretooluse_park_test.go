package cmd

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// An unattended flow run paused on an approval-required command gets a deny
// (the command must not run) that tells the terminal the run is paused.
func TestCodexPreToolUseParkedRunDeniesAndSaysPaused(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"decision":"deny","reason":"paused","request_id":"5a5c1b52-0000-4000-8000-000000000001","parked":true}`))
	}))
	defer server.Close()
	writeTestPermissionCredential(t, home, "agent", permissionHookCredential{BaseURL: server.URL, Token: "agt_synthetic", Source: permissionSourceCodexCLI})
	out := invokeCodexHook(t, "PreToolUse", `{"tool_name":"Bash","tool_input":{"command":"git push"}}`, true)
	spec, ok := out["hookSpecificOutput"].(map[string]interface{})
	if !ok || spec["permissionDecision"] != "deny" {
		t.Fatalf("parked call must be denied: %v", out)
	}
	message, _ := out["systemMessage"].(string)
	if !strings.Contains(message, "Paused in Preloop") || !strings.Contains(message, "5a5c1b52-0000-4000-8000-000000000001") {
		t.Errorf("systemMessage=%q", message)
	}
}

// A parked allow is a contradiction and must not widen anything.
func TestCodexPreToolUseParkedAllowIsRejected(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]interface{}{"decision": "allow", "parked": true})
	}))
	defer server.Close()
	writeTestPermissionCredential(t, home, "agent", permissionHookCredential{BaseURL: server.URL, Token: "agt_synthetic", Source: permissionSourceCodexCLI})
	out := invokeCodexHook(t, "PreToolUse", `{"tool_name":"Bash","tool_input":{"command":"ls"}}`, true)
	spec, ok := out["hookSpecificOutput"].(map[string]interface{})
	if !ok || spec["permissionDecision"] != "deny" {
		t.Fatalf("parked allow widened: %v", out)
	}
}
