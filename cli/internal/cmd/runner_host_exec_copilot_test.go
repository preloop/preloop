package cmd

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"unicode/utf8"

	"github.com/preloop/preloop/cli/internal/testenv"
)

const copilotTestExecID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

// Lines captured from Copilot CLI 1.0.88 `--output-format=json`, trimmed to
// the fields the runner reads.
const copilotSuccessStream = `{"type":"user.message","data":{"content":"say ok"},"id":"1"}
{"type":"assistant.message_delta","data":{"messageId":"m1","deltaContent":"ok"},"ephemeral":true,"id":"2"}
{"type":"assistant.message","data":{"messageId":"m1","model":"claude-sonnet-4.6","content":"ok"},"id":"3"}
{"type":"assistant.turn_end","data":{"turnId":"0"},"id":"4"}
{"type":"result","timestamp":"2026-09-27T00:01:32.264Z","sessionId":"33810b72-1e9a-4a02-bbcb-a125d10b886c","exitCode":0,"usage":{"premiumRequests":1,"totalApiDurationMs":1504,"sessionDurationMs":3717}}`

// setupCopilotHost isolates HOME and COPILOT_HOME, installs a fake copilot
// binary on PATH, and returns the Copilot home directory.
func setupCopilotHost(t *testing.T, body string) string {
	t.Helper()
	skipNoShebangOnWindows(t, "copilot host execution fake CLI")
	testenv.SetTempHome(t)
	copilotHome := t.TempDir()
	t.Setenv("COPILOT_HOME", copilotHome)
	dir := t.TempDir()
	script := "#!/bin/sh\n" + body + "\n"
	if err := os.WriteFile(filepath.Join(dir, "copilot"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return copilotHome
}

func copilotJob(extra map[string]any) map[string]any {
	job := map[string]any{
		"agent_type":          "copilot",
		"completion_protocol": "host_exec",
		"host_exec_profile":   "copilot-seat",
		"execution_id":        copilotTestExecID,
		"prompt":              "review the change",
	}
	for key, value := range extra {
		job[key] = value
	}
	return job
}

func runCopilotHostJob(t *testing.T, job map[string]any) (leasedJobOutcome, *runnerLogBuffer) {
	t.Helper()
	cmd, _, timeout, err := newHostExecJobCmd(job)
	if err != nil {
		t.Fatal(err)
	}
	buffer := &runnerLogBuffer{native: true, harness: hostExecHarnessCopilot}
	cmd.Stdout, cmd.Stderr = buffer, buffer
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	outcome := waitHostExecJob(cmd, copilotTestExecID, buffer, nil, timeout, "copilot-seat")
	if outcome.status == "FAILED" {
		outcome.errMsg = copilotHostExecFailure(buffer, "copilot-seat", job, outcome.errMsg)
	}
	return outcome, buffer
}

func TestNormalizeCopilotHostExecProfile(t *testing.T) {
	root := t.TempDir()
	base := hostExecProfile{Name: "copilot-seat", Executable: "copilot", WorkspaceRoot: root}
	if _, err := normalizeHostExecProfile(base); err != nil {
		t.Fatalf("plain copilot profile rejected: %v", err)
	}
	cases := map[string]hostExecProfile{
		"force_writes": func() hostExecProfile { p := base; p.ForceWrites = true; return p }(),
		"--allow-all":  func() hostExecProfile { p := base; p.Argv = []string{"--allow-all"}; return p }(),
		"--yolo":       func() hostExecProfile { p := base; p.Argv = []string{"--yolo"}; return p }(),
		"--model":      func() hostExecProfile { p := base; p.Argv = []string{"--model=gpt-5"}; return p }(),
		"--agent":      func() hostExecProfile { p := base; p.Argv = []string{"--agent", "x"}; return p }(),
		"-p":           func() hostExecProfile { p := base; p.Argv = []string{"-p", "x"}; return p }(),
		"flag rule":    func() hostExecProfile { p := base; p.AllowTools = []string{"--allow-all-tools"}; return p }(),
		"empty rule":   func() hostExecProfile { p := base; p.DenyTools = []string{" "}; return p }(),
	}
	for name, profile := range cases {
		if _, err := normalizeHostExecProfile(profile); err == nil {
			t.Errorf("%s: expected rejection", name)
		}
	}
	cursor := hostExecProfile{Name: "cursor-ask", Executable: "cursor-agent", WorkspaceRoot: root, AllowTools: []string{"write"}}
	if _, err := normalizeHostExecProfile(cursor); err == nil || !strings.Contains(err.Error(), "copilot") {
		t.Fatalf("cursor profile with allow_tools: err = %v", err)
	}
}

func TestCopilotHostExecAdvertisesCopilotCapability(t *testing.T) {
	writeHostExecProfiles(t, []hostExecProfile{{
		Name:          "copilot-seat",
		Executable:    "copilot",
		WorkspaceRoot: t.TempDir(),
		ModelMap:      map[string]string{"claude-sonnet-4.6": "claude-sonnet-4.6"},
	}})
	ads := hostExecAdvertisements()
	if len(ads) != 1 {
		t.Fatalf("ads = %#v", ads)
	}
	caps := strings.Join(ads[0].Capabilities, ",")
	if !strings.Contains(caps, "copilot_cli") || strings.Contains(caps, "cursor_cli") {
		t.Fatalf("capabilities = %s", caps)
	}
	if len(ads[0].Models) != 1 || ads[0].Models[0] != "claude-sonnet-4.6" {
		t.Fatalf("models = %v", ads[0].Models)
	}
}

func TestCopilotHostExecCommandLineEnvAndHooks(t *testing.T) {
	probe := t.TempDir()
	copilotHome := setupCopilotHost(t, `
printf '%s\n' "$@" > "$PRELOOP_HOST_EXEC_PROBE/argv"
env > "$PRELOOP_HOST_EXEC_PROBE/env"
pwd > "$PRELOOP_HOST_EXEC_PROBE/cwd"
echo '{"type":"result","sessionId":"ses-1","exitCode":0}'
`)
	t.Setenv("PRELOOP_HOST_EXEC_PROBE", probe)
	t.Setenv("COPILOT_PROVIDER_BASE_URL", "https://gateway.example.com/openai/v1")
	t.Setenv("COPILOT_PROVIDER_API_KEY", "byok-key")
	t.Setenv("COPILOT_ALLOW_ALL", "true")
	t.Setenv("COPILOT_GITHUB_TOKEN", "seat-login")
	t.Setenv("PRELOOP_TOKEN", "runner-credential")
	writeHostExecProfiles(t, []hostExecProfile{{
		Name:          "copilot-seat",
		Executable:    "copilot",
		WorkspaceRoot: t.TempDir(),
		ModelMap:      map[string]string{"team-default": "claude-sonnet-4.6"},
		AllowTools:    []string{"write", "shell(git:*)"},
		DenyTools:     []string{"shell(git push)"},
		PassEnv:       []string{"PRELOOP_HOST_EXEC_PROBE"},
	}})
	outcome, _ := runCopilotHostJob(t, copilotJob(map[string]any{
		"prompt":           "-starts with a dash",
		"model_identifier": "team-default",
	}))
	if outcome.status != "SUCCEEDED" {
		t.Fatalf("status=%s err=%s", outcome.status, outcome.errMsg)
	}
	argvRaw, err := os.ReadFile(filepath.Join(probe, "argv"))
	if err != nil {
		t.Fatal(err)
	}
	argv := strings.Split(strings.TrimSpace(string(argvRaw)), "\n")
	want := []string{
		"--prompt=-starts with a dash", "-s", "--no-ask-user", "--output-format=json",
		"--model=claude-sonnet-4.6", "--allow-tool=write", "--allow-tool=shell(git:*)",
		"--deny-tool=shell(git push)",
	}
	if strings.Join(argv, "|") != strings.Join(want, "|") {
		t.Fatalf("argv = %q\nwant  %q", argv, want)
	}
	envRaw, err := os.ReadFile(filepath.Join(probe, "env"))
	if err != nil {
		t.Fatal(err)
	}
	env := string(envRaw)
	for _, banned := range []string{
		"COPILOT_PROVIDER_BASE_URL", "COPILOT_PROVIDER_API_KEY",
		"COPILOT_ALLOW_ALL", "PRELOOP_TOKEN",
	} {
		if strings.Contains(env, banned+"=") {
			t.Fatalf("%s leaked into the Copilot environment", banned)
		}
	}
	if !strings.Contains(env, "COPILOT_GITHUB_TOKEN=seat-login") {
		t.Fatal("the runner user's Copilot login must be preserved")
	}
	cwd, err := os.ReadFile(filepath.Join(probe, "cwd"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(cwd), filepath.Join(hostExecWorkspaceDir, copilotTestExecID)) {
		t.Fatalf("cwd = %s", cwd)
	}
	hooksRaw, err := os.ReadFile(filepath.Join(copilotHome, "hooks", "preloop.json"))
	if err != nil {
		t.Fatalf("Preloop hook file not installed: %v", err)
	}
	var hooks struct {
		Hooks map[string][]map[string]any `json:"hooks"`
	}
	if err := json.Unmarshal(hooksRaw, &hooks); err != nil {
		t.Fatal(err)
	}
	for _, key := range copilotUsageHookEvents {
		entries := hooks.Hooks[key]
		if len(entries) != 1 || !strings.Contains(entries[0]["bash"].(string), "usage hook --from copilot") {
			t.Fatalf("hook %s = %v", key, entries)
		}
	}
	if _, ok := hooks.Hooks["preToolUse"]; ok {
		t.Fatal("the runner must not invent an approval hook")
	}
}

func TestCopilotHostExecKeepsOtherHookEntries(t *testing.T) {
	copilotHome := setupCopilotHost(t, `echo '{"type":"result","sessionId":"s","exitCode":0}'`)
	hooksDir := filepath.Join(copilotHome, "hooks")
	if err := os.MkdirAll(hooksDir, 0o700); err != nil {
		t.Fatal(err)
	}
	other := filepath.Join(hooksDir, "team.json")
	if err := os.WriteFile(other, []byte(`{"version":1,"hooks":{"sessionStart":[{"type":"command","bash":"echo team"}]}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	writeHostExecProfiles(t, []hostExecProfile{{Name: "copilot-seat", Executable: "copilot", WorkspaceRoot: t.TempDir()}})
	if outcome, _ := runCopilotHostJob(t, copilotJob(nil)); outcome.status != "SUCCEEDED" {
		t.Fatalf("status=%s err=%s", outcome.status, outcome.errMsg)
	}
	raw, err := os.ReadFile(other)
	if err != nil || !strings.Contains(string(raw), "echo team") {
		t.Fatalf("operator hook file changed: %s %v", raw, err)
	}
}

func TestCopilotHostExecAllowAllToolsRequiresApprovalHook(t *testing.T) {
	probe := t.TempDir()
	copilotHome := setupCopilotHost(t, `
printf '%s\n' "$@" > "$PRELOOP_HOST_EXEC_PROBE/argv"
echo '{"type":"result","sessionId":"s","exitCode":0}'
`)
	t.Setenv("PRELOOP_HOST_EXEC_PROBE", probe)
	writeHostExecProfiles(t, []hostExecProfile{{
		Name:          "copilot-seat",
		Executable:    "copilot",
		WorkspaceRoot: t.TempDir(),
		AllowAllTools: true,
		AllowTools:    []string{"write"},
		PassEnv:       []string{"PRELOOP_HOST_EXEC_PROBE"},
	}})
	if _, _, _, err := newHostExecJobCmd(copilotJob(nil)); err == nil || !strings.Contains(err.Error(), "copilot_approval_hook_missing") {
		t.Fatalf("err = %v", err)
	}
	// Entries Copilot never executes on this OS do not satisfy the gate: a
	// powershell entry on POSIX, or a generic command key anywhere.
	for i, key := range []string{"powershell", "command"} {
		dead := map[string]any{
			"version": 1,
			"hooks": map[string]any{
				"preToolUse": []any{map[string]any{"type": "command", key: "preloop agents permission-hook --source copilot_cli"}},
			},
		}
		raw, _ := json.Marshal(dead)
		if err := os.WriteFile(filepath.Join(copilotHome, "hooks", "preloop.json"), raw, 0o600); err != nil {
			t.Fatal(err)
		}
		job := copilotJob(map[string]any{"execution_id": fmt.Sprintf("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee%d", i)})
		if _, _, _, err := newHostExecJobCmd(job); err == nil || !strings.Contains(err.Error(), "copilot_approval_hook_missing") {
			t.Fatalf("%s-keyed hook accepted: err = %v", key, err)
		}
	}
	approval := map[string]any{
		"version": 1,
		"hooks": map[string]any{
			"preToolUse": []any{map[string]any{"type": "command", "bash": "preloop agents permission-hook --source copilot_cli"}},
		},
	}
	raw, _ := json.Marshal(approval)
	if err := os.WriteFile(filepath.Join(copilotHome, "hooks", "preloop.json"), raw, 0o600); err != nil {
		t.Fatal(err)
	}
	job := copilotJob(map[string]any{"execution_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd"})
	cmd, _, _, err := newHostExecJobCmd(job)
	if err != nil {
		t.Fatalf("approval hook installed, err = %v", err)
	}
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("run: %v %s", err, out)
	}
	argv, _ := os.ReadFile(filepath.Join(probe, "argv"))
	if !strings.Contains(string(argv), "--allow-all-tools") || strings.Contains(string(argv), "--allow-tool=") {
		t.Fatalf("argv = %s", argv)
	}
	after, _ := os.ReadFile(filepath.Join(copilotHome, "hooks", "preloop.json"))
	if !strings.Contains(string(after), "permission-hook") {
		t.Fatal("usage hook install removed the approval hook")
	}
}

func TestCopilotHostExecHarnessMustMatchProfile(t *testing.T) {
	setupCopilotHost(t, `exit 0`)
	writeHostExecProfiles(t, []hostExecProfile{
		{Name: "copilot-seat", Executable: "copilot", WorkspaceRoot: t.TempDir()},
		{Name: "cursor-ask", Executable: "cursor-agent", WorkspaceRoot: t.TempDir()},
	})
	if _, _, _, err := newHostExecJobCmd(copilotJob(map[string]any{"agent_type": "cursor"})); err == nil || !strings.Contains(err.Error(), "copilot_cli") {
		t.Fatalf("cursor lease on copilot profile: err = %v", err)
	}
	if _, _, _, err := newHostExecJobCmd(copilotJob(map[string]any{"host_exec_profile": "cursor-ask"})); err == nil || !strings.Contains(err.Error(), "cursor_cli") {
		t.Fatalf("copilot lease on cursor profile: err = %v", err)
	}
	if _, _, _, err := newHostExecJobCmd(copilotJob(map[string]any{"agent_type": "codex"})); err == nil {
		t.Fatal("unknown agent type must fail closed")
	}
	if got := jobRejectedHostExecInjection(copilotJob(map[string]any{"copilot_github_token": "x"})); !strings.Contains(got, "copilot_github_token") {
		t.Fatalf("got %q", got)
	}
}

func TestCopilotHostExecStructuredSuccess(t *testing.T) {
	setupCopilotHost(t, "cat <<'EOF'\n"+copilotSuccessStream+"\nEOF")
	writeHostExecProfiles(t, []hostExecProfile{{Name: "copilot-seat", Executable: "copilot", WorkspaceRoot: t.TempDir()}})
	outcome, buffer := runCopilotHostJob(t, copilotJob(nil))
	if outcome.status != "SUCCEEDED" || outcome.exitCode != 0 {
		t.Fatalf("status=%s exit=%d err=%s", outcome.status, outcome.exitCode, outcome.errMsg)
	}
	result := outcome.result
	if result["harness"] != "copilot_cli" || result["status"] != "success" {
		t.Fatalf("result = %v", result)
	}
	if result["session_id"] != "33810b72-1e9a-4a02-bbcb-a125d10b886c" || result["model"] != "claude-sonnet-4.6" {
		t.Fatalf("result = %v", result)
	}
	if result["premium_requests"] != float64(1) {
		t.Fatalf("premium_requests = %v", result["premium_requests"])
	}
	if _, ok := result["input_tokens"]; ok {
		t.Fatal("tokens must not be invented")
	}
	for _, line := range buffer.pending {
		if strings.Contains(line, "message_delta") {
			t.Fatalf("delta event kept in logs: %s", line)
		}
	}
	if len(buffer.pending) != 4 {
		t.Fatalf("pending logs = %d", len(buffer.pending))
	}
}

func TestCopilotHostExecFailures(t *testing.T) {
	cases := []struct {
		name   string
		script string
		job    map[string]any
		want   string
	}{
		{"exit zero without result", `echo '{"type":"assistant.turn_end"}'; exit 0`, nil, "structured completion"},
		{"result with nonzero exitCode", `echo '{"type":"result","sessionId":"s","exitCode":1}'; exit 0`, nil, "structured failure"},
		{"duplicate result", `echo '{"type":"result","sessionId":"s","exitCode":0}'; echo '{"type":"result","sessionId":"s","exitCode":0}'`, nil, "structured completion"},
		{"not logged in", `echo 'Error: No authentication information found.' >&2; exit 1`, nil, "copilot_not_logged_in"},
		{
			"model not on seat",
			`echo 'Error: Model "gpt-9" from --model flag is not available.' >&2; exit 1`,
			map[string]any{"model_identifier": "team-top"},
			`copilot_model_unavailable: Copilot model "gpt-9" (requested "team-top") is not available to this runner's Copilot seat; models mapped by profile "copilot-seat": claude-haiku-4.5, gpt-9`,
		},
		{"other startup error", `echo 'Error: something else' >&2; exit 3`, nil, "exit status 3: Error: something else"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			setupCopilotHost(t, tc.script)
			writeHostExecProfiles(t, []hostExecProfile{{
				Name:          "copilot-seat",
				Executable:    "copilot",
				WorkspaceRoot: t.TempDir(),
				ModelMap:      map[string]string{"team-top": "gpt-9", "team-fast": "claude-haiku-4.5"},
			}})
			outcome, _ := runCopilotHostJob(t, copilotJob(tc.job))
			if outcome.status != "FAILED" || !strings.Contains(outcome.errMsg, tc.want) {
				t.Fatalf("status=%s err=%q want %q", outcome.status, outcome.errMsg, tc.want)
			}
		})
	}
}

func TestCopilotHostExecRequiresPrompt(t *testing.T) {
	setupCopilotHost(t, `exit 0`)
	writeHostExecProfiles(t, []hostExecProfile{{Name: "copilot-seat", Executable: "copilot", WorkspaceRoot: t.TempDir()}})
	if _, _, _, err := newHostExecJobCmd(copilotJob(map[string]any{"prompt": "  "})); err == nil || !strings.Contains(err.Error(), "prompt") {
		t.Fatalf("err = %v", err)
	}
}

func TestCopilotStreamParserIgnoresUntrustedFields(t *testing.T) {
	var capture copilotCapture
	applyCopilotLine(&capture, `{"type":"assistant.message","data":{"model":"bad model; rm -rf"}}`)
	applyCopilotLine(&capture, `{"type":"result","sessionId":"bad id with spaces","exitCode":0,"usage":{"premiumRequests":-4}}`)
	if capture.Model != "" || capture.SessionID != "" || capture.PremiumRequests != nil {
		t.Fatalf("capture = %+v", capture)
	}
	result, err := copilotRunnerResult(capture)
	if err != nil || result["status"] != "success" {
		t.Fatalf("result=%v err=%v", result, err)
	}
}

func TestCopilotHostExecHooksAreIdempotentAndReplacedAtomically(t *testing.T) {
	testenv.SetTempHome(t)
	copilotHome := t.TempDir()
	t.Setenv("COPILOT_HOME", copilotHome)
	path := filepath.Join(copilotHome, "hooks", "preloop.json")
	if err := ensureCopilotHostExecUsageHooks(); err != nil {
		t.Fatal(err)
	}
	first, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" && first.Mode().Perm() != 0o600 {
		t.Fatalf("mode = %v", first.Mode().Perm())
	}
	// Steady state: an unchanged document is not rewritten, so a Copilot
	// process from a concurrent job never sees the file replaced.
	if err := ensureCopilotHostExecUsageHooks(); err != nil {
		t.Fatal(err)
	}
	second, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(first, second) || !second.ModTime().Equal(first.ModTime()) {
		t.Fatal("unchanged hooks file was rewritten")
	}

	if runtime.GOOS == "windows" {
		// Windows cannot rename over a file a reader holds open, so the
		// concurrent replacement check below is Unix-only. Steady-state
		// (unchanged file, no rewrite) is covered above on every OS.
		return
	}
	// Concurrent jobs racing with a reader: every read parses in full.
	stale := []byte(`{"version":1,"hooks":{"preToolUse":[{"type":"command","bash":"preloop agents permission-hook --source copilot_cli"}]}}` + "\n")
	var wg sync.WaitGroup
	stop := make(chan struct{})
	readErr := make(chan error, 1)
	go func() {
		for {
			select {
			case <-stop:
				close(readErr)
				return
			default:
			}
			raw, err := os.ReadFile(path)
			if err != nil {
				continue
			}
			var doc map[string]any
			if err := json.Unmarshal(raw, &doc); err != nil {
				readErr <- fmt.Errorf("reader saw a partial hooks file: %v: %q", err, raw)
				close(readErr)
				return
			}
		}
	}()
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 20; j++ {
				copilotHooksMu.Lock()
				err := writeFileAtomic(path, stale, 0o600)
				copilotHooksMu.Unlock()
				if err != nil {
					t.Error(err)
					return
				}
				if err := ensureCopilotHostExecUsageHooks(); err != nil {
					t.Error(err)
					return
				}
			}
		}()
	}
	wg.Wait()
	close(stop)
	if err := <-readErr; err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), "permission-hook") || !strings.Contains(string(raw), "usage hook --from copilot") {
		t.Fatalf("final hooks = %s", raw)
	}
	entries, err := os.ReadDir(filepath.Dir(path))
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if entry.Name() != "preloop.json" {
			t.Fatalf("leftover file %s", entry.Name())
		}
	}
}

func TestCopilotHostExecRejectsInjectedDenyTools(t *testing.T) {
	if got := jobRejectedHostExecInjection(copilotJob(map[string]any{"deny_tools": []string{"write"}})); !strings.Contains(got, "deny_tools") {
		t.Fatalf("got %q", got)
	}
}

func TestCopilotErrorLineTruncatesOnRuneBoundary(t *testing.T) {
	line := "Error: " + strings.Repeat("a", copilotMaxErrorBytes-8) + "é and more"
	var capture copilotCapture
	applyCopilotLine(&capture, line)
	if len(capture.ErrorLine) > copilotMaxErrorBytes || !utf8.ValidString(capture.ErrorLine) {
		t.Fatalf("error line = %d bytes, valid=%v", len(capture.ErrorLine), utf8.ValidString(capture.ErrorLine))
	}
	if !strings.HasSuffix(capture.ErrorLine, "a") {
		t.Fatalf("suffix = %q", capture.ErrorLine[len(capture.ErrorLine)-4:])
	}
	if got := truncateUTF8("ok", 10); got != "ok" {
		t.Fatalf("short string changed: %q", got)
	}
}
