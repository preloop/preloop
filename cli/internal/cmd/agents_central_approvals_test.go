package cmd

import (
	"encoding/json"
	"github.com/preloop/preloop/cli/internal/testenv"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestCentralApprovalLocallyAllowedCalls(t *testing.T) {
	for _, source := range []string{permissionSourceClaudeCode, permissionSourceCursor} {
		for _, tc := range []struct {
			name, decision, reason string
			timedOut, failOpen     bool
		}{
			{name: "native rule deny", decision: "deny", reason: "Denied by native rule"},
			{name: "approval declined", decision: "deny", reason: "Human declined"},
			{name: "approval approved", decision: "allow", reason: "Human approved"},
			{name: "no matching rule", decision: "allow", reason: "Client policy allows"},
			{name: "governance off no deny rule", decision: "allow", reason: "Native approvals off"},
			{name: "governance off deny rule", decision: "deny", reason: "Denied by native rule"},
			{name: "deny with fail open", decision: "deny", reason: "Denied by native rule", failOpen: true},
			{name: "expiry with fail open", decision: "deny", reason: "Approval expired", timedOut: true, failOpen: true},
		} {
			t.Run(source+"/"+tc.name, func(t *testing.T) {
				home := t.TempDir()
				testenv.SetHome(t, home)
				overrideManagedSettingsPath(t, filepath.Join(home, "absent.json"))
				if err := os.MkdirAll(filepath.Join(home, ".claude"), 0700); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(filepath.Join(home, ".claude", "settings.json"), []byte(`{"permissions":{"allow":["Bash(ls)"]}}`), 0600); err != nil {
					t.Fatal(err)
				}
				var posted []permissionCheckRequest
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					var req permissionCheckRequest
					if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
						t.Error(err)
					}
					posted = append(posted, req)
					_ = json.NewEncoder(w).Encode(permissionCheckResponse{Decision: tc.decision, Reason: tc.reason, TimedOut: tc.timedOut})
				}))
				defer server.Close()
				writeTestPermissionCredential(t, home, "agent", permissionHookCredential{BaseURL: server.URL, Token: "agt_synthetic", Source: source})
				raw := []byte(`{"tool_name":"Bash","tool_input":{"command":"ls"}}`)
				if source == permissionSourceCursor {
					raw = []byte(`{"hook_event_name":"beforeShellExecution","command":"ls","cwd":"/repo"}`)
				}
				got := resolvePermissionDecision(source, raw, tc.failOpen)
				if got.Behavior != tc.decision || got.Reason != tc.reason {
					t.Errorf("decision=%+v, want %s/%s", got, tc.decision, tc.reason)
				}
				if len(posted) != 1 {
					t.Fatalf("POST count=%d, want 1", len(posted))
				}
				if posted[0].ClientDecision != "allow" {
					t.Errorf("client_decision=%q, want allow", posted[0].ClientDecision)
				}
			})
		}
	}
}

func TestCentralApprovalClientDenyNeverWidened(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	overrideManagedSettingsPath(t, filepath.Join(home, "absent.json"))
	if err := os.MkdirAll(filepath.Join(home, ".claude"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(home, ".claude", "settings.json"), []byte(`{"permissions":{"deny":["Bash(ls)"]}}`), 0600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("client deny must not POST")
		_ = json.NewEncoder(w).Encode(permissionCheckResponse{Decision: "allow"})
	}))
	defer server.Close()
	for _, credential := range []bool{false, true} {
		if credential {
			writeTestPermissionCredential(t, home, "agent", permissionHookCredential{BaseURL: server.URL, Token: "agt_synthetic", Source: permissionSourceClaudeCode})
		}
		for _, failOpen := range []bool{false, true} {
			if got := resolvePermissionDecision(permissionSourceClaudeCode, []byte(`{"tool_name":"Bash","tool_input":{"command":"ls"}}`), failOpen); got.Behavior != "deny" {
				t.Errorf("credential=%t failOpen=%t: %+v", credential, failOpen, got)
			}
		}
	}
}

func TestCentralApprovalUnavailable(t *testing.T) {
	for _, source := range []string{permissionSourceClaudeCode, permissionSourceCursor, permissionSourceCodexCLI} {
		for _, status := range []int{http.StatusBadRequest, http.StatusUnauthorized, http.StatusForbidden, http.StatusTooManyRequests, http.StatusServiceUnavailable, http.StatusOK} {
			t.Run(source+"/"+http.StatusText(status), func(t *testing.T) {
				home := t.TempDir()
				testenv.SetHome(t, home)
				overrideManagedSettingsPath(t, filepath.Join(home, "absent.json"))
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					w.WriteHeader(status)
					_, _ = w.Write([]byte("invalid JSON"))
				}))
				defer server.Close()
				writeTestPermissionCredential(t, home, "agent", permissionHookCredential{BaseURL: server.URL, Token: "agt_synthetic", Source: source})
				raw := []byte(`{"tool_name":"Bash","tool_input":{"command":"ls"}}`)
				if source == permissionSourceCursor {
					raw = []byte(`{"command":"ls"}`)
				}
				for _, failOpen := range []bool{false, true} {
					want := "deny"
					if failOpen && status >= 500 {
						want = "allow"
					}
					if got := resolvePermissionDecision(source, raw, failOpen); got.Behavior != want {
						t.Errorf("failOpen=%t: %+v, want %s", failOpen, got, want)
					}
				}
				server.Close()
				if got := resolvePermissionDecision(source, raw, false); got.Behavior != "deny" {
					t.Errorf("transport failure: %+v", got)
				}
			})
		}
	}
}

func TestPermissionCheckHTTPTimeout(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-time.After(100 * time.Millisecond):
		}
	}))
	defer server.Close()
	_, err := callPermissionCheck(server.URL, "agt_synthetic", permissionCheckRequest{ToolName: "Shell"}, 10*time.Millisecond)
	if err == nil {
		t.Fatal("expected transport timeout")
	}
}

func TestApprovalHookWaitBudgets(t *testing.T) {
	for _, tc := range []struct {
		seconds int
		want    time.Duration
	}{
		{0, 86415 * time.Second}, {-1, 86415 * time.Second}, {86401, 86415 * time.Second},
		{30, 45 * time.Second}, {600, 615 * time.Second}, {86400, 86415 * time.Second},
	} {
		if got := permissionCheckTimeoutFor(permissionHookCredential{TimeoutSeconds: tc.seconds}); got != tc.want {
			t.Errorf("seconds=%d: got %s want %s", tc.seconds, got, tc.want)
		}
	}
}

func TestInstallApprovalHookDeadlines(t *testing.T) {
	for _, tc := range []struct{ name, source, event string }{
		{"Claude Code", permissionSourceClaudeCode, "PreToolUse"},
		{"Codex CLI", permissionSourceCodexCLI, "PermissionRequest"},
		{"Cursor", permissionSourceCursor, "preToolUse"},
	} {
		t.Run(tc.source, func(t *testing.T) {
			home := t.TempDir()
			testenv.SetHome(t, home)
			agent := AgentConfig{Name: tc.name, ConfigPath: filepath.Join(home, "config.json")}
			if err := installApprovalHooks(agent, "http://127.0.0.1:1", "agt_synthetic", nil); err != nil {
				t.Fatal(err)
			}
			path, err := approvalHookConfigPath(tc.source)
			if err != nil {
				t.Fatal(err)
			}
			doc := readJSONDoc(t, path)
			hooks := doc["hooks"].(map[string]interface{})[tc.event].([]interface{})
			entry := hooks[0].(map[string]interface{})
			if tc.source != permissionSourceCursor {
				entry = entry["hooks"].([]interface{})[0].(map[string]interface{})
			}
			if entry["timeout"] != float64(86430) {
				t.Errorf("host timeout=%v want86430", entry["timeout"])
			}
			credPath, err := permissionHookCredentialPath(agent)
			if err != nil {
				t.Fatal(err)
			}
			cred := readJSONDoc(t, credPath)
			if cred["timeout_seconds"] != float64(86400) {
				t.Errorf("credential wait=%v want86400", cred["timeout_seconds"])
			}
		})
	}
}
