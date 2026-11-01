package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// Claude Code host execution (issue #1668). A private runner runs the locally
// installed `claude` in print mode under the operator's existing login
// (`claude auth login`, a console API key, or ANTHROPIC_* variables the
// operator exported), so the run uses that account. The binary is never
// modified, no file under ~/.claude is written, and no credential is read:
// login is verified with `claude auth status --json`, keeping only the
// loggedIn verdict.
//
// Verified against Claude Code 2.1.296 (`claude --help`, `claude auth status
// --help`). Every argument is runner-owned: Claude Code has many flags that
// change permissions or load settings (--dangerously-skip-permissions,
// --permission-mode, --settings, --plugin-dir, --add-dir), so a profile
// cannot add argv. Tool approval comes from the Preloop PreToolUse hook that
// `preloop agents onboard "Claude Code" --approvals` installs; without it the
// job fails before Claude starts.

const (
	hostExecHarnessClaude = "claude_code"

	claudeMaxErrorBytes    = 512
	claudeAuthProbeTimeout = 10 * time.Second
)

var (
	hostExecClaudeNames = map[string]struct{}{
		"claude": {},
	}
	// claudeTokenEnvNames are the variables Claude Code reads a credential
	// from. Only their presence is checked.
	claudeTokenEnvNames = []string{"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"}
)

func hostExecIsClaudeBinary(executable string) bool {
	_, ok := hostExecClaudeNames[hostExecBinaryBase(executable)]
	return ok
}

// validateClaudeHostExecProfile keeps Claude profiles declarative: the
// runner owns every argument and Preloop approvals gate every tool.
func validateClaudeHostExecProfile(profile hostExecProfile) error {
	if profile.ForceWrites || profile.AllowAllTools || len(profile.AllowTools) > 0 || len(profile.DenyTools) > 0 {
		return fmt.Errorf("Claude Code tools require the Preloop approval hook; tool grants and force_writes are unavailable")
	}
	if len(profile.Argv) > 0 {
		return fmt.Errorf("Claude Code profile argv is unavailable; use model_map and Preloop approvals")
	}
	return nil
}

// claudeHostExecPrompt returns the prompt Claude reads on stdin. Stdin keeps
// the prompt out of argv (no flag parsing, no process-list exposure, no
// Windows command-line limit).
func claudeHostExecPrompt(job map[string]any) (string, error) {
	prompt, err := jobPromptText(job)
	if err != nil {
		return "", err
	}
	if strings.TrimSpace(prompt) == "" || strings.ContainsRune(prompt, 0) {
		return "", fmt.Errorf("Claude Code requires a nonempty prompt without NUL")
	}
	return prompt, nil
}

// buildClaudeHostExecArgs renders `claude -p --output-format stream-json
// --verbose [--model alias] [--mcp-config path --allowedTools mcp__preloop-flow]
// [--resume id]`. The prompt is delivered on stdin.
func buildClaudeHostExecArgs(profile hostExecProfile, job map[string]any, mcpConfig, resume string) ([]string, error) {
	if err := validateClaudeHostExecProfile(profile); err != nil {
		return nil, err
	}
	if _, err := claudeHostExecPrompt(job); err != nil {
		return nil, err
	}
	// stream-json in print mode requires --verbose.
	args := []string{"-p", "--output-format", "stream-json", "--verbose"}
	if requested := jobModelIdentifier(job); requested != "" {
		alias := profile.ModelMap[requested]
		if alias == "" {
			return nil, fmt.Errorf("model not in local model_map")
		}
		args = append(args, "--model", alias)
	}
	if mcpConfig != "" {
		// The flow's Preloop MCP server is filtered server-side to the
		// flow's allowed tools; print mode cannot prompt, so grant the
		// server here. The PreToolUse approval hook still runs first.
		args = append(args, "--mcp-config", mcpConfig, "--allowedTools", "mcp__"+hostExecMCPServerName)
	}
	if resume != "" {
		if !uuidRe.MatchString(resume) {
			return nil, fmt.Errorf("invalid Claude Code session id")
		}
		args = append(args, "--resume", strings.ToLower(resume))
	}
	return args, nil
}

// claudeConfigDir is $CLAUDE_CONFIG_DIR or ~/.claude.
func claudeConfigDir() (string, error) {
	if dir := strings.TrimSpace(os.Getenv("CLAUDE_CONFIG_DIR")); dir != "" {
		return dir, nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".claude"), nil
}

// claudeApprovalHookInstalled reports whether the Preloop PreToolUse
// approval hook is in the user settings (or the managed settings) Claude
// Code loads (settings.json, settings.local.json, managed settings). Only
// the hooks block of settings files is read; credential files are never
// opened.
func claudeApprovalHookInstalled() bool {
	paths := []string{claudeManagedSettingsPath()}
	if dir, err := claudeConfigDir(); err == nil {
		paths = append([]string{filepath.Join(dir, "settings.json"), filepath.Join(dir, "settings.local.json")}, paths...)
	}
	for _, path := range paths {
		doc, err := loadJSONDocumentOrEmpty(path)
		if err != nil {
			continue
		}
		if claudeHooksHaveApproval(doc) {
			return true
		}
	}
	return false
}

func claudeHooksHaveApproval(doc map[string]interface{}) bool {
	hooks, _ := doc["hooks"].(map[string]interface{})
	for _, raw := range asArrayValue(hooks["PreToolUse"]) {
		entry, _ := asObjectMap(raw)
		matcher, _ := entry["matcher"].(string)
		if matcher != "" && matcher != "*" && matcher != ".*" {
			continue
		}
		for _, rawHook := range asArrayValue(entry["hooks"]) {
			hook, _ := asObjectMap(rawHook)
			command, _ := hook["command"].(string)
			if hook["type"] == "command" && strings.Contains(command, "agents permission-hook --source "+permissionSourceClaudeCode) {
				return true
			}
		}
	}
	return false
}

// claudeHostExecEnvAllowed keeps the variables that carry Claude Code's own
// login and configuration.
func claudeHostExecEnvAllowed(key string) bool {
	return strings.HasPrefix(key, "ANTHROPIC_") || strings.HasPrefix(key, "CLAUDE_CODE_") || key == "CLAUDE_CONFIG_DIR"
}

// claudeCapture is the bounded state read from Claude's stream-json output.
// Only identifiers, counts and token totals are kept.
type claudeCapture struct {
	SessionID    string
	Model        string
	Results      int
	ResultErr    bool
	Subtype      string
	NumTurns     *int
	InputTokens  *int64
	OutputTokens *int64
	ErrorLine    string
	// MessageIDs are the distinct assistant message ids seen: one per model
	// response, so their count is the CLI-observed model call count.
	MessageIDs map[string]struct{}
}

type claudeStreamEvent struct {
	Type      string `json:"type"`
	Subtype   string `json:"subtype"`
	SessionID string `json:"session_id"`
	Model     string `json:"model"`
	IsError   bool   `json:"is_error"`
	NumTurns  *int   `json:"num_turns"`
	Message   *struct {
		ID string `json:"id"`
	} `json:"message"`
	Usage *struct {
		InputTokens  *int64 `json:"input_tokens"`
		OutputTokens *int64 `json:"output_tokens"`
	} `json:"usage"`
}

// applyClaudeLine records one output line. Every line is kept in the log.
func applyClaudeLine(capture *claudeCapture, line string) {
	trimmed := strings.TrimSpace(line)
	if trimmed == "" {
		return
	}
	if trimmed[0] != '{' {
		// Startup failures (no login, unknown model) print as plain text.
		if capture.ErrorLine == "" {
			capture.ErrorLine = truncateUTF8(trimmed, claudeMaxErrorBytes)
		}
		return
	}
	var event claudeStreamEvent
	if json.Unmarshal([]byte(trimmed), &event) != nil {
		return
	}
	if uuidRe.MatchString(event.SessionID) {
		capture.SessionID = strings.ToLower(event.SessionID)
	}
	switch event.Type {
	case "system":
		if event.Subtype == "init" && hostExecModelRe.MatchString(event.Model) {
			capture.Model = event.Model
		}
	case "assistant":
		if event.Message != nil && event.Message.ID != "" {
			if capture.MessageIDs == nil {
				capture.MessageIDs = map[string]struct{}{}
			}
			capture.MessageIDs[event.Message.ID] = struct{}{}
		}
	case "result":
		capture.Results++
		capture.Subtype = event.Subtype
		capture.ResultErr = event.IsError || event.Subtype != "success"
		capture.NumTurns = event.NumTurns
		if event.Usage != nil {
			capture.InputTokens = event.Usage.InputTokens
			capture.OutputTokens = event.Usage.OutputTokens
		}
	}
}

// claudeRunnerResult requires exactly one terminal `result` event. A result
// with is_error or a non-success subtype is a structured failure.
func claudeRunnerResult(capture claudeCapture) (map[string]any, error) {
	if capture.Results != 1 {
		return nil, fmt.Errorf("host execution exited without a valid structured completion result")
	}
	status := "success"
	if capture.ResultErr {
		status = "failure"
	}
	result := map[string]any{"status": status, "harness": hostExecHarnessClaude}
	if capture.SessionID != "" {
		result["session_id"] = capture.SessionID
	}
	if capture.Model != "" {
		result["model"] = capture.Model
	}
	if capture.NumTurns != nil && *capture.NumTurns >= 0 {
		result["num_turns"] = *capture.NumTurns
	}
	if len(capture.MessageIDs) > 0 {
		// Distinct model responses the CLI received; the control plane
		// compares this with the execution's gateway rows.
		result["cli_model_calls"] = len(capture.MessageIDs)
		result["cli_model_calls_kind"] = "exact"
	}
	usage := map[string]any{}
	if capture.InputTokens != nil && *capture.InputTokens >= 0 {
		usage["input_tokens"] = *capture.InputTokens
	}
	if capture.OutputTokens != nil && *capture.OutputTokens >= 0 {
		usage["output_tokens"] = *capture.OutputTokens
	}
	if len(usage) > 0 {
		result["usage"] = usage
	}
	if status == "success" {
		noteHarnessRunSucceeded(hostExecHarnessClaude, capture.Model)
	}
	return result, nil
}

// claudeHostExecFailure names Claude's plain-text startup errors.
func claudeHostExecFailure(buffer *runnerLogBuffer, fallback string) string {
	buffer.mu.Lock()
	line := buffer.claudeCapture.ErrorLine
	subtype := buffer.claudeCapture.Subtype
	buffer.mu.Unlock()
	lower := strings.ToLower(line)
	switch {
	case strings.Contains(lower, "not logged in") || strings.Contains(lower, "please run /login") || strings.Contains(lower, "invalid api key"):
		noteHarnessSignedOut(hostExecHarnessClaude)
		return "claude_not_logged_in: Claude Code on this runner has no usable login; run `claude auth login` as the runner user"
	case strings.Contains(lower, "model") && (strings.Contains(lower, "not found") || strings.Contains(lower, "not available") || strings.Contains(lower, "invalid")):
		return "claude_model_unavailable: " + line
	case subtype == "error_max_turns":
		return "claude_max_turns: Claude Code stopped at its turn limit"
	case line != "":
		if fallback == "" {
			return line
		}
		return fallback + ": " + line
	default:
		return fallback
	}
}

// probeClaudeLogin runs `claude auth status --json` and keeps only the
// loggedIn verdict. The rest of the output (which may name the account) is
// discarded.
func probeClaudeLogin(executable string) string {
	ctx, cancel := context.WithTimeout(context.Background(), claudeAuthProbeTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, executable, "auth", "status", "--json")
	cmd.Env = hostExecProbeEnv(executable)
	out := &boundedTail{max: 64 * 1024}
	cmd.Stdout = out
	_ = cmd.Run()
	if ctx.Err() != nil {
		return harnessLoginUnknown
	}
	return claudeLoginFromStatus(out.String())
}

func claudeLoginFromStatus(output string) string {
	var status struct {
		LoggedIn *bool `json:"loggedIn"`
	}
	if json.Unmarshal([]byte(strings.TrimSpace(output)), &status) != nil || status.LoggedIn == nil {
		return harnessLoginUnknown
	}
	if *status.LoggedIn {
		return harnessLoginSignedIn
	}
	return harnessLoginSignedOut
}

// buildClaudeEntry reports Claude Code. It runs flows and sessions only when the
// executable resolves and the login is verified; otherwise it is
// presence-only so the console does not offer a runner path that cannot run.
func buildClaudeEntry(d harnessInventoryDeps, discovered map[string]AgentConfig) (harnessInventoryEntry, bool) {
	const id = hostExecHarnessClaude
	agent, wasDiscovered := discovered[id]
	if d.Resolve == nil {
		return harnessInventoryEntry{}, false
	}
	executable, err := d.Resolve("claude")
	if err != nil && !wasDiscovered {
		return harnessInventoryEntry{}, false
	}
	entry := harnessInventoryEntry{
		Harness:      id,
		DisplayName:  harnessDisplayNames[id],
		LoginState:   harnessLoginUnknown,
		LoginSource:  "unknown",
		Governance:   "ungoverned",
		SupportLevel: harnessSupportPresenceOnly,
		SessionMode:  "none",
		Billing:      "unknown",
	}
	if wasDiscovered {
		entry.Governance = discoveryGovernance(agent)
	}
	if d.ClaudeHook != nil {
		switch {
		case d.ClaudeHook():
			entry.Governance = "governed"
		case entry.Governance == "governed":
			// Onboarded but without the approval hook: runs fail closed.
			entry.Governance = "partial"
		}
	}
	if err == nil {
		if d.Version != nil {
			entry.Version = d.Version(executable)
		}
		for _, name := range claudeTokenEnvNames {
			if d.HasEnv(name) {
				entry.LoginState, entry.LoginSource = harnessLoginSignedIn, "env"
				break
			}
		}
		if entry.LoginSource != "env" && d.ClaudeLogin != nil {
			entry.LoginState = d.ClaudeLogin(executable)
			if entry.LoginState != harnessLoginUnknown {
				entry.LoginSource = "cli_status"
			}
		}
	}
	// Only a credential variable or `claude auth status` verifies the
	// login. Decide before the discovery fallback below, whose source is
	// also reported as cli_status.
	verified := entry.LoginState == harnessLoginSignedIn && (entry.LoginSource == "env" || entry.LoginSource == "cli_status")
	if entry.LoginState == harnessLoginUnknown && wasDiscovered {
		// Reported for display only; discovery is not a verified login, so
		// it never makes the harness runnable.
		entry.LoginState, entry.LoginSource = discoveryLogin(agent)
	}
	signedOut, observed := d.Hints.snapshot(id)
	if signedOut {
		entry.LoginState = harnessLoginSignedOut
	}
	switch {
	case wasDiscovered && (agent.OnboardingState == "fully_onboarded" || agent.OnboardingState == "gateway_only"):
		entry.Billing = "metered"
	case entry.LoginState == harnessLoginSignedIn:
		entry.Billing = "seat"
	}
	seen := map[string]struct{}{}
	models := appendModels(nil, seen, harnessModelSourceStatic, harnessStaticModelsFor(id, entry.Version)...)
	entry.Models = appendModels(models, seen, harnessModelSourceObserved, observed...)
	if err == nil && verified && !signedOut {
		entry.SupportLevel = claudeSupportLevel
		entry.SessionMode = claudeSessionMode
		if name := generatedProfileFor(id, d); name != "" {
			entry.GeneratedProfile = &name
		}
		entry.Capabilities = hostExecCapabilitiesFor(id)
	}
	return entry, true
}

// Runner flows and runner sessions (#1670) both run Claude Code.
const (
	claudeSupportLevel = harnessSupportFlowsSessions
	claudeSessionMode  = "resume"
)
