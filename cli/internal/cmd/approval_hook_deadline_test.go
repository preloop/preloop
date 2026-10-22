package cmd

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestApprovalHookDeadlineBelowHostTimeout(t *testing.T) {
	sources := []string{
		permissionSourceClaudeCode, permissionSourceCodexCLI, permissionSourceCursor,
		permissionSourceCopilotCLI, permissionSourceDevinCLI,
	}
	for _, source := range sources {
		for _, configured := range []int{0, 30, 300, 540, 600, 1800, 86400, 90000} {
			deadline := permissionCheckTimeoutFor(permissionHookCredential{Source: source, TimeoutSeconds: configured})
			host := time.Duration(approvalHookHostTimeoutSeconds(source, configured)) * time.Second
			if deadline >= host {
				t.Errorf("%s/%d: hook deadline %s not below host timeout %s", source, configured, deadline, host)
			}
			if source == permissionSourceClaudeCode && host > claudeCodeHookHostTimeoutCapSeconds*time.Second {
				t.Errorf("%s/%d: host timeout %s above Claude Code's 600 s default", source, configured, host)
			}
		}
	}
}

// deadlineServer holds permission-check past the hook's deadline and records
// the expire call.
func deadlineServer(t *testing.T, expireStatus string) (*httptest.Server, *string, *string, *sync.Mutex) {
	t.Helper()
	var mu sync.Mutex
	var proposedID, expiredPath string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == permissionCheckPath:
			var body permissionCheckRequest
			_ = json.NewDecoder(r.Body).Decode(&body)
			mu.Lock()
			proposedID = body.ApprovalRequestID
			mu.Unlock()
			select {
			case <-r.Context().Done():
			case <-time.After(2 * time.Second):
			}
		case strings.HasSuffix(r.URL.Path, "/expire"):
			mu.Lock()
			expiredPath = r.URL.Path
			mu.Unlock()
			_ = json.NewEncoder(w).Encode(map[string]string{"approval_request_id": "x", "status": expireStatus})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	return server, &proposedID, &expiredPath, &mu
}

func shortHookDeadline(t *testing.T) {
	t.Helper()
	previous := permissionHookDeadline
	permissionHookDeadline = func(permissionHookCredential) time.Duration { return 150 * time.Millisecond }
	t.Cleanup(func() { permissionHookDeadline = previous })
}

func TestApprovalHookExpiresServerApprovalOnDeadline(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	shortHookDeadline(t)
	server, proposedID, expiredPath, mu := deadlineServer(t, "expired")
	writeTestPermissionCredential(t, home, "codex-agent", permissionHookCredential{
		BaseURL: server.URL, Token: "agt_test", Source: permissionSourceCodexCLI,
	})

	decision := resolvePermissionDecision(permissionSourceCodexCLI, []byte(`{"tool_name":"Bash","tool_input":{"command":"ls"}}`), false)

	mu.Lock()
	defer mu.Unlock()
	if decision.Behavior != "deny" {
		t.Fatalf("decision=%+v, want deny", decision)
	}
	if len(*proposedID) != 36 {
		t.Fatalf("hook did not propose an approval id: %q", *proposedID)
	}
	if *expiredPath != "/api/v1/approvals/"+*proposedID+"/expire" {
		t.Fatalf("expire path=%q, want the proposed id %q", *expiredPath, *proposedID)
	}
}

func TestApprovalHookDenyReasonNamesTimeoutNotTransport(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	shortHookDeadline(t)
	server, proposedID, _, mu := deadlineServer(t, "expired")
	writeTestPermissionCredential(t, home, "codex-agent", permissionHookCredential{
		BaseURL: server.URL, Token: "agt_test", Source: permissionSourceCodexCLI,
	})

	decision := resolvePermissionDecision(permissionSourceCodexCLI, []byte(`{"tool_name":"Bash","tool_input":{"command":"ls"}}`), false)

	mu.Lock()
	id := *proposedID
	mu.Unlock()
	if strings.Contains(decision.Reason, "could not reach") {
		t.Fatalf("deadline reported as a transport failure: %q", decision.Reason)
	}
	for _, want := range []string{"Preloop approval " + id[:8], "was not answered within 1 min", server.URL + "/console/approval/" + id} {
		if !strings.Contains(decision.Reason, want) {
			t.Errorf("reason %q missing %q", decision.Reason, want)
		}
	}

	// A real transport failure still says so.
	server.Close()
	failed := resolvePermissionDecision(permissionSourceCodexCLI, []byte(`{"tool_name":"Bash","tool_input":{"command":"ls"}}`), false)
	if failed.Behavior != "deny" || !strings.Contains(failed.Reason, "could not reach") {
		t.Errorf("transport failure=%+v, want could-not-reach deny", failed)
	}
}

func TestApprovalHookSystemMessageNamesApprover(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	responses := []permissionCheckResponse{
		{Decision: "allow", Reason: "", RequestID: "r1", DecidedBy: "jane"},
		{Decision: "deny", Reason: "not on main", RequestID: "r2", DecidedBy: "jane"},
	}
	var mu sync.Mutex
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		next := responses[0]
		responses = responses[1:]
		mu.Unlock()
		_ = json.NewEncoder(w).Encode(next)
	}))
	defer server.Close()
	writeTestPermissionCredential(t, home, "claude-agent", permissionHookCredential{
		BaseURL: server.URL, Token: "agt_test", Source: permissionSourceClaudeCode,
	})
	raw := []byte(`{"tool_name":"Bash","tool_input":{"command":"ls"}}`)

	approved := resolvePermissionDecision(permissionSourceClaudeCode, raw, false)
	if approved.SystemMessage != "Approved in Preloop by jane" {
		t.Errorf("approved systemMessage=%q", approved.SystemMessage)
	}
	rendered := renderHookDecision(permissionSourceClaudeCode, approved)
	if rendered["systemMessage"] != "Approved in Preloop by jane" {
		t.Errorf("Claude payload missing systemMessage: %v", rendered)
	}

	denied := resolvePermissionDecision(permissionSourceClaudeCode, raw, false)
	if denied.SystemMessage != "Denied in Preloop by jane: not on main" {
		t.Errorf("denied systemMessage=%q", denied.SystemMessage)
	}
	codex := renderHookDecision(permissionSourceCodexCLI, denied)
	if codex["systemMessage"] != "Denied in Preloop by jane: not on main" {
		t.Errorf("Codex payload missing systemMessage: %v", codex)
	}
}

func TestClaudeApprovalHookEntryCarriesStatusMessage(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	agent := AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, "config.json")}
	if err := installApprovalHooks(agent, "http://127.0.0.1:1", "agt_synthetic", nil); err != nil {
		t.Fatal(err)
	}
	path, _ := approvalHookConfigPath(permissionSourceClaudeCode)
	doc := readJSONDoc(t, path)
	entry := doc["hooks"].(map[string]interface{})["PreToolUse"].([]interface{})[0].(map[string]interface{})
	inner := entry["hooks"].([]interface{})[0].(map[string]interface{})
	if inner["statusMessage"] != "Waiting for Preloop approval" {
		t.Errorf("statusMessage=%v", inner["statusMessage"])
	}
}
