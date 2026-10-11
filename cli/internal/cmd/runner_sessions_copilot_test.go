package cmd

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

const copilotSessTestUUID = "11111111-2222-4333-8444-555555555555"

func TestCopilotSessionArgsGolden(t *testing.T) {
	profile := hostExecProfile{
		Name: "copilot", Executable: "copilot",
		AllowTools: []string{"shell(git:*)"},
		DenyTools:  []string{"shell(rm:*)"},
		ModelMap:   map[string]string{"sonnet": "claude-sonnet-4.6"},
	}
	turn1, err := copilotSessionArgs(profile, runnerSessionTurnSpec{HarnessSessionID: copilotSessTestUUID, Text: "-explain", Model: "sonnet"})
	if err != nil {
		t.Fatal(err)
	}
	want1 := []string{
		"--prompt=-explain", "-s", "--no-ask-user", "--output-format=json",
		"--allow-tool=shell(git:*)", "--deny-tool=shell(rm:*)",
		"--no-remote", "--model=claude-sonnet-4.6",
		"--session-id=" + copilotSessTestUUID,
	}
	if !reflect.DeepEqual(turn1, want1) {
		t.Fatalf("turn 1 argv\n got %q\nwant %q", turn1, want1)
	}
	turnN, err := copilotSessionArgs(profile, runnerSessionTurnSpec{HarnessSessionID: copilotSessTestUUID, Text: "next", Model: "auto", Resume: true})
	if err != nil {
		t.Fatal(err)
	}
	wantN := []string{
		"--prompt=next", "-s", "--no-ask-user", "--output-format=json",
		"--allow-tool=shell(git:*)", "--deny-tool=shell(rm:*)",
		"--no-remote", "--resume=" + copilotSessTestUUID,
	}
	if !reflect.DeepEqual(turnN, wantN) {
		t.Fatalf("turn N argv\n got %q\nwant %q", turnN, wantN)
	}
	for _, args := range [][]string{turn1, turnN} {
		for _, arg := range args {
			if arg == "--remote" || strings.HasPrefix(arg, "--remote=") || arg == "--allow-all-tools" || arg == "--yolo" || strings.HasPrefix(arg, "--assisted") {
				t.Fatalf("forbidden flag %q", arg)
			}
		}
	}
	// An unmapped model id passes through only when it is a plain id.
	if _, err := copilotSessionArgs(profile, runnerSessionTurnSpec{HarnessSessionID: copilotSessTestUUID, Text: "x", Model: "--yolo"}); err == nil {
		t.Fatalf("a flag-shaped model must be refused")
	}
	if _, err := copilotSessionArgs(profile, runnerSessionTurnSpec{HarnessSessionID: "../../other", Text: "x"}); err == nil {
		t.Fatalf("a non-uuid session id must be refused")
	}
}

func TestCopilotSessionEnvUsesAllowlistAndStripsBYOK(t *testing.T) {
	environ := []string{
		"PATH=/usr/bin", "HOME=/home/jane",
		"COPILOT_PROVIDER_BASE_URL=https://byok.example.com",
		"COPILOT_PROVIDER_API_KEY=k", "Copilot_Allow_All=true", "COPILOT_ALLOW_ALL=true",
		"PRELOOP_TOKEN=runner-secret", "AWS_SECRET_ACCESS_KEY=x",
		"COPILOT_GH_HOST=example.ghe.com",
	}
	env := copilotSessionEnv(hostExecProfile{Name: "copilot", Executable: "copilot"}, environ, "/opt/copilot/bin/copilot")
	joined := strings.Join(env, "\n")
	for _, banned := range []string{"COPILOT_PROVIDER_", "ALLOW_ALL", "Allow_All", "PRELOOP_TOKEN", "AWS_SECRET"} {
		if strings.Contains(joined, banned) {
			t.Fatalf("%s reached the session env:\n%s", banned, joined)
		}
	}
	if !strings.Contains(joined, "COPILOT_GH_HOST=example.ghe.com") {
		t.Fatalf("harness namespace must pass:\n%s", joined)
	}
}

func TestCopilotSessionStartRequiresApprovalHook(t *testing.T) {
	a := &copilotSessionAdapter{
		loadProfile:           func() (hostExecProfile, error) { return hostExecProfile{Name: "copilot", Executable: "copilot"}, nil },
		ensureUsageHooks:      func() error { return nil },
		approvalHookInstalled: func() (bool, error) { return false, nil },
		resolveCommand:        func(string) (string, []string, error) { return "/bin/copilot", nil, nil },
		environ:               os.Environ,
		newID:                 newSessionUUID,
	}
	err := a.Start(&runnerRemoteSession{})
	if err == nil || !strings.Contains(err.Error(), "copilot_approval_hook_missing") {
		t.Fatalf("want copilot_approval_hook_missing even without allow_all_tools, got %v", err)
	}
	a.approvalHookInstalled = func() (bool, error) { return true, nil }
	s := &runnerRemoteSession{}
	if err := a.Start(s); err != nil || !uuidRe.MatchString(s.HarnessSessionID) {
		t.Fatalf("start: %v %q", err, s.HarnessSessionID)
	}
}

func TestCopilotSessionEventMapping(t *testing.T) {
	capture := copilotCapture{}
	var kinds []string
	for _, line := range strings.Split(copilotSuccessStream, "\n") {
		if kind, _, ok := copilotSessionEvent(&capture, line); ok {
			kinds = append(kinds, kind)
		}
	}
	if !reflect.DeepEqual(kinds, []string{"agent_message", "usage"}) {
		t.Fatalf("kinds = %v", kinds)
	}
	if kind, payload, ok := copilotSessionEvent(&capture, `{"type":"tool.execution_start","data":{"toolName":"shell"}}`); !ok || kind != "tool_call" || payload["event"] != "tool.execution_start" {
		t.Fatalf("tool start: %s %v", kind, payload)
	}
	if kind, _, _ := copilotSessionEvent(&capture, `{"type":"tool.execution_complete","data":{}}`); kind != "tool_result" {
		t.Fatalf("tool complete: %s", kind)
	}
	if kind, payload, _ := copilotSessionEvent(&capture, "Error: No authentication information found"); kind != "stderr" || payload["text"] == "" {
		t.Fatalf("plain text error: %s", kind)
	}
}

// installFakeCopilotSession writes a fake copilot that logs its argv and
// working directory, then prints a successful JSONL turn. When the prompt
// contains "sleep" it blocks instead, so a stop can kill it mid-turn.
func installFakeCopilotSession(t *testing.T) string {
	t.Helper()
	argvLog := filepath.Join(t.TempDir(), "argv.log")
	body := `printf '%s|%s\n' "$(pwd)" "$*" >> ` + argvLog + `
case "$*" in *sleep*) sleep 30 ;; esac
sid=""
for a in "$@"; do case "$a" in --session-id=*) sid="${a#--session-id=}" ;; --resume=*) sid="${a#--resume=}" ;; esac; done
echo '{"type":"assistant.message","data":{"messageId":"m1","model":"gpt-5.2","content":"ok"}}'
echo '{"type":"tool.execution_start","data":{"toolName":"shell"}}'
echo "{\"type\":\"result\",\"sessionId\":\"$sid\",\"exitCode\":0,\"usage\":{\"premiumRequests\":1}}"`
	copilotHome := setupCopilotHost(t, body)
	hooks := filepath.Join(copilotHome, "hooks")
	if err := os.MkdirAll(hooks, 0o700); err != nil {
		t.Fatal(err)
	}
	doc := `{"version":1,"hooks":{"preToolUse":[{"type":"command","bash":"preloop agents permission-hook --source copilot_cli"}]}}`
	if err := os.WriteFile(filepath.Join(hooks, copilotPreloopHooksFileName), []byte(doc), 0o600); err != nil {
		t.Fatal(err)
	}
	return argvLog
}

func readArgvLog(t *testing.T, path string) []string {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return strings.Split(strings.TrimSpace(string(raw)), "\n")
}

func TestCopilotSessionEndToEndWithFakeBinary(t *testing.T) {
	argvLog := installFakeCopilotSession(t)
	dir := realTempDir(t)
	enableTestSessions(t, dir)
	m := newRunnerSessionManager(nil)
	m.notifyHost = func(string) {}
	h := &sessionHarness{t: t, m: m}

	start := startMsg(sessTestID)
	start.Model = "gpt-5.2"
	start.FirstPrompt = "first turn"
	m.handle(start)
	msgs := h.waitFor(isTurnDone(runnerSessionFirstTurnID))
	if done := findMsg(msgs, isTurnDone(runnerSessionFirstTurnID)); done["status"] != "ok" {
		t.Fatalf("turn 1 failed: %v", msgs)
	}
	if findMsg(msgs, func(m map[string]any) bool { return m["kind"] == "tool_call" }) == nil {
		t.Fatalf("tool call event missing: %v", msgs)
	}
	m.handle(runnerWSMessage{Type: "session_turn", RemoteSessionID: sessTestID, TurnID: "t2", Text: "second turn"})
	h.waitFor(isTurnDone("t2"))

	lines := readArgvLog(t, argvLog)
	if len(lines) != 2 {
		t.Fatalf("want 2 copilot runs, got %q", lines)
	}
	if !strings.HasPrefix(lines[0], dir+"|") || !strings.Contains(lines[0], "--session-id=") || strings.Contains(lines[0], "--resume") || !strings.Contains(lines[0], "--model=gpt-5.2") || !strings.Contains(lines[0], "--no-remote") {
		t.Fatalf("turn 1 argv: %q", lines[0])
	}
	sid := m.sessions[sessTestID].HarnessSessionID
	if !strings.Contains(lines[1], "--resume="+sid) || strings.Contains(lines[1], "--session-id") {
		t.Fatalf("turn 2 must resume %s: %q", sid, lines[1])
	}

	// Kill mid-turn: the process is gone well before its 30 s sleep.
	m.handle(runnerWSMessage{Type: "session_turn", RemoteSessionID: sessTestID, TurnID: "t3", Text: "please sleep"})
	h.waitFor(isState("running"))
	began := time.Now()
	m.handle(runnerWSMessage{Type: "session_stop", RemoteSessionID: sessTestID, Mode: "kill"})
	msgs = h.waitFor(isState("ended"))
	if time.Since(began) > 10*time.Second {
		t.Fatalf("kill took %s", time.Since(began))
	}
	if done := findMsg(msgs, isTurnDone("t3")); done == nil || done["error_code"] != "killed" {
		t.Fatalf("killed turn: %v", msgs)
	}
}

func TestCopilotSessionResumesAfterRunnerRestart(t *testing.T) {
	argvLog := installFakeCopilotSession(t)
	enableTestSessions(t, realTempDir(t))
	m := newRunnerSessionManager(nil)
	m.notifyHost = func(string) {}
	h := &sessionHarness{t: t, m: m}
	start := startMsg(sessTestID)
	start.FirstPrompt = "first"
	m.handle(start)
	h.waitFor(isTurnDone(runnerSessionFirstTurnID))
	sid := m.sessions[sessTestID].HarnessSessionID
	m.shutdown()

	restarted := newRunnerSessionManager(nil)
	restarted.notifyHost = func(string) {}
	h.m = restarted
	restarted.handle(runnerWSMessage{Type: "session_turn", RemoteSessionID: sessTestID, TurnID: "after", Text: "again"})
	h.waitFor(isTurnDone("after"))
	lines := readArgvLog(t, argvLog)
	if last := lines[len(lines)-1]; !strings.Contains(last, "--resume="+sid) {
		t.Fatalf("turn after restart must use --resume=%s: %q", sid, last)
	}
}

func TestCopilotSessionTurnFailsWhenNotLoggedIn(t *testing.T) {
	installFakeCopilotSession(t)
	if err := os.WriteFile(filepath.Join(strings.Split(os.Getenv("PATH"), string(os.PathListSeparator))[0], "copilot"), []byte("#!/bin/sh\necho 'Error: No authentication information found'\nexit 1\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	a := newCopilotSessionAdapter()
	result := a.Turn(context.Background(), runnerSessionTurnSpec{HarnessSessionID: copilotSessTestUUID, Text: "hi", Dir: realTempDir(t)}, func(string, map[string]any) {})
	if result.Status != "error" || result.ErrorCode != rejectHarnessSignedOut {
		t.Fatalf("want harness_signed_out, got %+v", result)
	}
}

func enableTestSessions(t *testing.T, dir string) {
	t.Helper()
	writeRunnerConfig(t, map[string]any{
		"harnesses":              map[string]any{"copilot_cli": map[string]any{"sessions_enabled": true}},
		"authorized_directories": []any{map[string]any{"id": "dir_9f2c", "path": dir, "label": "ims"}},
	})
}
