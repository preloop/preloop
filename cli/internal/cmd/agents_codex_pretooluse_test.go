package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
	"github.com/spf13/cobra"
)

func invokeCodexHook(t *testing.T, event, raw string, failOpen bool) map[string]interface{} {
	t.Helper()
	cmd := &cobra.Command{}
	cmd.Flags().String("source", permissionSourceCodexCLI, "")
	cmd.Flags().String("hook-event", event, "")
	cmd.Flags().Bool("fail-open", failOpen, "")
	cmd.SetIn(bytes.NewBufferString(raw))
	var out bytes.Buffer
	cmd.SetOut(&out)
	if err := runAgentsPermissionHook(cmd, nil); err != nil {
		t.Fatal(err)
	}
	var result map[string]interface{}
	if err := json.Unmarshal(out.Bytes(), &result); err != nil {
		t.Fatalf("invalid JSON %q: %v", out.String(), err)
	}
	return result
}

func TestCodexPreToolUseCentralDecision(t *testing.T) {
	for _, tc := range []struct {
		name, decision string
		timedOut       bool
	}{
		{"no rule", "allow", false}, {"rule approved", "allow", false},
		{"rule deny", "deny", false}, {"approval declined", "deny", false}, {"expired", "deny", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			home := t.TempDir()
			testenv.SetHome(t, home)
			var posted []map[string]interface{}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var req map[string]interface{}
				_ = json.NewDecoder(r.Body).Decode(&req)
				posted = append(posted, req)
				_ = json.NewEncoder(w).Encode(permissionCheckResponse{Decision: tc.decision, Reason: tc.name, TimedOut: tc.timedOut})
			}))
			defer server.Close()
			writeTestPermissionCredential(t, home, "agent", permissionHookCredential{BaseURL: server.URL, Token: "agt_synthetic", Source: permissionSourceCodexCLI})
			out := invokeCodexHook(t, "PreToolUse", `{"hook_event_name":"PreToolUse","session_id":"session-a","turn_id":"turn-a","tool_use_id":"call-a","tool_name":"Bash","tool_input":{"command":"ls"}}`, true)
			if len(posted) != 1 {
				t.Fatalf("POSTs=%d want1", len(posted))
			}
			if posted[0]["evaluation_phase"] != "pre_tool_use" {
				t.Errorf("phase=%v", posted[0]["evaluation_phase"])
			}
			if _, ok := posted[0]["client_decision"]; ok {
				t.Errorf("must not claim host decision: %v", posted[0])
			}
			if tc.decision == "allow" {
				if len(out) != 0 {
					t.Errorf("central allow must be neutral to host: %v", out)
				}
			} else {
				spec, ok := out["hookSpecificOutput"].(map[string]interface{})
				if !ok || spec["hookEventName"] != "PreToolUse" || spec["permissionDecision"] != "deny" {
					t.Errorf("wrong deny envelope: %v", out)
				}
			}
		})
	}
}

func TestCodexHooksNeverReuseApprovalAcrossEvents(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	var posted []map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req map[string]interface{}
		_ = json.NewDecoder(r.Body).Decode(&req)
		posted = append(posted, req)
		decision := "allow"
		if req["evaluation_phase"] != "pre_tool_use" {
			decision = "deny"
		}
		_ = json.NewEncoder(w).Encode(permissionCheckResponse{Decision: decision, Reason: "independent gate", RequestID: "synthetic-request"})
	}))
	defer server.Close()
	writeTestPermissionCredential(t, home, "agent", permissionHookCredential{BaseURL: server.URL, Token: "agt_synthetic", Source: permissionSourceCodexCLI})
	// Identical arguments in the same session/turn are not a correlation key.
	for _, event := range []string{"PreToolUse", "PermissionRequest", "PermissionRequest", "PreToolUse"} {
		raw := `{"hook_event_name":"` + event + `","session_id":"session-a","turn_id":"turn-a","tool_name":"Bash","tool_input":{"command":"ls"}}`
		out := invokeCodexHook(t, event, raw, false)
		if event == "PreToolUse" {
			if len(out) != 0 {
				t.Errorf("expected neutral: %v", out)
			}
		} else {
			spec := out["hookSpecificOutput"].(map[string]interface{})
			decision := spec["decision"].(map[string]interface{})
			if spec["hookEventName"] != "PermissionRequest" || decision["behavior"] != "deny" {
				t.Errorf("PermissionRequest reused earlier allow: %v", out)
			}
		}
	}
	if len(posted) != 4 {
		t.Errorf("requests=%d, want independent evaluation for all4", len(posted))
	}
}

func TestCodexPreToolUseMalformedEventDenies(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	for _, raw := range []string{"not JSON", `{}`, `{"hook_event_name":"PermissionRequest","tool_name":"Bash"}`} {
		out := invokeCodexHook(t, "PreToolUse", raw, true)
		spec, ok := out["hookSpecificOutput"].(map[string]interface{})
		if !ok || spec["hookEventName"] != "PreToolUse" || spec["permissionDecision"] != "deny" {
			t.Errorf("invalid event must fail closed in installed envelope: %v", out)
		}
	}
}

func TestCodexInstallBothHooksAndOffboard(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	agent := AgentConfig{Name: "Codex CLI", ConfigPath: filepath.Join(home, ".codex", "config.toml")}
	path, err := approvalHookConfigPath(permissionSourceCodexCLI)
	if err != nil {
		t.Fatal(err)
	}
	if err := upsertNestedCommandHook(path, "PermissionRequest", "*", "preloop agents permission-hook --source codex_cli", 1800); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if err := installApprovalHooks(agent, "http://127.0.0.1:1", "agt_synthetic", nil); err != nil {
			t.Fatal(err)
		}
	}
	doc := readJSONDoc(t, path)
	for _, event := range []string{"PreToolUse", "PermissionRequest"} {
		cmds := nestedHookCommands(t, doc, event)
		if countSubstring(cmds, "permission-hook") != 1 {
			t.Errorf("%s hooks=%v", event, cmds)
		}
	}
	if !containsSubstring(nestedHookCommands(t, doc, "PreToolUse"), "--hook-event PreToolUse") {
		t.Error("PreToolUse must pin failure response envelope")
	}
	if err := removeApprovalHooks(agent, nil); err != nil {
		t.Fatal(err)
	}
}

func TestApprovalReonboardPreservesShorterBudget(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	agent := AgentConfig{Name: "Codex CLI", ConfigPath: filepath.Join(home, ".codex", "config.toml")}
	writeTestPermissionCredential(t, home, runtimePrincipalIDForAgent(agent), permissionHookCredential{Source: permissionSourceCodexCLI, Token: "old", TimeoutSeconds: 600})
	if err := installApprovalHooks(agent, "http://127.0.0.1:1", "new", nil); err != nil {
		t.Fatal(err)
	}
	path, _ := permissionHookCredentialPath(agent)
	cred := readJSONDoc(t, path)
	if cred["timeout_seconds"] != float64(600) || cred["token"] != "new" {
		t.Errorf("credential not preserved/refreshed: %v", cred)
	}
	path, _ = approvalHookConfigPath(permissionSourceCodexCLI)
	doc := readJSONDoc(t, path)
	for _, event := range []string{"PreToolUse", "PermissionRequest"} {
		hook := doc["hooks"].(map[string]interface{})[event].([]interface{})[0].(map[string]interface{})["hooks"].([]interface{})[0].(map[string]interface{})
		if hook["timeout"] != float64(630) {
			t.Errorf("%s deadline=%v", event, hook["timeout"])
		}
	}
}

func TestRuntimeReonboardPreservesApprovalControls(t *testing.T) {
	for _, name := range []string{"OpenClaw", "Hermes"} {
		t.Run(name, func(t *testing.T) {
			agent := AgentConfig{Name: name, ConfigPath: "/tmp/synthetic/config.json"}
			old := map[string]interface{}{"enabled": false, "bearer_token": "old", "control_ws_url": "wss://old.example/control", "permission_check_url": "https://old.example/permission"}
			if name == "OpenClaw" {
				old["tool_approval_enabled"] = false
				old["tool_approval_fail_open"] = true
				old["tool_approval_timeout_seconds"] = float64(600)
			} else {
				old["tool_approval"] = map[string]interface{}{"enabled": false, "fail_open": true, "timeout_seconds": float64(600), "permission_check_url": "https://old.example/permission"}
			}
			doc := map[string]interface{}{}
			if name == "OpenClaw" {
				ensureObjectPath(doc, "plugins", "entries", openClawPreloopPluginID)["config"] = old
			} else {
				ensureObjectPath(doc, "preloop")["control"] = old
			}
			fresh := buildManagedAgentControlConfig(agent, "https://new.example", "new", nil, nil, nil)
			applyAgentControlConfigToDocument(agent, doc, fresh)
			var got map[string]interface{}
			if name == "OpenClaw" {
				got = ensureObjectPath(doc, "plugins", "entries", openClawPreloopPluginID, "config")
			} else {
				got = ensureObjectPath(doc, "preloop", "control")
			}
			if got["enabled"] != false || got["bearer_token"] != "new" || got["control_ws_url"] == old["control_ws_url"] {
				t.Errorf("settings lost or stale identity: %v", got)
			}
			if _, ok := got["permission_check_url"]; ok {
				t.Error("stale endpoint preserved")
			}
			if name == "OpenClaw" {
				if got["tool_approval_enabled"] != false || got["tool_approval_fail_open"] != true || got["tool_approval_timeout_seconds"] != float64(600) {
					t.Errorf("approval settings lost: %v", got)
				}
			} else {
				approval, ok := got["tool_approval"].(map[string]interface{})
				if !ok || approval["enabled"] != false || approval["fail_open"] != true || approval["timeout_seconds"] != float64(600) {
					t.Errorf("approval settings lost: %v", approval)
				}
				if _, ok := approval["permission_check_url"]; ok {
					t.Error("stale nested endpoint preserved")
				}
			}
		})
	}
}

func TestCodexPreToolUseFailureClasses(t *testing.T) {
	for _, tc := range []struct {
		name        string
		status      int
		body        string
		wantNeutral bool
	}{
		{"unavailable", 503, `unavailable`, true},
		{"unauthorized", 401, `unauthorized`, false}, {"forbidden", 403, `forbidden`, false},
		{"validation", 422, `invalid`, false}, {"rate limited", 429, `limited`, false},
		{"invalid JSON", 200, `invalid`, false}, {"unknown decision", 200, `{"decision":"ask"}`, false},
		{"expired allow", 200, `{"decision":"allow","timed_out":true}`, false},
		{"null reason", 200, `{"decision":"allow","reason":null}`, false},
		{"null timed out", 200, `{"decision":"allow","timed_out":null}`, false},
		{"noncanonical decision", 200, `{"decision":" ALLOW "}`, false},
		{"invalid operator note", 200, `{"decision":"allow","operator_note":42}`, false},
		{"missing canonical decision", 200, `{"Decision":"allow"}`, false},
		{"null response", 200, `null`, false}, {"empty response", 200, `{}`, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			home := t.TempDir()
			testenv.SetHome(t, home)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.body))
			}))
			defer server.Close()
			writeTestPermissionCredential(t, home, "agent", permissionHookCredential{BaseURL: server.URL, Token: "agt_synthetic", Source: permissionSourceCodexCLI})
			out := invokeCodexHook(t, "PreToolUse", `{"tool_name":"Bash","tool_input":{"command":"ls"}}`, true)
			if tc.wantNeutral {
				if len(out) != 0 {
					t.Errorf("fail-open availability should be neutral: %v", out)
				}
			} else {
				spec, ok := out["hookSpecificOutput"].(map[string]interface{})
				if !ok || spec["permissionDecision"] != "deny" {
					t.Errorf("failure widened: %v", out)
				}
			}
		})
	}
}

func TestPermissionCheckAuthWithBrokenBodyStaysClosed(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "100")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte("denied"))
	}))
	defer server.Close()
	_, err := callPermissionCheck(server.URL, "agt_synthetic", permissionCheckRequest{ToolName: "Bash"}, 0)
	if _, ok := err.(*permissionCheckUnavailableError); ok || err == nil {
		t.Fatalf("HTTP403 must stay a denial despite truncated body: %v", err)
	}
}

func TestRuntimeReonboardRejectsMalformedApprovalSettings(t *testing.T) {
	for _, bad := range []interface{}{true, "600", float64(29), float64(86401), float64(30.5)} {
		old := map[string]interface{}{"tool_approval_timeout_seconds": bad, "tool_approval_enabled": "false", "tool_approval_fail_open": "false"}
		got := preserveRuntimeApprovalConfig("openclaw", old, map[string]interface{}{"bearer_token": "fresh"})
		if _, ok := got["tool_approval_timeout_seconds"]; ok {
			t.Errorf("invalid timeout preserved: %v", bad)
		}
		if _, ok := got["tool_approval_enabled"]; ok {
			t.Error("nonboolean enabled preserved")
		}
		if _, ok := got["tool_approval_fail_open"]; ok {
			t.Error("nonboolean fail-open preserved")
		}
	}
	old := map[string]interface{}{"tool_approval": map[string]interface{}{"enabled": "false", "fail_open": 1, "timeout_seconds": true}}
	got := preserveRuntimeApprovalConfig("hermes", old, map[string]interface{}{"bearer_token": "fresh"})
	if _, ok := got["tool_approval"]; ok {
		t.Errorf("malformed Hermes approval block preserved: %v", got)
	}
}

func TestPermissionResponseNullableAndUnknownFields(t *testing.T) {
	for _, tc := range []struct{ body, decision string }{
		{`{"decision":"allow","request_id":null,"operator_note":null,"future_field":true}`, "allow"},
		{`{"decision":"deny","Decision":"allow","reason":"blocked","timed_out":false}`, "deny"},
	} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(tc.body)) }))
		result, err := callPermissionCheck(server.URL, "agt_synthetic", permissionCheckRequest{ToolName: "Bash"}, 0)
		server.Close()
		if err != nil || result.Decision != tc.decision {
			t.Errorf("body=%s result=%+v err=%v", tc.body, result, err)
		}
	}
}
