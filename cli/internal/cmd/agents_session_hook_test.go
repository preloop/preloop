package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

type sessionHookServer struct {
	mu       sync.Mutex
	requests []sessionStartRequest
	srv      *httptest.Server
}

func newSessionHookServer(t *testing.T, runtimeID string) *sessionHookServer {
	t.Helper()
	s := &sessionHookServer{}
	s.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != agentSessionStartPath || r.Header.Get("Authorization") != "Bearer agt_session" {
			http.Error(w, "unexpected", http.StatusUnauthorized)
			return
		}
		var req sessionStartRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		s.mu.Lock()
		s.requests = append(s.requests, req)
		s.mu.Unlock()
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"runtime_session_id": runtimeID,
			"parent_session_id":  req.ParentSessionID,
			"started_at":         "2026-10-04T05:00:00+00:00",
			"created":            true,
		})
	}))
	t.Cleanup(s.srv.Close)
	return s
}

func (s *sessionHookServer) calls() []sessionStartRequest {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]sessionStartRequest(nil), s.requests...)
}

func runSessionHook(t *testing.T, payload map[string]interface{}) string {
	t.Helper()
	raw, _ := json.Marshal(payload)
	var stderr bytes.Buffer
	agentsSessionHookCmd.SetIn(bytes.NewReader(raw))
	agentsSessionHookCmd.SetErr(&stderr)
	agentsSessionHookCmd.SetOut(&bytes.Buffer{})
	if err := agentsSessionHookCmd.Flags().Set("source", permissionSourceClaudeCode); err != nil {
		t.Fatal(err)
	}
	if err := runAgentsSessionHook(agentsSessionHookCmd, nil); err != nil {
		t.Fatalf("session hook returned an error: %v", err)
	}
	return stderr.String()
}

func setupSessionHook(t *testing.T, runtimeID string) (string, *sessionHookServer) {
	t.Helper()
	home := t.TempDir()
	testenv.SetHome(t, home)
	server := newSessionHookServer(t, runtimeID)
	writeTestPermissionCredential(t, home, "claude_machine", permissionHookCredential{
		BaseURL: server.srv.URL,
		Token:   "agt_session",
		Source:  permissionSourceClaudeCode,
	})
	return home, server
}

func TestSessionStartRecordsParentWritesHandoffAndExportsLineage(t *testing.T) {
	home, server := setupSessionHook(t, "11111111-2222-3333-4444-555555555555")
	envFile := filepath.Join(home, "claude-env.sh")
	t.Setenv("CLAUDE_ENV_FILE", envFile)
	t.Setenv(parentSessionEnvVar, "99999999-2222-3333-4444-555555555555")
	t.Setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")

	stderr := runSessionHook(t, map[string]interface{}{
		"hook_event_name": "SessionStart",
		"session_id":      "ext-child-1",
		"cwd":             "/work/clone",
	})

	calls := server.calls()
	if len(calls) != 1 || calls[0].ParentSessionID != "99999999-2222-3333-4444-555555555555" ||
		calls[0].SessionID != "ext-child-1" || calls[0].Cwd != "/work/clone" {
		t.Fatalf("unexpected registration: %+v", calls)
	}
	handoff := readJSONDoc(t, filepath.Join(home, ".preloop", "sessions", "ext-child-1.json"))
	if handoff["runtime_session_id"] != "11111111-2222-3333-4444-555555555555" ||
		handoff["agent_kind"] != "claude_code" || handoff["cwd"] != "/work/clone" ||
		handoff["started_at"] == nil {
		t.Fatalf("unexpected handoff file: %v", handoff)
	}
	env, err := os.ReadFile(envFile)
	if err != nil {
		t.Fatal(err)
	}
	if string(env) != "export PRELOOP_PARENT_SESSION_ID=11111111-2222-3333-4444-555555555555\n" {
		t.Fatalf("unexpected env export: %q", env)
	}
	if stderr != "preloop runtime_session_id=11111111-2222-3333-4444-555555555555\n" {
		t.Fatalf("stderr = %q", stderr)
	}

	// A resume of the same session does not print the line a second time.
	again := runSessionHook(t, map[string]interface{}{
		"hook_event_name": "SessionStart",
		"session_id":      "ext-child-1",
	})
	if strings.Contains(again, "runtime_session_id=") {
		t.Fatalf("id line printed twice: %q", again)
	}
}

func TestSessionStartInteractivePrintsNothing(t *testing.T) {
	_, _ = setupSessionHook(t, "11111111-2222-3333-4444-555555555555")
	t.Setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
	t.Setenv("CLAUDE_ENV_FILE", "")
	stderr := runSessionHook(t, map[string]interface{}{
		"hook_event_name": "SessionStart",
		"session_id":      "ext-interactive",
	})
	if stderr != "" {
		t.Fatalf("interactive stderr = %q", stderr)
	}
}

func TestFirstPromptIsSentOnce(t *testing.T) {
	_, server := setupSessionHook(t, "11111111-2222-3333-4444-555555555555")
	t.Setenv("CLAUDE_ENV_FILE", "")
	runSessionHook(t, map[string]interface{}{"hook_event_name": "SessionStart", "session_id": "ext-p"})
	runSessionHook(t, map[string]interface{}{"hook_event_name": "UserPromptSubmit", "session_id": "ext-p", "prompt": "Fix the bug"})
	runSessionHook(t, map[string]interface{}{"hook_event_name": "UserPromptSubmit", "session_id": "ext-p", "prompt": "And another"})

	calls := server.calls()
	if len(calls) != 2 {
		t.Fatalf("want 2 registrations (start, first prompt), got %d: %+v", len(calls), calls)
	}
	if calls[1].FirstPrompt != "Fix the bug" {
		t.Fatalf("first prompt = %q", calls[1].FirstPrompt)
	}
}

func TestSessionHookFailsOpenWithoutCredentialOrServer(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	stderr := runSessionHook(t, map[string]interface{}{"hook_event_name": "SessionStart", "session_id": "ext-none"})
	if stderr != "" {
		t.Fatalf("no credential should be silent, got %q", stderr)
	}
	writeTestPermissionCredential(t, home, "claude_machine", permissionHookCredential{
		BaseURL: "http://127.0.0.1:1", Token: "agt_session", Source: permissionSourceClaudeCode,
	})
	stderr = runSessionHook(t, map[string]interface{}{"hook_event_name": "SessionStart", "session_id": "ext-none"})
	if !strings.Contains(stderr, "could not register the session") {
		t.Fatalf("expected a logged failure, got %q", stderr)
	}
	// A path-like session id never becomes a file name.
	if _, err := sessionHandoffPath("../escape"); err == nil {
		t.Fatal("expected a path-like id to be rejected")
	}
}

func TestClaudeInstallWiresAndRemovesSessionHooks(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	agent := AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, "config.json")}
	if err := installApprovalHooks(agent, "http://127.0.0.1:1", "agt_synthetic", nil); err != nil {
		t.Fatal(err)
	}
	path, err := approvalHookConfigPath(permissionSourceClaudeCode)
	if err != nil {
		t.Fatal(err)
	}
	hooks := readJSONDoc(t, path)["hooks"].(map[string]interface{})
	for _, event := range sessionHookEvents {
		list, ok := hooks[event].([]interface{})
		if !ok || len(list) != 1 {
			t.Fatalf("%s not installed: %v", event, hooks[event])
		}
		inner := list[0].(map[string]interface{})["hooks"].([]interface{})[0].(map[string]interface{})
		if !strings.Contains(inner["command"].(string), "agents permission-hook session --source claude_code") {
			t.Fatalf("%s command = %v", event, inner["command"])
		}
	}
	// Re-onboarding replaces rather than duplicates.
	if err := installApprovalHooks(agent, "http://127.0.0.1:1", "agt_synthetic", nil); err != nil {
		t.Fatal(err)
	}
	hooks = readJSONDoc(t, path)["hooks"].(map[string]interface{})
	if len(hooks["SessionStart"].([]interface{})) != 1 {
		t.Fatalf("SessionStart duplicated: %v", hooks["SessionStart"])
	}
	if err := removeApprovalHooks(agent, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	doc := readJSONDoc(t, path)
	if remaining, ok := doc["hooks"].(map[string]interface{}); ok {
		for _, event := range append([]string{"PreToolUse"}, sessionHookEvents...) {
			if _, present := remaining[event]; present {
				t.Fatalf("%s left behind: %v", event, remaining)
			}
		}
	}
}
