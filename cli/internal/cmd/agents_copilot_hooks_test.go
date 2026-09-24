package cmd

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func copilotTestAgent(home string) AgentConfig {
	return AgentConfig{
		Name:       "Copilot CLI",
		ConfigPath: filepath.Join(home, ".copilot", "mcp.json"),
	}
}

func TestPermissionSourceForAgentCopilotCLI(t *testing.T) {
	for _, name := range []string{"Copilot CLI", "copilot cli", "COPILOT CLI"} {
		got := permissionSourceForAgent(AgentConfig{Name: name})
		if got != permissionSourceCopilotCLI {
			t.Errorf("permissionSourceForAgent(%q) = %q, want %q", name, got, permissionSourceCopilotCLI)
		}
	}
}

func TestInstallCopilotHooksWritesPreloopJSON(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	t.Setenv("COPILOT_HOME", "")
	agent := copilotTestAgent(home)
	hooksPath := filepath.Join(home, ".copilot", "hooks", "preloop.json")

	if err := installCopilotUsageHooks(agent, nil); err != nil {
		t.Fatalf("install usage: %v", err)
	}
	if err := installApprovalHooks(agent, "https://preloop.ai", "agt_copilot", nil); err != nil {
		t.Fatalf("install approvals: %v", err)
	}
	// Idempotent re-onboard.
	if err := installCopilotUsageHooks(agent, nil); err != nil {
		t.Fatalf("reinstall usage: %v", err)
	}
	if err := installApprovalHooks(agent, "https://preloop.ai", "agt_copilot", nil); err != nil {
		t.Fatalf("reinstall approvals: %v", err)
	}

	doc := readJSONDoc(t, hooksPath)
	if v, ok := doc["version"]; !ok || (v != float64(1) && v != 1) {
		t.Errorf("hooks version missing/wrong: %v", doc["version"])
	}
	hooks, _ := doc["hooks"].(map[string]interface{})

	preTool := flatHookEntries(t, doc, "preToolUse")
	if len(preTool) != 1 {
		t.Fatalf("expected one preToolUse entry, got %d", len(preTool))
	}
	if preTool[0]["type"] != "command" {
		t.Errorf("preToolUse type=%v, want command", preTool[0]["type"])
	}
	bash, _ := preTool[0]["bash"].(string)
	if !strings.Contains(bash, "permission-hook --source copilot_cli") || !filepath.IsAbs(strings.Fields(bash)[0]) {
		t.Errorf("preToolUse bash wrong: %q", bash)
	}
	if _, hasPS := preTool[0]["powershell"]; hasPS {
		t.Errorf("powershell should be omitted when existing writers do not set it")
	}
	timeout, _ := preTool[0]["timeoutSec"].(float64)
	if timeout < float64(approvalHookProcessHeadroomSeconds) {
		t.Errorf("preToolUse timeoutSec=%v, want host budget with headroom", timeout)
	}

	for _, key := range copilotUsageHookEvents {
		entries := flatHookEntries(t, doc, key)
		if len(entries) != 1 {
			t.Fatalf("expected one %s entry, got %d", key, len(entries))
		}
		command, _ := entries[0]["bash"].(string)
		if !strings.HasSuffix(command, " usage hook --from copilot") {
			t.Errorf("%s bash wrong: %q", key, command)
		}
		if entries[0]["timeoutSec"] != float64(cursorUsageHookTimeoutSeconds) {
			t.Errorf("%s timeoutSec=%v, want %d", key, entries[0]["timeoutSec"], cursorUsageHookTimeoutSeconds)
		}
	}
	if len(hooks) != 1+len(copilotUsageHookEvents) {
		t.Errorf("unexpected hook keys: %v", hooks)
	}

	// Sibling hook files must be left alone.
	sibling := filepath.Join(home, ".copilot", "hooks", "other.json")
	if err := os.WriteFile(sibling, []byte(`{"version":1,"hooks":{}}`), 0600); err != nil {
		t.Fatal(err)
	}
	if err := installCopilotUsageHooks(agent, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(sibling); err != nil {
		t.Errorf("sibling hook file was touched: %v", err)
	}
}

func TestInstallCopilotUsageHooksHonorsCOPILOT_HOME(t *testing.T) {
	_ = testenv.SetHome(t, t.TempDir())
	copilotHome := t.TempDir()
	t.Setenv("COPILOT_HOME", copilotHome)
	agent := AgentConfig{Name: "Copilot CLI", ConfigPath: filepath.Join(copilotHome, "mcp.json")}

	if err := installCopilotUsageHooks(agent, nil); err != nil {
		t.Fatalf("install: %v", err)
	}
	path := filepath.Join(copilotHome, "hooks", "preloop.json")
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("expected hooks at %s: %v", path, err)
	}
}

func TestRemoveCopilotHooksDeletesOnlyPreloopFile(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	t.Setenv("COPILOT_HOME", "")
	agent := copilotTestAgent(home)
	hooksDir := filepath.Join(home, ".copilot", "hooks")
	if err := os.MkdirAll(hooksDir, 0700); err != nil {
		t.Fatal(err)
	}
	sibling := filepath.Join(hooksDir, "other.json")
	if err := os.WriteFile(sibling, []byte(`{"version":1}`), 0600); err != nil {
		t.Fatal(err)
	}

	if err := installCopilotUsageHooks(agent, nil); err != nil {
		t.Fatal(err)
	}
	if err := installApprovalHooks(agent, "https://preloop.ai", "agt_c", nil); err != nil {
		t.Fatal(err)
	}
	if err := removeApprovalHooks(agent, nil); err != nil {
		t.Fatalf("remove: %v", err)
	}
	if _, err := os.Stat(filepath.Join(hooksDir, "preloop.json")); !os.IsNotExist(err) {
		t.Errorf("preloop.json should be gone, err=%v", err)
	}
	if _, err := os.Stat(sibling); err != nil {
		t.Errorf("sibling must survive offboard: %v", err)
	}
}

func TestRenderHookDecisionCopilotAllowDeny(t *testing.T) {
	allow := renderHookDecision(permissionSourceCopilotCLI, hookDecision{Behavior: "allow"})
	if allow["permissionDecision"] != "allow" {
		t.Fatalf("allow payload: %#v", allow)
	}
	if _, hasReason := allow["permissionDecisionReason"]; hasReason {
		t.Fatalf("allow must not carry a reason: %#v", allow)
	}
	// Distinct from Cursor's {"permission": ...} schema.
	if _, hasPermission := allow["permission"]; hasPermission {
		t.Fatalf("must not reuse Cursor schema: %#v", allow)
	}

	deny := renderHookDecision(permissionSourceCopilotCLI, hookDecision{
		Behavior: "deny",
		Reason:   "blocked by policy",
	})
	if deny["permissionDecision"] != "deny" {
		t.Fatalf("deny payload: %#v", deny)
	}
	if deny["permissionDecisionReason"] != "blocked by policy" {
		t.Fatalf("deny reason: %#v", deny)
	}
}

func TestBuildPermissionRequestCopilotCamelCaseAndPascalCase(t *testing.T) {
	camel := []byte(`{
		"sessionId": "sess-1",
		"cwd": "/tmp/proj",
		"toolName": "bash",
		"toolArgs": "{\"command\":\"ls\"}"
	}`)
	req, err := buildPermissionRequest(permissionSourceCopilotCLI, camel, permissionHookCredential{})
	if err != nil {
		t.Fatal(err)
	}
	if req.Source != permissionSourceCopilotCLI || req.ToolName != "bash" || req.SessionID != "sess-1" {
		t.Fatalf("camel request: %#v", req)
	}
	if req.ToolInput["command"] != "ls" {
		t.Fatalf("toolArgs JSON string not coerced: %#v", req.ToolInput)
	}

	pascal := []byte(`{
		"hook_event_name": "PreToolUse",
		"session_id": "sess-2",
		"tool_name": "Bash",
		"tool_input": {"command": "pwd"}
	}`)
	req, err = buildPermissionRequest(permissionSourceCopilotCLI, pascal, permissionHookCredential{})
	if err != nil {
		t.Fatal(err)
	}
	if req.ToolName != "Bash" || req.SessionID != "sess-2" || req.ToolInput["command"] != "pwd" {
		t.Fatalf("pascal request: %#v", req)
	}
}

func TestCopilotUsageHooksDoNotRequireApprovals(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	t.Setenv("COPILOT_HOME", "")
	agent := copilotTestAgent(home)

	if err := installCopilotUsageHooks(agent, nil); err != nil {
		t.Fatal(err)
	}
	doc := readJSONDoc(t, filepath.Join(home, ".copilot", "hooks", "preloop.json"))
	hooks, _ := doc["hooks"].(map[string]interface{})
	if _, has := hooks["preToolUse"]; has {
		t.Fatalf("usage-only install must not add preToolUse: %#v", hooks)
	}
	for _, key := range copilotUsageHookEvents {
		if _, ok := hooks[key]; !ok {
			t.Errorf("missing usage event %s", key)
		}
	}
}

func TestNormalizePermissionSourceCopilot(t *testing.T) {
	for _, input := range []string{"copilot_cli", "Copilot CLI", "copilot"} {
		if got := normalizePermissionSource(input); got != permissionSourceCopilotCLI {
			t.Errorf("normalizePermissionSource(%q) = %q", input, got)
		}
	}
}

func TestRemoveCopilotHooksDeletesPreloopFile(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	t.Setenv("COPILOT_HOME", "")
	agent := copilotTestAgent(home)

	if err := installApprovalHooks(agent, "https://preloop.ai", "agt_c", nil); err != nil {
		t.Fatal(err)
	}
	if err := installCopilotUsageHooks(agent, nil); err != nil {
		t.Fatal(err)
	}
	if err := removeApprovalHooks(agent, nil); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(home, ".copilot", "hooks", "preloop.json")
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("preloop.json should be removed, stat err=%v", err)
	}
}
