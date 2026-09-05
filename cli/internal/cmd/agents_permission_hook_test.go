package cmd

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestNormalizePermissionSource(t *testing.T) {
	cases := map[string]string{
		"claude_code": permissionSourceClaudeCode,
		"Claude Code": permissionSourceClaudeCode,
		"codex_cli":   permissionSourceCodexCLI,
		"Codex CLI":   permissionSourceCodexCLI,
		"codex":       permissionSourceCodexCLI,
		"cursor":      permissionSourceCursor,
		"unknown":     "",
		"":            "",
	}
	for input, want := range cases {
		if got := normalizePermissionSource(input); got != want {
			t.Errorf("normalizePermissionSource(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestCoerceToolInput(t *testing.T) {
	if got := coerceToolInput(map[string]interface{}{"a": 1}); got["a"] != 1 {
		t.Errorf("object passthrough failed: %v", got)
	}
	if got := coerceToolInput(`{"command":"ls"}`); got["command"] != "ls" {
		t.Errorf("json string decode failed: %v", got)
	}
	if got := coerceToolInput("plain text"); got["value"] != "plain text" {
		t.Errorf("non-json string wrap failed: %v", got)
	}
	if got := coerceToolInput(nil); got != nil {
		t.Errorf("nil should map to nil, got %v", got)
	}
}

func TestBuildPermissionRequestClaudeCodeHonorsConfig(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	overrideManagedSettingsPath(t, filepath.Join(t.TempDir(), "absent.json"))
	claudeDir := filepath.Join(home, ".claude")
	if err := os.MkdirAll(claudeDir, 0700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	settings := `{"permissions":{"allow":["Bash(npm run test:*)"]}}`
	if err := os.WriteFile(filepath.Join(claudeDir, "settings.json"), []byte(settings), 0644); err != nil {
		t.Fatalf("write settings: %v", err)
	}

	raw := []byte(`{
		"session_id":"sess-1",
		"cwd":"/repo",
		"hook_event_name":"PreToolUse",
		"tool_name":"Bash",
		"tool_input":{"command":"npm run test:unit","description":"run unit tests"}
	}`)
	req, err := buildPermissionRequest(permissionSourceClaudeCode, raw, permissionHookCredential{})
	if err != nil {
		t.Fatalf("buildPermissionRequest: %v", err)
	}
	if req.ToolName != "Bash" {
		t.Errorf("tool_name = %q", req.ToolName)
	}
	if req.SessionID != "sess-1" || req.Cwd != "/repo" {
		t.Errorf("session/cwd mismatch: %+v", req)
	}
	if req.ClientDecision != "allow" {
		t.Errorf("client_decision = %q, want allow", req.ClientDecision)
	}
	if req.AgentReasoning != "run unit tests" {
		t.Errorf("agent_reasoning = %q", req.AgentReasoning)
	}
}

func TestBuildPermissionRequestCodexOmitsClientDecision(t *testing.T) {
	raw := []byte(`{"session_id":"s","cwd":"/w","tool_name":"Bash","tool_input":{"command":"ls"}}`)
	req, err := buildPermissionRequest(permissionSourceCodexCLI, raw, permissionHookCredential{})
	if err != nil {
		t.Fatalf("buildPermissionRequest: %v", err)
	}
	if req.ToolName != "Bash" {
		t.Errorf("tool_name = %q", req.ToolName)
	}
	if req.ClientDecision != "" {
		t.Errorf("codex should omit client_decision, got %q", req.ClientDecision)
	}
}

func TestBuildPermissionRequestCursorShellAndMCP(t *testing.T) {
	shell := []byte(`{"command":"rm -rf /tmp/x","cwd":"/w","conversation_id":"c-1","sandbox":false}`)
	req, err := buildPermissionRequest(permissionSourceCursor, shell, permissionHookCredential{})
	if err != nil {
		t.Fatalf("buildPermissionRequest shell: %v", err)
	}
	if req.ToolName != "Shell" {
		t.Errorf("expected synthesized Shell tool, got %q", req.ToolName)
	}
	if req.ToolInput["command"] != "rm -rf /tmp/x" {
		t.Errorf("command mapping failed: %v", req.ToolInput)
	}
	if req.SessionID != "c-1" {
		t.Errorf("expected conversation_id as session, got %q", req.SessionID)
	}
	if req.ClientDecision != "ask" {
		t.Errorf("non-allowlisted non-sandbox shell should ask, got %q", req.ClientDecision)
	}

	mcp := []byte(`{"tool_name":"search","tool_input":"{\"q\":\"hi\"}","url":"https://x","conversation_id":"c-2"}`)
	req, err = buildPermissionRequest(permissionSourceCursor, mcp, permissionHookCredential{})
	if err != nil {
		t.Fatalf("buildPermissionRequest mcp: %v", err)
	}
	if req.ToolName != "search" {
		t.Errorf("tool_name = %q", req.ToolName)
	}
	if req.ToolInput["q"] != "hi" {
		t.Errorf("expected decoded tool_input, got %v", req.ToolInput)
	}
	if req.ClientDecision != "ask" {
		t.Errorf("third-party MCP should ask, got %q", req.ClientDecision)
	}
}

func TestRenderHookDecision(t *testing.T) {
	allow := hookDecision{Behavior: "allow", Reason: "ok"}
	deny := hookDecision{Behavior: "deny", Reason: "blocked"}

	claudeAllow := renderHookDecision(permissionSourceClaudeCode, allow)
	cs := claudeAllow["hookSpecificOutput"].(map[string]interface{})
	if cs["hookEventName"] != "PreToolUse" || cs["permissionDecision"] != "allow" {
		t.Errorf("claude allow mapping wrong: %v", cs)
	}
	claudeDeny := renderHookDecision(permissionSourceClaudeCode, deny)
	cs = claudeDeny["hookSpecificOutput"].(map[string]interface{})
	if cs["permissionDecision"] != "deny" || cs["permissionDecisionReason"] != "blocked" {
		t.Errorf("claude deny mapping wrong: %v", cs)
	}

	codexAllow := renderHookDecision(permissionSourceCodexCLI, allow)
	co := codexAllow["hookSpecificOutput"].(map[string]interface{})
	dec := co["decision"].(map[string]interface{})
	if co["hookEventName"] != "PermissionRequest" || dec["behavior"] != "allow" {
		t.Errorf("codex allow mapping wrong: %v", co)
	}
	if _, hasMessage := dec["message"]; hasMessage {
		t.Errorf("codex allow should not carry a message: %v", dec)
	}
	codexDeny := renderHookDecision(permissionSourceCodexCLI, deny)
	co = codexDeny["hookSpecificOutput"].(map[string]interface{})
	dec = co["decision"].(map[string]interface{})
	if dec["behavior"] != "deny" || dec["message"] != "blocked" {
		t.Errorf("codex deny mapping wrong: %v", dec)
	}

	cursorAllow := renderHookDecision(permissionSourceCursor, allow)
	if cursorAllow["permission"] != "allow" {
		t.Errorf("cursor allow mapping wrong: %v", cursorAllow)
	}
	cursorDeny := renderHookDecision(permissionSourceCursor, deny)
	if cursorDeny["permission"] != "deny" || cursorDeny["agent_message"] != "blocked" {
		t.Errorf("cursor deny mapping wrong: %v", cursorDeny)
	}
}

func TestResolvePermissionDecisionFailDefault(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	// No credential file and no settings.json -> no token available.
	raw := []byte(`{"tool_name":"Bash","tool_input":{"command":"ls"}}`)

	failClosed := resolvePermissionDecision(permissionSourceCodexCLI, raw, false)
	if failClosed.Behavior != "deny" {
		t.Errorf("expected deny by default, got %q", failClosed.Behavior)
	}

	failOpen := resolvePermissionDecision(permissionSourceCodexCLI, raw, true)
	if failOpen.Behavior != "allow" {
		t.Errorf("expected allow with fail-open, got %q", failOpen.Behavior)
	}
}

func TestResolvePermissionDecisionSkipsClaudeHookUnderCursor(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	// Even with a durable credential that would otherwise escalate (and fail
	// closed on an unreachable Preloop), Cursor's third-party load of the
	// Claude Code PreToolUse hook must auto-allow.
	agent := AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}
	writeTestPermissionCredential(t, home, runtimePrincipalIDForAgent(agent), permissionHookCredential{
		BaseURL: "https://preloop.ai",
		Token:   "agt_cursor_cross_talk",
		Source:  permissionSourceClaudeCode,
	})

	prevGetenv := permissionHookGetenv
	t.Cleanup(func() { permissionHookGetenv = prevGetenv })
	permissionHookGetenv = func(key string) string {
		switch key {
		case "CURSOR_AGENT":
			return "1"
		case "CURSOR_VERSION":
			return "1.0.0-test"
		default:
			return ""
		}
	}

	raw := []byte(`{"tool_name":"Write","tool_input":{"file_path":"/tmp/x","content":"hi"}}`)
	decision := resolvePermissionDecision(permissionSourceClaudeCode, raw, false)
	if decision.Behavior != "allow" {
		t.Fatalf("expected allow under Cursor host, got %q (%s)", decision.Behavior, decision.Reason)
	}
	if !strings.Contains(decision.Reason, "Cursor") {
		t.Errorf("reason should mention Cursor, got %q", decision.Reason)
	}

	// Codex / Cursor sources are unaffected by the Claude-under-Cursor guard.
	permissionHookGetenv = func(string) string { return "" }
	codexDecision := resolvePermissionDecision(permissionSourceCodexCLI, raw, false)
	if codexDecision.Behavior != "deny" {
		t.Errorf("codex without credential should still fail closed, got %q", codexDecision.Behavior)
	}
}

func TestIsCursorHostInvokingClaudeHook(t *testing.T) {
	prevGetenv := permissionHookGetenv
	t.Cleanup(func() { permissionHookGetenv = prevGetenv })

	permissionHookGetenv = func(string) string { return "" }
	if isCursorHostInvokingClaudeHook() {
		t.Fatal("expected false with empty env")
	}

	permissionHookGetenv = func(key string) string {
		if key == "CURSOR_PROJECT_DIR" {
			return "/repo"
		}
		return ""
	}
	if !isCursorHostInvokingClaudeHook() {
		t.Fatal("expected true when CURSOR_PROJECT_DIR is set")
	}

	permissionHookGetenv = func(key string) string {
		if key == "CURSOR_AGENT" {
			return "true"
		}
		return ""
	}
	if !isCursorHostInvokingClaudeHook() {
		t.Fatal("expected true when CURSOR_AGENT is truthy")
	}
}

func TestResolvePermissionDecisionClientFallbackAllow(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	overrideManagedSettingsPath(t, filepath.Join(t.TempDir(), "absent.json"))
	claudeDir := filepath.Join(home, ".claude")
	if err := os.MkdirAll(claudeDir, 0700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	// Config auto-allows this command, but there is no reachable Preloop
	// credential -> we should still honor the client's own allow.
	settings := `{"permissions":{"allow":["Bash(ls)"]}}`
	if err := os.WriteFile(filepath.Join(claudeDir, "settings.json"), []byte(settings), 0644); err != nil {
		t.Fatalf("write settings: %v", err)
	}
	raw := []byte(`{"tool_name":"Bash","tool_input":{"command":"ls"}}`)
	decision := resolvePermissionDecision(permissionSourceClaudeCode, raw, false)
	if decision.Behavior != "allow" {
		t.Errorf("expected client-config allow fallback, got %q (%s)", decision.Behavior, decision.Reason)
	}
}

func TestResolvePermissionDecisionCallsEndpoint(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	var gotAuth string
	var gotBody permissionCheckRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != permissionCheckPath {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}
		gotAuth = r.Header.Get("Authorization")
		_ = json.NewDecoder(r.Body).Decode(&gotBody)
		_ = json.NewEncoder(w).Encode(permissionCheckResponse{
			Decision: "allow",
			Reason:   "approved on watch",
		})
	}))
	defer server.Close()

	writeTestPermissionCredential(t, home, "codex-agent", permissionHookCredential{
		BaseURL: server.URL,
		Token:   "agt_test",
		Source:  permissionSourceCodexCLI,
	})

	raw := []byte(`{"tool_name":"Bash","tool_input":{"command":"ls"}}`)
	decision := resolvePermissionDecision(permissionSourceCodexCLI, raw, false)
	if decision.Behavior != "allow" || decision.Reason != "approved on watch" {
		t.Errorf("unexpected decision: %+v", decision)
	}
	if gotAuth != "Bearer agt_test" {
		t.Errorf("unexpected auth header: %q", gotAuth)
	}
	if gotBody.Source != permissionSourceCodexCLI || gotBody.ToolName != "Bash" {
		t.Errorf("unexpected request body: %+v", gotBody)
	}
}

func TestClaudeSettingsBearerTokenFallback(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	claudeDir := filepath.Join(home, ".claude")
	if err := os.MkdirAll(claudeDir, 0700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	settings := `{"servers":{"preloop":{"headers":{"Authorization":"Bearer agt_durable"}}}}`
	if err := os.WriteFile(filepath.Join(claudeDir, "settings.json"), []byte(settings), 0644); err != nil {
		t.Fatalf("write settings: %v", err)
	}
	token, ok := claudeSettingsBearerToken()
	if !ok || token != "agt_durable" {
		t.Errorf("expected agt_durable, got %q (ok=%v)", token, ok)
	}

	cred, err := resolvePermissionHookCredential(permissionSourceClaudeCode)
	if err != nil {
		t.Fatalf("resolvePermissionHookCredential: %v", err)
	}
	if cred.Token != "agt_durable" {
		t.Errorf("expected fallback token, got %q", cred.Token)
	}
}

func TestInstallRemoveApprovalHooksClaudeCode(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	agent := AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}

	// Pre-existing unrelated user settings + an unrelated PreToolUse hook.
	claudeDir := filepath.Join(home, ".claude")
	if err := os.MkdirAll(claudeDir, 0700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	preexisting := `{"model":"claude-sonnet-4","hooks":{"PreToolUse":[{"matcher":"Write","hooks":[{"type":"command","command":"echo keep"}]}]}}`
	settingsPath := filepath.Join(claudeDir, "settings.json")
	if err := os.WriteFile(settingsPath, []byte(preexisting), 0644); err != nil {
		t.Fatalf("write settings: %v", err)
	}

	var installOut strings.Builder
	if err := installApprovalHooks(agent, "https://preloop.ai", "agt_x", &installOut); err != nil {
		t.Fatalf("installApprovalHooks: %v", err)
	}
	if note := installOut.String(); !strings.Contains(note, "onboard Cursor --approvals") {
		t.Errorf("expected Cursor cross-talk note in onboarding output, got %q", note)
	}

	doc := readJSONDoc(t, settingsPath)
	if doc["model"] != "claude-sonnet-4" {
		t.Errorf("unrelated setting was clobbered: %v", doc["model"])
	}
	pre := nestedHookCommands(t, doc, "PreToolUse")
	if !containsSubstring(pre, "echo keep") {
		t.Errorf("pre-existing hook was removed: %v", pre)
	}
	if !containsSubstring(pre, "permission-hook --source claude_code") {
		t.Errorf("our hook was not installed: %v", pre)
	}

	// Credential file written with token and timeout.
	credPath := filepath.Join(home, ".preloop", "agents", runtimePrincipalIDForAgent(agent), permissionHookCredentialFileName)
	credDoc := readJSONDoc(t, credPath)
	if credDoc["token"] != "agt_x" || credDoc["source"] != permissionSourceClaudeCode {
		t.Errorf("credential file wrong: %v", credDoc)
	}
	if timeout, ok := credDoc["timeout_seconds"].(float64); !ok || timeout <= 0 {
		t.Errorf("expected timeout_seconds in credential, got %v", credDoc["timeout_seconds"])
	}

	// Idempotent: re-install should not duplicate our entry.
	if err := installApprovalHooks(agent, "https://preloop.ai", "agt_x", nil); err != nil {
		t.Fatalf("re-install: %v", err)
	}
	doc = readJSONDoc(t, settingsPath)
	pre = nestedHookCommands(t, doc, "PreToolUse")
	if countSubstring(pre, "permission-hook --source claude_code") != 1 {
		t.Errorf("expected exactly one preloop hook, got %v", pre)
	}

	// Remove: our entry and credential gone, the unrelated hook preserved.
	if err := removeApprovalHooks(agent, nil); err != nil {
		t.Fatalf("removeApprovalHooks: %v", err)
	}
	doc = readJSONDoc(t, settingsPath)
	pre = nestedHookCommands(t, doc, "PreToolUse")
	if containsSubstring(pre, "permission-hook") {
		t.Errorf("our hook was not removed: %v", pre)
	}
	if !containsSubstring(pre, "echo keep") {
		t.Errorf("unrelated hook was removed during cleanup: %v", pre)
	}
	if _, err := os.Stat(credPath); !os.IsNotExist(err) {
		t.Errorf("credential file should be removed, stat err=%v", err)
	}
}

func TestInstallRemoveApprovalHooksCodex(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	agent := AgentConfig{Name: "Codex CLI", ConfigPath: filepath.Join(home, ".codex", "config.toml")}
	hooksPath := filepath.Join(home, ".codex", "hooks.json")

	if err := installApprovalHooks(agent, "https://preloop.ai", "agt_y", nil); err != nil {
		t.Fatalf("install: %v", err)
	}
	doc := readJSONDoc(t, hooksPath)
	cmds := nestedHookCommands(t, doc, "PermissionRequest")
	if !containsSubstring(cmds, "permission-hook --source codex_cli") {
		t.Errorf("codex hook not installed: %v", cmds)
	}

	if err := removeApprovalHooks(agent, nil); err != nil {
		t.Fatalf("remove: %v", err)
	}
	// hooks.json was created solely by us, so it should be deleted on removal.
	if _, err := os.Stat(hooksPath); !os.IsNotExist(err) {
		t.Errorf("expected codex hooks.json removed, stat err=%v", err)
	}
}

func TestInstallRemoveApprovalHooksCursor(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	agent := AgentConfig{Name: "Cursor", ConfigPath: filepath.Join(home, ".cursor", "mcp.json")}
	hooksPath := filepath.Join(home, ".cursor", "hooks.json")

	if err := installApprovalHooks(agent, "https://preloop.ai", "agt_z", nil); err != nil {
		t.Fatalf("install: %v", err)
	}
	doc := readJSONDoc(t, hooksPath)
	if v, ok := doc["version"]; !ok || (v != float64(1) && v != 1) {
		t.Errorf("cursor hooks version missing/wrong: %v", doc["version"])
	}
	hooks := doc["hooks"].(map[string]interface{})
	for _, key := range []string{"beforeShellExecution", "beforeMCPExecution", "preToolUse"} {
		arr, _ := hooks[key].([]interface{})
		if len(arr) != 1 {
			t.Fatalf("expected one %s entry, got %v", key, arr)
		}
		entry := arr[0].(map[string]interface{})
		if cmd, _ := entry["command"].(string); !containsOne(cmd, "permission-hook --source cursor") {
			t.Errorf("%s command wrong: %v", key, entry)
		}
	}

	if err := removeApprovalHooks(agent, nil); err != nil {
		t.Fatalf("remove: %v", err)
	}
	if _, err := os.Stat(hooksPath); !os.IsNotExist(err) {
		t.Errorf("expected cursor hooks.json removed, stat err=%v", err)
	}
}

// --- test helpers ---

func writeTestPermissionCredential(t *testing.T, home, principal string, cred permissionHookCredential) {
	t.Helper()
	dir := filepath.Join(home, ".preloop", "agents", principal)
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatalf("mkdir cred dir: %v", err)
	}
	data, err := json.Marshal(cred)
	if err != nil {
		t.Fatalf("marshal cred: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, permissionHookCredentialFileName), data, 0600); err != nil {
		t.Fatalf("write cred: %v", err)
	}
}

func readJSONDoc(t *testing.T, path string) map[string]interface{} {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	var doc map[string]interface{}
	if err := json.Unmarshal(data, &doc); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	return doc
}

func nestedHookCommands(t *testing.T, doc map[string]interface{}, eventKey string) []string {
	t.Helper()
	hooks, _ := doc["hooks"].(map[string]interface{})
	list, _ := hooks[eventKey].([]interface{})
	var cmds []string
	for _, item := range list {
		entry, _ := item.(map[string]interface{})
		inner, _ := entry["hooks"].([]interface{})
		for _, h := range inner {
			hm, _ := h.(map[string]interface{})
			if c, ok := hm["command"].(string); ok {
				cmds = append(cmds, c)
			}
		}
	}
	return cmds
}

func containsOne(haystack, needle string) bool {
	return containsSubstring([]string{haystack}, needle)
}

func containsSubstring(values []string, needle string) bool {
	return countSubstring(values, needle) > 0
}

func countSubstring(values []string, needle string) int {
	count := 0
	for _, v := range values {
		if indexOf(v, needle) >= 0 {
			count++
		}
	}
	return count
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

func TestPermissionCheckTimeoutFor(t *testing.T) {
	t.Parallel()

	got := permissionCheckTimeoutFor(permissionHookCredential{TimeoutSeconds: 600})
	want := 600*time.Second + permissionCheckHTTPHeadroom
	if got != want {
		t.Fatalf("timeout = %v, want %v", got, want)
	}

	got = permissionCheckTimeoutFor(permissionHookCredential{})
	want = time.Duration(defaultApprovalHookTimeoutSeconds)*time.Second + permissionCheckHTTPHeadroom
	if got != want {
		t.Fatalf("default timeout = %v, want %v", got, want)
	}
}

func TestBuildPermissionRequestCursorAllowlistAndSandbox(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	policyPath := filepath.Join(dir, "permissions.json")
	if err := os.WriteFile(policyPath, []byte(`{
		"terminalAllowlist": ["git", "npm:install*"],
		"mcpAllowlist": ["github:*"]
	}`), 0o600); err != nil {
		t.Fatal(err)
	}

	cred := permissionHookCredential{
		BaseURL:     "https://preloop.ai",
		PolicyPaths: []string{policyPath},
	}

	cases := []struct {
		name     string
		raw      string
		wantTool string
		wantDec  string
	}{
		{
			name:     "allowlisted shell",
			raw:      `{"command":"git status","sandbox":false}`,
			wantTool: "Shell",
			wantDec:  "allow",
		},
		{
			name:     "non-allowlisted shell asks",
			raw:      `{"command":"rm -rf /tmp/x","sandbox":false}`,
			wantTool: "Shell",
			wantDec:  "ask",
		},
		{
			name:     "sandboxed shell allows",
			raw:      `{"command":"rm -rf /tmp/x","sandbox":true}`,
			wantTool: "Shell",
			wantDec:  "allow",
		},
		{
			name:     "allowlisted mcp",
			raw:      `{"tool_name":"list_issues","server_name":"github","tool_input":{}}`,
			wantTool: "list_issues",
			wantDec:  "allow",
		},
		{
			name:     "preloop mcp allows",
			raw:      `{"tool_name":"search","url":"https://preloop.ai/mcp","tool_input":{}}`,
			wantTool: "search",
			wantDec:  "allow",
		},
		{
			name:     "third party mcp asks",
			raw:      `{"tool_name":"delete","server_name":"other","url":"https://example.com","tool_input":{}}`,
			wantTool: "delete",
			wantDec:  "ask",
		},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			req, err := buildPermissionRequest(permissionSourceCursor, []byte(tc.raw), cred)
			if err != nil {
				t.Fatalf("build: %v", err)
			}
			if req.ToolName != tc.wantTool {
				t.Fatalf("tool = %q, want %q", req.ToolName, tc.wantTool)
			}
			if req.ClientDecision != tc.wantDec {
				t.Fatalf("client_decision = %q, want %q", req.ClientDecision, tc.wantDec)
			}
		})
	}
}

func TestEvaluateClaudePermissionPolicySafeTools(t *testing.T) {
	t.Parallel()

	policy := claudePermissionPolicy{}
	if got := evaluateClaudePermissionPolicy(policy, "", "Grep", nil); got != "allow" {
		t.Fatalf("Grep = %q, want allow", got)
	}
	if got := evaluateClaudePermissionPolicy(policy, "", "Read", nil); got != "allow" {
		t.Fatalf("Read = %q, want allow", got)
	}
	if got := evaluateClaudePermissionPolicy(policy, "", "Bash", map[string]interface{}{"command": "ls"}); got != "ask" {
		t.Fatalf("Bash = %q, want ask", got)
	}

	policy.Allow = []string{"Bash"}
	if got := evaluateClaudePermissionPolicy(policy, "", "Bash", map[string]interface{}{"command": "ls"}); got != "allow" {
		t.Fatalf("allowed Bash = %q, want allow", got)
	}

	policy = claudePermissionPolicy{Deny: []string{"Grep"}}
	if got := evaluateClaudePermissionPolicy(policy, "", "Grep", nil); got != "deny" {
		t.Fatalf("denied Grep = %q, want deny", got)
	}
}

func boolPtr(v bool) *bool { return &v }

func TestBuildPermissionRequestClaudeProjectSettings(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	overrideManagedSettingsPath(t, filepath.Join(t.TempDir(), "absent.json"))

	// User settings have no rules; the allow rule lives in the project's
	// .claude/settings.json under the session cwd carried by the hook event.
	project := t.TempDir()
	projectClaude := filepath.Join(project, ".claude")
	if err := os.MkdirAll(projectClaude, 0700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	settings := `{"permissions":{"allow":["Bash(npm run test:*)"],"deny":["Bash(npm publish:*)"]}}`
	if err := os.WriteFile(filepath.Join(projectClaude, "settings.json"), []byte(settings), 0644); err != nil {
		t.Fatalf("write project settings: %v", err)
	}

	build := func(command string) permissionCheckRequest {
		t.Helper()
		raw := []byte(`{"cwd":` + jsonString(project) + `,"tool_name":"Bash","tool_input":{"command":` + jsonString(command) + `}}`)
		req, err := buildPermissionRequest(permissionSourceClaudeCode, raw, permissionHookCredential{})
		if err != nil {
			t.Fatalf("buildPermissionRequest: %v", err)
		}
		return req
	}

	if req := build("npm run test -- --watch"); req.ClientDecision != "allow" {
		t.Errorf("project allow rule ignored, client_decision = %q", req.ClientDecision)
	}
	if req := build("npm publish --tag latest"); req.ClientDecision != "deny" {
		t.Errorf("project deny rule ignored, client_decision = %q", req.ClientDecision)
	}
	if req := build("terraform apply"); req.ClientDecision != "ask" {
		t.Errorf("unmatched command should ask, client_decision = %q", req.ClientDecision)
	}
}

func TestBuildPermissionRequestClaudeManagedDenyWins(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	managedPath := filepath.Join(t.TempDir(), "managed-settings.json")
	overrideManagedSettingsPath(t, managedPath)

	claudeDir := filepath.Join(home, ".claude")
	if err := os.MkdirAll(claudeDir, 0700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	userSettings := `{"permissions":{"allow":["Bash(curl:*)"]}}`
	if err := os.WriteFile(filepath.Join(claudeDir, "settings.json"), []byte(userSettings), 0644); err != nil {
		t.Fatalf("write user settings: %v", err)
	}
	managed := `{"permissions":{"deny":["Bash(curl:*)"]}}`
	if err := os.WriteFile(managedPath, []byte(managed), 0644); err != nil {
		t.Fatalf("write managed settings: %v", err)
	}

	raw := []byte(`{"tool_name":"Bash","tool_input":{"command":"curl https://example.com"}}`)
	req, err := buildPermissionRequest(permissionSourceClaudeCode, raw, permissionHookCredential{})
	if err != nil {
		t.Fatalf("buildPermissionRequest: %v", err)
	}
	if req.ClientDecision != "deny" {
		t.Errorf("managed deny should beat user allow, client_decision = %q", req.ClientDecision)
	}
}

func TestClaudeSafeReadBashFallback(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	overrideManagedSettingsPath(t, filepath.Join(t.TempDir(), "absent.json"))

	build := func(command string, cred permissionHookCredential) permissionCheckRequest {
		t.Helper()
		raw := []byte(`{"tool_name":"Bash","tool_input":{"command":` + jsonString(command) + `}}`)
		req, err := buildPermissionRequest(permissionSourceClaudeCode, raw, cred)
		if err != nil {
			t.Fatalf("buildPermissionRequest: %v", err)
		}
		return req
	}

	// Claude Code's default is exact policy fidelity: with no configured
	// rules, even read-only Bash commands escalate to Preloop, mirroring the
	// prompt Claude Code itself would have shown.
	if req := build("git status", permissionHookCredential{}); req.ClientDecision != "ask" {
		t.Errorf("safe-read Bash should ask by default, got %q", req.ClientDecision)
	}
	// Mutating or chained commands escalate too.
	if req := build("git status && git push", permissionHookCredential{}); req.ClientDecision != "ask" {
		t.Errorf("chained command should ask, got %q", req.ClientDecision)
	}
	if req := build("git push", permissionHookCredential{}); req.ClientDecision != "ask" {
		t.Errorf("mutating command should ask, got %q", req.ClientDecision)
	}
	// The credential file can opt in to the read-only auto-allow...
	on := permissionHookCredential{SafeReadAutoAllow: boolPtr(true)}
	if req := build("git status", on); req.ClientDecision != "allow" {
		t.Errorf("opted-in safe-read should auto-allow, got %q", req.ClientDecision)
	}
	// ...without widening mutating commands.
	if req := build("git push", on); req.ClientDecision != "ask" {
		t.Errorf("mutating command should still ask when opted in, got %q", req.ClientDecision)
	}

	// A configured deny rule wins over the safe-read fallback.
	claudeDir := filepath.Join(home, ".claude")
	if err := os.MkdirAll(claudeDir, 0700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	settings := `{"permissions":{"deny":["Bash(git status:*)"]}}`
	if err := os.WriteFile(filepath.Join(claudeDir, "settings.json"), []byte(settings), 0644); err != nil {
		t.Fatalf("write settings: %v", err)
	}
	if req := build("git status", permissionHookCredential{}); req.ClientDecision != "deny" {
		t.Errorf("deny rule should beat safe-read fallback, got %q", req.ClientDecision)
	}
}

func TestCursorSafeReadAutoAllow(t *testing.T) {
	// No permissions.json anywhere (the common case): safe reads auto-allow,
	// everything else still asks.
	home := t.TempDir()
	testenv.SetHome(t, home)

	build := func(raw string, cred permissionHookCredential) permissionCheckRequest {
		t.Helper()
		req, err := buildPermissionRequest(permissionSourceCursor, []byte(raw), cred)
		if err != nil {
			t.Fatalf("buildPermissionRequest: %v", err)
		}
		return req
	}

	cases := []struct {
		name string
		raw  string
		want string
	}{
		{"plain ls allows", `{"command":"ls","sandbox":false}`, "allow"},
		{"git status allows", `{"command":"git status","sandbox":false}`, "allow"},
		{"safe pipeline allows", `{"command":"cat foo | grep bar | wc -l","sandbox":false}`, "allow"},
		{"chained injection asks", `{"command":"ls; rm -rf /","sandbox":false}`, "ask"},
		{"redirect asks", `{"command":"cat foo > bar","sandbox":false}`, "ask"},
		{"and-chain asks", `{"command":"git status && git push","sandbox":false}`, "ask"},
		{"mutating command asks", `{"command":"rm -rf /tmp/x","sandbox":false}`, "ask"},
		// An MCP tool carrying a "command"-shaped input must not ride the
		// shell safe-read fallback.
		{"mcp with command field asks", `{"tool_name":"run","server_name":"other","tool_input":{"command":"ls"}}`, "ask"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if req := build(tc.raw, permissionHookCredential{}); req.ClientDecision != tc.want {
				t.Errorf("client_decision = %q, want %q", req.ClientDecision, tc.want)
			}
		})
	}

	// safe_read_auto_allow: false in the credential disables the fallback.
	off := permissionHookCredential{SafeReadAutoAllow: boolPtr(false)}
	if req := build(`{"command":"ls","sandbox":false}`, off); req.ClientDecision != "ask" {
		t.Errorf("disabled safe-read should ask, got %q", req.ClientDecision)
	}
}

func TestCursorLocalWorkspaceEditsAutoAllow(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want string
	}{
		{
			name: "Write relative path allows",
			raw:  `{"cwd":"/repo","tool_name":"Write","tool_input":{"file_path":"src/foo.ts"}}`,
			want: "allow",
		},
		{
			name: "StrReplace workspace path allows",
			raw:  `{"cwd":"/repo","tool_name":"StrReplace","tool_input":{"path":"/repo/src/foo.ts"}}`,
			want: "allow",
		},
		{
			name: "Write outside workspace asks",
			raw:  `{"cwd":"/repo","tool_name":"Write","tool_input":{"file_path":"/etc/passwd"}}`,
			want: "ask",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req, err := buildPermissionRequest(
				permissionSourceCursor,
				[]byte(tc.raw),
				permissionHookCredential{},
			)
			if err != nil {
				t.Fatalf("buildPermissionRequest: %v", err)
			}
			if req.ClientDecision != tc.want {
				t.Fatalf("client_decision = %q, want %q", req.ClientDecision, tc.want)
			}
		})
	}
}

func TestFailureDecisionReasonsAreActionable(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	// Missing credential: the deny reason must say what is missing and how to
	// repair or disable the hook. Codex is used because it never computes a
	// client decision, so the failure path is always taken.
	raw := []byte(`{"tool_name":"Bash","tool_input":{"command":"rm -rf /tmp/x"}}`)
	decision := resolvePermissionDecision(permissionSourceCodexCLI, raw, false)
	if decision.Behavior != "deny" {
		t.Fatalf("expected fail-closed deny, got %q", decision.Behavior)
	}
	for _, want := range []string{
		"no Preloop credential found",
		"preloop agents onboard \"Codex CLI\" --approvals",
		filepath.Join(home, ".codex", "hooks.json"),
	} {
		if !containsOne(decision.Reason, want) {
			t.Errorf("missing-credential reason lacks %q:\n%s", want, decision.Reason)
		}
	}

	// Unreachable Preloop: the deny reason must include the URL, the error,
	// and the same remediation.
	writeTestPermissionCredential(t, home, "codex-agent", permissionHookCredential{
		BaseURL: "http://127.0.0.1:1",
		Token:   "agt_test",
		Source:  permissionSourceCodexCLI,
	})
	decision = resolvePermissionDecision(permissionSourceCodexCLI, raw, false)
	if decision.Behavior != "deny" {
		t.Fatalf("expected fail-closed deny, got %q", decision.Behavior)
	}
	for _, want := range []string{
		"could not reach Preloop at http://127.0.0.1:1" + permissionCheckPath,
		"denied by default",
		"preloop agents onboard \"Codex CLI\" --approvals",
		filepath.Join(home, ".codex", "hooks.json"),
	} {
		if !containsOne(decision.Reason, want) {
			t.Errorf("unreachable reason lacks %q:\n%s", want, decision.Reason)
		}
	}
}

// Claude Code and Cursor have a native "ask" verdict, so a Preloop hard
// failure hands the prompt back to the agent's local UI instead of denying;
// Codex (no ask verdict) stays fail-closed, and --fail-open still wins.
func TestFailureDecisionAskFallbackBySource(t *testing.T) {
	t.Parallel()
	if got := failureDecision(permissionSourceClaudeCode, false, "boom"); got.Behavior != "ask" {
		t.Errorf("claude_code failure should ask locally, got %q", got.Behavior)
	}
	if got := failureDecision(permissionSourceCursor, false, "boom"); got.Behavior != "ask" {
		t.Errorf("cursor failure should ask locally, got %q", got.Behavior)
	}
	if got := failureDecision(permissionSourceCodexCLI, false, "boom"); got.Behavior != "deny" {
		t.Errorf("codex failure should fail closed, got %q", got.Behavior)
	}
	if got := failureDecision(permissionSourceClaudeCode, true, "boom"); got.Behavior != "allow" {
		t.Errorf("fail-open should win over ask fallback, got %q", got.Behavior)
	}
}

// A timed-out (unanswered) Preloop approval re-surfaces the agent's local
// prompt on ask-capable adapters; an explicit human deny never does.
func TestResolvePermissionDecisionTimeoutAskFallback(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	overrideManagedSettingsPath(t, filepath.Join(t.TempDir(), "absent.json"))

	respond := func(resp permissionCheckResponse) *httptest.Server {
		return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_ = json.NewEncoder(w).Encode(resp)
		}))
	}
	raw := []byte(`{"tool_name":"Bash","tool_input":{"command":"rm -rf /tmp/x"}}`)

	// Timed out -> ask locally (Claude Code).
	server := respond(permissionCheckResponse{
		Decision: "deny", Reason: "Approval request timed out", TimedOut: true,
	})
	writeTestPermissionCredential(t, home, "claude-agent", permissionHookCredential{
		BaseURL: server.URL, Token: "agt_test", Source: permissionSourceClaudeCode,
	})
	decision := resolvePermissionDecision(permissionSourceClaudeCode, raw, false)
	server.Close()
	if decision.Behavior != "ask" {
		t.Errorf("timed-out approval should fall back to local ask, got %q (%s)", decision.Behavior, decision.Reason)
	}

	// Explicit human deny -> deny, never downgraded.
	server = respond(permissionCheckResponse{Decision: "deny", Reason: "Declined on watch"})
	writeTestPermissionCredential(t, home, "claude-agent", permissionHookCredential{
		BaseURL: server.URL, Token: "agt_test", Source: permissionSourceClaudeCode,
	})
	decision = resolvePermissionDecision(permissionSourceClaudeCode, raw, false)
	server.Close()
	if decision.Behavior != "deny" || decision.Reason != "Declined on watch" {
		t.Errorf("human deny must stay deny, got %+v", decision)
	}

	// Codex has no ask verdict: a timed-out approval stays deny.
	server = respond(permissionCheckResponse{
		Decision: "deny", Reason: "Approval request timed out", TimedOut: true,
	})
	writeTestPermissionCredential(t, home, "codex-agent", permissionHookCredential{
		BaseURL: server.URL, Token: "agt_test", Source: permissionSourceCodexCLI,
	})
	decision = resolvePermissionDecision(permissionSourceCodexCLI, raw, false)
	server.Close()
	if decision.Behavior != "deny" {
		t.Errorf("codex timeout must stay deny, got %q", decision.Behavior)
	}
}

// The ask verdict renders into each adapter's native schema.
func TestRenderHookDecisionAsk(t *testing.T) {
	t.Parallel()
	ask := hookDecision{Behavior: "ask", Reason: "ask locally"}

	claude := renderHookDecision(permissionSourceClaudeCode, ask)
	cs := claude["hookSpecificOutput"].(map[string]interface{})
	if cs["permissionDecision"] != "ask" {
		t.Errorf("claude ask mapping wrong: %v", cs)
	}

	cursor := renderHookDecision(permissionSourceCursor, ask)
	if cursor["permission"] != "ask" {
		t.Errorf("cursor ask mapping wrong: %v", cursor)
	}

	// Codex degrades ask to deny (schema has no ask verdict).
	codex := renderHookDecision(permissionSourceCodexCLI, ask)
	co := codex["hookSpecificOutput"].(map[string]interface{})
	dec := co["decision"].(map[string]interface{})
	if dec["behavior"] != "deny" {
		t.Errorf("codex ask should degrade to deny, got %v", dec)
	}
}

func jsonString(s string) string {
	data, _ := json.Marshal(s)
	return string(data)
}

func TestMatchCursorTerminalAllowlist(t *testing.T) {
	t.Parallel()

	allow := []string{"git", "npm:install*"}
	if !matchCursorTerminalAllowlist(allow, "git status") {
		t.Fatal("expected git status to match")
	}
	if !matchCursorTerminalAllowlist(allow, "npm install express") {
		t.Fatal("expected npm install express to match")
	}
	if matchCursorTerminalAllowlist(allow, "rm -rf /") {
		t.Fatal("did not expect rm to match")
	}
}

func TestEnablePermissionPromptBuiltin(t *testing.T) {
	t.Parallel()

	var captured map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/tool-configurations" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		if err := json.NewDecoder(r.Body).Decode(&captured); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]interface{}{"id": "cfg-1"})
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "test-token")
	var out strings.Builder
	if err := enablePermissionPromptBuiltin(client, "agent-123", &out); err != nil {
		t.Fatalf("enablePermissionPromptBuiltin: %v", err)
	}

	if captured["tool_name"] != "permission_prompt" {
		t.Fatalf("expected permission_prompt tool_name, got %v", captured["tool_name"])
	}
	if captured["tool_source"] != "builtin" {
		t.Fatalf("expected builtin tool_source, got %v", captured["tool_source"])
	}
	if captured["managed_agent_id"] != "agent-123" {
		t.Fatalf("expected agent-scoped enable, got %v", captured["managed_agent_id"])
	}
	if captured["is_enabled"] != true {
		t.Fatalf("expected is_enabled true, got %v", captured["is_enabled"])
	}
	if !strings.Contains(out.String(), "--permission-prompt-tool mcp__preloop__permission_prompt") {
		t.Fatalf("expected exact flag instruction in output, got:\n%s", out.String())
	}
	if !strings.Contains(out.String(), "scoped to this agent") {
		t.Fatalf("expected scope note in output, got:\n%s", out.String())
	}
}

func TestEnablePermissionPromptBuiltinToleratesExistingRow(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"detail": "Configuration for tool 'permission_prompt' already exists",
		})
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "test-token")
	var out strings.Builder
	if err := enablePermissionPromptBuiltin(client, "agent-123", &out); err != nil {
		t.Fatalf("expected already-exists to be tolerated, got %v", err)
	}
	if !strings.Contains(out.String(), "already configured") {
		t.Fatalf("expected already-configured note, got:\n%s", out.String())
	}
}

func TestEnablePermissionPromptBuiltinPropagatesServerErrors(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "test-token")
	if err := enablePermissionPromptBuiltin(client, "agent-123", nil); err == nil {
		t.Fatal("expected server error to propagate")
	}
}

// Cursor's preToolUse fires for every tool, including Shell and MCP tools
// that beforeShellExecution / beforeMCPExecution already gate. The duplicate
// preToolUse event must be answered locally, without a permission-check POST,
// so one command raises exactly one approval request. Native file tools keep
// their preToolUse gating: an out-of-workspace Write still posts, and an
// in-workspace Write is auto-allowed locally by isLocalWorkspaceEdit.
func TestResolvePermissionDecisionCursorPreToolUseDedupe(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	var mu sync.Mutex
	var posted []permissionCheckRequest
	var postsAllowed bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body permissionCheckRequest
		_ = json.NewDecoder(r.Body).Decode(&body)
		mu.Lock()
		posted = append(posted, body)
		allowed := postsAllowed
		mu.Unlock()
		if !allowed {
			t.Errorf("permission-check must not be called for this event, got POST for tool %q", body.ToolName)
		}
		_ = json.NewEncoder(w).Encode(permissionCheckResponse{
			Decision: "allow",
			Reason:   "approved on watch",
		})
	}))
	defer server.Close()

	writeTestPermissionCredential(t, home, "cursor-agent", permissionHookCredential{
		BaseURL:       server.URL,
		Token:         "agt_test",
		Source:        permissionSourceCursor,
		WorkspaceRoot: "/repo",
	})
	writeCursorPreloopHookEvents(t, home, "beforeShellExecution", "beforeMCPExecution", "preToolUse")

	cases := []struct {
		name         string
		raw          string
		wantBehavior string
		wantReason   string
		wantPosts    int
		wantToolName string
	}{
		{
			name:         "beforeShellExecution posts once",
			raw:          `{"hook_event_name":"beforeShellExecution","command":"rm -rf build","cwd":"/repo","sandbox":false}`,
			wantBehavior: "allow",
			wantReason:   "approved on watch",
			wantPosts:    1,
			wantToolName: "Shell",
		},
		{
			name:         "preToolUse Shell is answered locally",
			raw:          `{"hook_event_name":"preToolUse","tool_name":"Shell","tool_input":{"command":"rm -rf build","working_directory":"/repo"},"cwd":"/repo"}`,
			wantBehavior: "allow",
			wantReason:   cursorPreToolUseDuplicateReason,
			wantPosts:    0,
		},
		{
			name:         "beforeMCPExecution posts once",
			raw:          `{"hook_event_name":"beforeMCPExecution","tool_name":"create_issue","tool_input":"{\"title\":\"x\"}","mcp_server_name":"linear","url":"https://mcp.linear.app/sse","cwd":"/repo"}`,
			wantBehavior: "allow",
			wantReason:   "approved on watch",
			wantPosts:    1,
			wantToolName: "create_issue",
		},
		{
			name:         "preToolUse MCP tool with mcp_server_name is answered locally",
			raw:          `{"hook_event_name":"preToolUse","tool_name":"create_issue","tool_input":{"title":"x"},"mcp_server_name":"linear","cwd":"/repo"}`,
			wantBehavior: "allow",
			wantReason:   cursorPreToolUseDuplicateReason,
			wantPosts:    0,
		},
		{
			name:         "preToolUse MCP matcher form is answered locally",
			raw:          `{"hook_event_name":"preToolUse","tool_name":"MCP:create_issue","tool_input":{"title":"x"},"cwd":"/repo"}`,
			wantBehavior: "allow",
			wantReason:   cursorPreToolUseDuplicateReason,
			wantPosts:    0,
		},
		{
			name:         "preToolUse Preloop MCP tool is answered locally",
			raw:          `{"hook_event_name":"preToolUse","tool_name":"preloop_create_issue","tool_input":{"title":"x"},"cwd":"/repo"}`,
			wantBehavior: "allow",
			wantReason:   cursorPreToolUseDuplicateReason,
			wantPosts:    0,
		},
		{
			name:         "preToolUse Write outside workspace still posts",
			raw:          `{"hook_event_name":"preToolUse","tool_name":"Write","tool_input":{"file_path":"/etc/motd"},"cwd":"/repo","workspace_roots":["/repo"]}`,
			wantBehavior: "allow",
			wantReason:   "approved on watch",
			wantPosts:    1,
			wantToolName: "Write",
		},
		{
			name:         "preToolUse Write inside workspace is auto-allowed locally",
			raw:          `{"hook_event_name":"preToolUse","tool_name":"Write","tool_input":{"file_path":"/repo/src/a.ts"},"cwd":"/repo","workspace_roots":["/repo"]}`,
			wantBehavior: "allow",
			wantReason:   "Allowed by the agent's own configuration.",
			wantPosts:    0,
		},
		{
			name:         "preToolUse Delete outside workspace still posts",
			raw:          `{"hook_event_name":"preToolUse","tool_name":"Delete","tool_input":{"path":"/etc/motd"},"cwd":"/repo","workspace_roots":["/repo"]}`,
			wantBehavior: "allow",
			wantReason:   "approved on watch",
			wantPosts:    1,
			wantToolName: "Delete",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			mu.Lock()
			posted = nil
			postsAllowed = tc.wantPosts > 0
			mu.Unlock()

			decision := resolvePermissionDecision(permissionSourceCursor, []byte(tc.raw), false)
			if decision.Behavior != tc.wantBehavior {
				t.Fatalf("behavior = %q (%s), want %q", decision.Behavior, decision.Reason, tc.wantBehavior)
			}
			if decision.Reason != tc.wantReason {
				t.Errorf("reason = %q, want %q", decision.Reason, tc.wantReason)
			}

			mu.Lock()
			got := append([]permissionCheckRequest(nil), posted...)
			mu.Unlock()
			if len(got) != tc.wantPosts {
				t.Fatalf("permission-check POSTs = %d, want %d (%+v)", len(got), tc.wantPosts, got)
			}
			if tc.wantPosts > 0 && got[0].ToolName != tc.wantToolName {
				t.Errorf("posted tool_name = %q, want %q", got[0].ToolName, tc.wantToolName)
			}
		})
	}
}

func writeCursorPreloopHookEvents(t *testing.T, home string, events ...string) {
	t.Helper()
	hooks := map[string]interface{}{}
	for _, event := range events {
		hooks[event] = []interface{}{
			map[string]interface{}{"command": "preloop agents permission-hook --source cursor"},
		}
	}
	path := filepath.Join(home, ".cursor", "hooks.json")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatalf("mkdir hooks dir: %v", err)
	}
	if err := writeJSONDocument(path, map[string]interface{}{"version": 1, "hooks": hooks}); err != nil {
		t.Fatalf("write hooks.json: %v", err)
	}
}

// cursorPreToolUseDuplicateDecision must only short-circuit preToolUse events
// for tools that have a dedicated before* hook that is still installed;
// every other event falls through to the normal evaluation path.
func TestCursorPreToolUseDuplicateDecision(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	writeCursorPreloopHookEvents(t, home, "beforeShellExecution", "beforeMCPExecution", "preToolUse")

	cases := []struct {
		name string
		raw  string
		want bool
	}{
		{name: "preToolUse Shell", raw: `{"hook_event_name":"preToolUse","tool_name":"Shell","tool_input":{"command":"ls"}}`, want: true},
		{name: "preToolUse shell lower-case", raw: `{"hook_event_name":"preToolUse","tool_name":"shell","tool_input":{"command":"ls"}}`, want: true},
		{name: "preToolUse MCP server name", raw: `{"hook_event_name":"preToolUse","tool_name":"search","mcp_server_name":"linear"}`, want: true},
		{name: "preToolUse MCP server url", raw: `{"hook_event_name":"preToolUse","tool_name":"search","mcp_server_url":"https://mcp.example.com/sse"}`, want: true},
		{name: "preToolUse MCP matcher prefix", raw: `{"hook_event_name":"preToolUse","tool_name":"MCP:search"}`, want: true},
		{name: "preToolUse Preloop MCP by tool name", raw: `{"hook_event_name":"preToolUse","tool_name":"preloop_list_issues"}`, want: true},
		{name: "preToolUse Preloop MCP by url", raw: `{"hook_event_name":"preToolUse","tool_name":"list_issues","url":"https://api.preloop.ai/mcp"}`, want: true},
		{name: "preToolUse Write", raw: `{"hook_event_name":"preToolUse","tool_name":"Write","tool_input":{"file_path":"a.ts"}}`, want: false},
		{name: "preToolUse StrReplace", raw: `{"hook_event_name":"preToolUse","tool_name":"StrReplace","tool_input":{"path":"a.ts"}}`, want: false},
		{name: "preToolUse Delete", raw: `{"hook_event_name":"preToolUse","tool_name":"Delete","tool_input":{"path":"a.ts"}}`, want: false},
		{name: "preToolUse Read", raw: `{"hook_event_name":"preToolUse","tool_name":"Read","tool_input":{"path":"a.ts"}}`, want: false},
		{name: "preToolUse Task", raw: `{"hook_event_name":"preToolUse","tool_name":"Task"}`, want: false},
		{name: "preToolUse without tool_name", raw: `{"hook_event_name":"preToolUse"}`, want: false},
		{name: "beforeShellExecution", raw: `{"hook_event_name":"beforeShellExecution","command":"ls"}`, want: false},
		{name: "beforeMCPExecution", raw: `{"hook_event_name":"beforeMCPExecution","tool_name":"search","mcp_server_name":"linear"}`, want: false},
		{name: "missing hook_event_name", raw: `{"tool_name":"Shell","tool_input":{"command":"ls"}}`, want: false},
		{name: "empty payload", raw: ``, want: false},
		{name: "invalid json", raw: `{`, want: false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			decision, got := cursorPreToolUseDuplicateDecision([]byte(tc.raw))
			if got != tc.want {
				t.Fatalf("duplicate = %v, want %v", got, tc.want)
			}
			if got && (decision.Behavior != "allow" || decision.Reason != cursorPreToolUseDuplicateReason) {
				t.Errorf("unexpected decision %+v", decision)
			}
		})
	}
}

// Cursor documents mcp_server_name as the server key on MCP hook events; it
// must feed both the mcpAllowlist matcher and Preloop MCP detection.
func TestCursorMCPServerNameHonorsDocumentedKey(t *testing.T) {
	event := map[string]interface{}{"mcp_server_name": "linear", "url": "https://mcp.linear.app/sse"}
	if got := cursorMCPServerName(event); got != "linear" {
		t.Fatalf("cursorMCPServerName = %q, want %q", got, "linear")
	}
	if !matchCursorMCPAllowlist([]string{"linear:create_*"}, event, "create_issue") {
		t.Error("mcpAllowlist should match on documented mcp_server_name")
	}
	preloop := map[string]interface{}{"mcp_server_name": "preloop", "command": "npx preloop-mcp"}
	if !isPreloopMCPTool(preloop, "list_issues", "") {
		t.Error("isPreloopMCPTool should detect Preloop via mcp_server_name")
	}
	if got := cursorMCPServerName(map[string]interface{}{"mcp_server_url": "https://mcp.example.com/sse"}); got != "https://mcp.example.com/sse" {
		t.Errorf("mcp_server_url alias = %q", got)
	}
	if got := cursorMCPServerName(map[string]interface{}{"server": "legacy"}); got != "legacy" {
		t.Errorf("server alias = %q", got)
	}
}

func TestCursorPreToolUseDuplicateRequiresInstalledBeforeHook(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	raw := []byte(`{"hook_event_name":"preToolUse","tool_name":"Shell","tool_input":{"command":"ls"}}`)

	if _, got := cursorPreToolUseDuplicateDecision(raw); got {
		t.Fatal("missing hooks.json must not locally allow Shell preToolUse")
	}

	writeCursorPreloopHookEvents(t, home, "preToolUse")
	if _, got := cursorPreToolUseDuplicateDecision(raw); got {
		t.Fatal("preToolUse-only install must not locally allow Shell")
	}

	writeCursorPreloopHookEvents(t, home, "beforeShellExecution")
	if _, got := cursorPreToolUseDuplicateDecision(raw); !got {
		t.Fatal("beforeShellExecution install should locally allow Shell preToolUse")
	}

	mcpRaw := []byte(`{"hook_event_name":"preToolUse","tool_name":"search","mcp_server_name":"linear"}`)
	if _, got := cursorPreToolUseDuplicateDecision(mcpRaw); got {
		t.Fatal("Shell before-hook must not locally allow MCP preToolUse")
	}
	writeCursorPreloopHookEvents(t, home, "beforeMCPExecution")
	if _, got := cursorPreToolUseDuplicateDecision(mcpRaw); !got {
		t.Fatal("beforeMCPExecution install should locally allow MCP preToolUse")
	}
}
