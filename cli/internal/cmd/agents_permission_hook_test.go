package cmd

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
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
	t.Setenv("HOME", home)
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
	t.Setenv("HOME", home)
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

func TestResolvePermissionDecisionClientFallbackAllow(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
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
	t.Setenv("HOME", home)

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
	t.Setenv("HOME", home)
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
	t.Setenv("HOME", home)
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

	if err := installApprovalHooks(agent, "https://preloop.ai", "agt_x", nil); err != nil {
		t.Fatalf("installApprovalHooks: %v", err)
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
	t.Setenv("HOME", home)
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
	t.Setenv("HOME", home)
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
	for _, key := range []string{"beforeShellExecution", "beforeMCPExecution"} {
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
