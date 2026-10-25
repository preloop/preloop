package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func withPauseClock(t *testing.T, now time.Time) *time.Time {
	t.Helper()
	current := now
	previous := approvalsPauseNow
	approvalsPauseNow = func() time.Time { return current }
	t.Cleanup(func() { approvalsPauseNow = previous })
	return &current
}

func captureHookStderr(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	previous := permissionHookStderr
	permissionHookStderr = &buf
	t.Cleanup(func() { permissionHookStderr = previous })
	return &buf
}

func TestApprovalsPauseSetExpireAndResume(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	clock := withPauseClock(t, time.Date(2026, 10, 18, 12, 0, 0, 0, time.UTC))

	if _, ok := activeApprovalsPause(permissionSourceCodexCLI); ok {
		t.Fatal("no pause expected before pausing")
	}
	until, err := setApprovalsPause("", time.Hour)
	if err != nil {
		t.Fatalf("setApprovalsPause: %v", err)
	}
	if want := clock.Add(time.Hour); !until.Equal(want) {
		t.Fatalf("until = %v, want %v", until, want)
	}
	for _, source := range []string{permissionSourceCodexCLI, permissionSourceClaudeCode} {
		if got, ok := activeApprovalsPause(source); !ok || !got.Equal(until) {
			t.Errorf("all-agent pause not active for %s: %v %v", source, got, ok)
		}
	}

	*clock = clock.Add(time.Hour + time.Second)
	if _, ok := activeApprovalsPause(permissionSourceCodexCLI); ok {
		t.Fatal("pause must expire automatically")
	}

	if _, err := setApprovalsPause(permissionSourceCodexCLI, 2*time.Hour); err != nil {
		t.Fatal(err)
	}
	if _, ok := activeApprovalsPause(permissionSourceClaudeCode); ok {
		t.Error("a Codex pause must not pause Claude Code")
	}
	if _, ok := activeApprovalsPause(permissionSourceCodexCLI); !ok {
		t.Error("Codex pause should be active")
	}
	if err := clearApprovalsPause(permissionSourceCodexCLI); err != nil {
		t.Fatal(err)
	}
	if _, ok := activeApprovalsPause(permissionSourceCodexCLI); ok {
		t.Error("resume must clear the pause early")
	}
}

func TestApprovalsPauseResumeAllClearsAgentPauses(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	withPauseClock(t, time.Date(2026, 10, 18, 12, 0, 0, 0, time.UTC))
	if _, err := setApprovalsPause(permissionSourceCursor, time.Hour); err != nil {
		t.Fatal(err)
	}
	if _, err := setApprovalsPause("", time.Hour); err != nil {
		t.Fatal(err)
	}
	if err := clearApprovalsPause(""); err != nil {
		t.Fatal(err)
	}
	if _, ok := activeApprovalsPause(permissionSourceCursor); ok {
		t.Error("resume without an agent must clear every pause")
	}
}

func TestApprovalsPauseCorruptStateIsNotAPause(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	path, err := approvalsPausePath()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(path, home) {
		t.Fatalf("pause state %s must live under the Preloop config dir in %s", path, home)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, ok := activeApprovalsPause(permissionSourceCodexCLI); ok {
		t.Error("an unreadable pause file must fail closed (no pause)")
	}
}

func TestParsePauseDuration(t *testing.T) {
	for _, tc := range []struct {
		in      string
		want    time.Duration
		wantErr bool
	}{
		{"", time.Hour, false},
		{"30m", 30 * time.Minute, false},
		{"24h", 24 * time.Hour, false},
		{"25h", 0, true},
		{"0s", 0, true},
		{"-1h", 0, true},
		{"soon", 0, true},
	} {
		got, err := parsePauseDuration(tc.in)
		if (err != nil) != tc.wantErr || (!tc.wantErr && got != tc.want) {
			t.Errorf("parsePauseDuration(%q) = %v, %v", tc.in, got, err)
		}
	}
}

func TestResolvePauseTarget(t *testing.T) {
	cases := map[string]string{
		"":            "",
		"Codex CLI":   permissionSourceCodexCLI,
		"codex":       permissionSourceCodexCLI,
		"Claude Code": permissionSourceClaudeCode,
		"cursor":      permissionSourceCursor,
		"Copilot CLI": permissionSourceCopilotCLI,
		"Devin":       permissionSourceDevinCLI,
	}
	for in, want := range cases {
		got, err := resolvePauseTarget(in)
		if err != nil || got != want {
			t.Errorf("resolvePauseTarget(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
	if _, err := resolvePauseTarget("not-an-agent"); err == nil {
		t.Error("unknown agent must be rejected")
	}
}

func TestPermissionHookBypassesServerWhilePaused(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	clock := withPauseClock(t, time.Now())
	stderr := captureHookStderr(t)

	var calls int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()
	writeTestPermissionCredential(t, home, "codex-agent", permissionHookCredential{
		BaseURL: server.URL,
		Token:   "agt_test",
		Source:  permissionSourceCodexCLI,
	})
	raw := []byte(`{"tool_name":"Bash","tool_input":{"command":"ls"}}`)

	until, err := setApprovalsPause("", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	decision := resolvePermissionDecision(permissionSourceCodexCLI, raw, false)
	if decision.Behavior != hookBehaviorPass {
		t.Fatalf("paused hook should defer to the agent, got %q (%s)", decision.Behavior, decision.Reason)
	}
	if atomic.LoadInt32(&calls) != 0 {
		t.Fatal("paused hook must not call the server")
	}
	stamp := formatPauseUntil(until)
	if !strings.Contains(stderr.String(), "paused until") || !strings.Contains(stderr.String(), stamp) {
		t.Errorf("stderr note missing pause expiry %q: %q", stamp, stderr.String())
	}

	// Codex PermissionRequest: no decision lets Codex show its own prompt.
	rendered := renderHookDecision(permissionSourceCodexCLI, decision)
	if _, ok := rendered["hookSpecificOutput"]; ok {
		t.Errorf("paused Codex render must carry no decision: %v", rendered)
	}
	var buf bytes.Buffer
	if err := writeCodexPreToolUseDecision(&buf, decision); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(buf.String(), "deny") {
		t.Errorf("paused Codex PreToolUse must not deny: %s", buf.String())
	}
	claude := renderHookDecision(permissionSourceClaudeCode, decision)
	if _, ok := claude["hookSpecificOutput"]; ok {
		t.Errorf("paused Claude render must not override the local prompt: %v", claude)
	}
	if msg, _ := claude["systemMessage"].(string); !strings.Contains(msg, "paused") {
		t.Errorf("paused Claude render should tell the user: %v", claude)
	}
	for _, source := range []string{permissionSourceCursor, permissionSourceCopilotCLI, permissionSourceDevinCLI} {
		data, _ := json.Marshal(renderHookDecision(source, decision))
		if strings.Contains(string(data), "deny") || strings.Contains(string(data), "block") {
			t.Errorf("paused %s render must not deny: %s", source, data)
		}
	}

	// After expiry the hook goes back to the server and fails closed.
	*clock = clock.Add(time.Hour + time.Minute)
	decision = resolvePermissionDecision(permissionSourceCodexCLI, raw, false)
	if decision.Behavior != "deny" {
		t.Fatalf("expired pause must fail closed again, got %q", decision.Behavior)
	}
	if atomic.LoadInt32(&calls) == 0 {
		t.Fatal("expired pause must call the server again")
	}
}

func TestPermissionHookPauseIgnoredForFlowCredentials(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	withPauseClock(t, time.Now())
	captureHookStderr(t)
	t.Setenv("PRELOOP_FLOW_CREDENTIAL_REQUIRED", "1")
	if _, err := setApprovalsPause("", time.Hour); err != nil {
		t.Fatal(err)
	}
	raw := []byte(`{"tool_name":"Bash","tool_input":{"command":"ls"}}`)
	decision := resolvePermissionDecision(permissionSourceCodexCLI, raw, false)
	if decision.Behavior != "deny" {
		t.Fatalf("flow runs must stay governed while paused, got %q", decision.Behavior)
	}
}

func TestFailClosedDenyLeadsWithPauseCommand(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	writeTestPermissionCredential(t, home, "codex-agent", permissionHookCredential{
		BaseURL: "http://127.0.0.1:1",
		Token:   "agt_test",
		Source:  permissionSourceCodexCLI,
	})
	raw := []byte(`{"tool_name":"Bash","tool_input":{"command":"ls"}}`)
	decision := resolvePermissionDecision(permissionSourceCodexCLI, raw, false)
	if decision.Behavior != "deny" {
		t.Fatalf("expected deny, got %q", decision.Behavior)
	}
	pause := strings.Index(decision.Reason, "run `preloop agents pause`")
	repair := strings.Index(decision.Reason, "preloop agents onboard \"Codex CLI\" --approvals")
	if pause < 0 || repair < 0 || pause > repair {
		t.Errorf("deny must offer the pause command before the repair command:\n%s", decision.Reason)
	}
	if !strings.Contains(decision.Reason, "resumes automatically in 1h") {
		t.Errorf("deny must say the pause expires on its own:\n%s", decision.Reason)
	}
}

func TestDefaultPauseLabelMatchesDuration(t *testing.T) {
	if got, err := time.ParseDuration(defaultPauseLabel); err != nil || got != defaultPauseDuration {
		t.Fatalf("defaultPauseLabel %q does not match defaultPauseDuration %s", defaultPauseLabel, defaultPauseDuration)
	}
}

func TestAgentsPauseAndResumeCommands(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	withPauseClock(t, time.Date(2026, 10, 18, 12, 0, 0, 0, time.UTC))

	var out bytes.Buffer
	agentsPauseCmd.SetOut(&out)
	t.Cleanup(func() {
		agentsPauseCmd.SetOut(nil)
		_ = agentsPauseCmd.Flags().Set("for", defaultPauseDuration.String())
	})
	if err := agentsPauseCmd.Flags().Set("for", "30m"); err != nil {
		t.Fatal(err)
	}
	if err := runAgentsPause(agentsPauseCmd, []string{"Codex CLI"}); err != nil {
		t.Fatalf("pause: %v", err)
	}
	if !strings.Contains(out.String(), "paused") || !strings.Contains(out.String(), "preloop agents resume") {
		t.Errorf("pause output: %q", out.String())
	}
	if _, ok := activeApprovalsPause(permissionSourceCodexCLI); !ok {
		t.Fatal("pause command did not persist a pause")
	}
	if err := agentsPauseCmd.Flags().Set("for", "48h"); err != nil {
		t.Fatal(err)
	}
	if err := runAgentsPause(agentsPauseCmd, nil); err == nil {
		t.Error("pause longer than 24h must be rejected")
	}

	out.Reset()
	agentsResumeCmd.SetOut(&out)
	t.Cleanup(func() { agentsResumeCmd.SetOut(nil) })
	if err := runAgentsResume(agentsResumeCmd, []string{"codex"}); err != nil {
		t.Fatalf("resume: %v", err)
	}
	if _, ok := activeApprovalsPause(permissionSourceCodexCLI); ok {
		t.Fatal("resume command did not clear the pause")
	}
	if !strings.Contains(out.String(), "resumed") {
		t.Errorf("resume output: %q", out.String())
	}
}

func TestApprovalsPauseStatusLine(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	withPauseClock(t, time.Date(2026, 10, 18, 12, 0, 0, 0, time.UTC))
	if line := approvalsPauseStatusLine(permissionSourceCodexCLI); line != "" {
		t.Errorf("no pause should print nothing, got %q", line)
	}
	if _, err := setApprovalsPause("", time.Hour); err != nil {
		t.Fatal(err)
	}
	for _, source := range []string{permissionSourceOpenCode, "pi", ""} {
		if line := approvalsPauseStatusLine(source); line != "" {
			t.Errorf("%q does not read the pause marker; status must not claim a pause: %q", source, line)
		}
	}
	line := approvalsPauseStatusLine(permissionSourceCodexCLI)
	if !strings.Contains(line, "Approvals: paused until") {
		t.Errorf("status line = %q", line)
	}
}
