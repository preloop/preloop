package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"sync"
	"unicode/utf8"
)

// Copilot CLI host execution (issue #956). A private runner runs the locally
// installed `copilot` under the operator's existing login (`copilot login`,
// COPILOT_GITHUB_TOKEN, GH_TOKEN, GITHUB_TOKEN or the GitHub CLI login), so
// the run uses GitHub-hosted models under that user's Copilot seat. The
// traffic never passes through the Preloop gateway.
//
// Verified against GitHub Copilot CLI 1.0.88 (`copilot help`,
// `copilot help permissions`, `copilot help environment`) and
// https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference.

const (
	hostExecHarnessCursor  = "cursor_cli"
	hostExecHarnessCopilot = "copilot_cli"

	copilotMaxToolRules     = 64
	copilotMaxToolRuleBytes = 256
	copilotMaxErrorBytes    = 512
)

var (
	hostExecCopilotNames = map[string]struct{}{
		"copilot": {},
	}
	// Flags the runner owns on a Copilot run. A local argv cannot change the
	// prompt transport, output format, model, permission grants, working
	// directory, session identity, or where the transcript is published.
	copilotManagedFlags = map[string]struct{}{
		"--": {}, "-p": {}, "--prompt": {}, "-i": {}, "--interactive": {},
		"-s": {}, "--silent": {}, "--output-format": {}, "--model": {},
		"--no-ask-user": {}, "-C": {}, "-r": {}, "--resume": {}, "--continue": {},
		"--session-id": {}, "--connect": {}, "-n": {}, "--name": {}, "--agent": {},
		"--allow-all": {}, "--yolo": {}, "--allow-all-tools": {},
		"--allow-all-paths": {}, "--allow-all-urls": {}, "--allow-tool": {},
		"--deny-tool": {}, "--add-dir": {}, "--share": {}, "--share-gist": {},
		"--remote": {}, "--remote-export": {}, "--acp": {},
		"--additional-mcp-config": {}, "--plugin-dir": {},
	}
	// BYOK and permission variables that would silently change what the
	// operator's profile means: a provider URL moves traffic off the seat,
	// and COPILOT_ALLOW_ALL=true grants every tool.
	copilotStrippedEnvPrefixes = []string{"COPILOT_PROVIDER_"}
	copilotStrippedEnv         = map[string]struct{}{
		"COPILOT_OFFLINE":   {},
		"COPILOT_ALLOW_ALL": {},
	}
	copilotSessionIDRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)
)

func hostExecIsCopilotBinary(executable string) bool {
	_, ok := hostExecCopilotNames[hostExecBinaryBase(executable)]
	return ok
}

// hostExecHarnessForAgentType maps a leased agent_type to the harness the
// runner must use. Unknown types return "" and fail closed.
func hostExecHarnessForAgentType(agentType string) string {
	switch agentType {
	case "cursor":
		return hostExecHarnessCursor
	case "copilot":
		return hostExecHarnessCopilot
	default:
		return ""
	}
}

func validateCopilotHostExecProfile(profile hostExecProfile) error {
	if profile.ForceWrites {
		return fmt.Errorf("force_writes applies to Cursor profiles; grant Copilot writes with allow_tools (for example \"write\")")
	}
	for _, arg := range profile.Argv {
		flag := strings.SplitN(arg, "=", 2)[0]
		if _, managed := copilotManagedFlags[flag]; managed {
			return fmt.Errorf("profile argv cannot override managed flag %s", flag)
		}
	}
	if err := validateCopilotToolRules("allow_tools", profile.AllowTools); err != nil {
		return err
	}
	return validateCopilotToolRules("deny_tools", profile.DenyTools)
}

func validateCopilotToolRules(field string, rules []string) error {
	if len(rules) > copilotMaxToolRules {
		return fmt.Errorf("%s supports at most %d entries", field, copilotMaxToolRules)
	}
	for i, rule := range rules {
		trimmed := strings.TrimSpace(rule)
		switch {
		case trimmed == "":
			return fmt.Errorf("%s[%d] is empty", field, i)
		case !utf8.ValidString(rule) || strings.ContainsRune(rule, 0):
			return fmt.Errorf("%s[%d] is not valid UTF-8", field, i)
		case len(rule) > copilotMaxToolRuleBytes:
			return fmt.Errorf("%s[%d] exceeds %d bytes", field, i, copilotMaxToolRuleBytes)
		case strings.HasPrefix(trimmed, "-"):
			return fmt.Errorf("%s[%d] must be a tool rule, not a flag", field, i)
		}
	}
	return nil
}

// buildCopilotHostExecArgs renders the non-interactive Copilot command. The
// prompt uses the `--prompt=` spelling of -p so a prompt that starts with a
// dash is never parsed as a flag. --allow-all-tools is only ever set when the
// operator opted in on the local profile.
func buildCopilotHostExecArgs(profile hostExecProfile, job map[string]any, mcpArgs ...string) ([]string, error) {
	prompt, err := jobPromptText(job)
	if err != nil {
		return nil, err
	}
	if strings.ContainsRune(prompt, 0) {
		return nil, fmt.Errorf("prompt contains NUL")
	}
	if strings.TrimSpace(prompt) == "" {
		return nil, fmt.Errorf("copilot host execution requires a prompt")
	}
	args := append([]string{}, profile.Argv...)
	args = append(args, "--prompt="+prompt, "-s", "--no-ask-user", "--output-format=json")
	if requested := jobModelIdentifier(job); requested != "" {
		alias := profile.ModelMap[requested]
		if alias == "" {
			return nil, fmt.Errorf("model not in local model_map")
		}
		args = append(args, "--model="+alias)
	}
	args = append(args, mcpArgs...)
	if profile.AllowAllTools {
		args = append(args, "--allow-all-tools")
	} else {
		for _, rule := range profile.AllowTools {
			args = append(args, "--allow-tool="+strings.TrimSpace(rule))
		}
		if len(mcpArgs) > 0 {
			// The flow's Preloop MCP server is filtered server-side to the
			// flow's allowed tools; -p mode cannot prompt, so grant it here.
			// A deny_tools rule for the server still wins.
			args = append(args, "--allow-tool="+hostExecMCPServerName)
		}
	}
	for _, rule := range profile.DenyTools {
		args = append(args, "--deny-tool="+strings.TrimSpace(rule))
	}
	return args, nil
}

// copilotHostExecEnv is the Copilot-specific strip stage applied after the
// allowlist in hostExecChildEnv. The allowlist keeps the seat login
// (COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN); this pass drops the
// variables that would route the run through a BYOK provider or grant every
// tool.
func copilotHostExecEnv(environ []string) []string {
	out := make([]string, 0, len(environ))
	for _, entry := range environ {
		// Compare case-insensitively: Windows environment names are, and
		// the allowlist admits COPILOT_* in any case there, so a mixed-case
		// Copilot_Allow_All must not slip past. On POSIX a lowercase name is
		// not read by Copilot, so dropping it too costs nothing.
		key := strings.ToUpper(strings.SplitN(entry, "=", 2)[0])
		if _, drop := copilotStrippedEnv[key]; drop {
			continue
		}
		stripped := false
		for _, prefix := range copilotStrippedEnvPrefixes {
			if strings.HasPrefix(key, prefix) {
				stripped = true
				break
			}
		}
		if !stripped {
			out = append(out, entry)
		}
	}
	return out
}

// prepareCopilotHostExecHooks installs the Preloop usage hooks into the
// Preloop-owned ~/.copilot/hooks/preloop.json (or $COPILOT_HOME/hooks) so the
// run's sessionStart / sessionEnd / agentStop events are ingested. No other
// file under ~/.copilot is touched. A profile that opted into
// --allow-all-tools must already have the approval preToolUse hook from
// `preloop agents onboard "Copilot CLI" --approvals`; without it every tool
// would run ungated, so the job fails before Copilot starts.
func prepareCopilotHostExecHooks(profile hostExecProfile) error {
	if err := ensureCopilotHostExecUsageHooks(); err != nil {
		return fmt.Errorf("copilot_hooks_unavailable: install Preloop Copilot hooks: %w", err)
	}
	if !profile.AllowAllTools {
		return nil
	}
	installed, err := copilotApprovalHookInstalled()
	if err != nil {
		return fmt.Errorf("copilot_hooks_unavailable: read Preloop Copilot hooks: %w", err)
	}
	if !installed {
		return fmt.Errorf(
			"copilot_approval_hook_missing: profile %q sets allow_all_tools, which requires the Preloop preToolUse approval hook; run `preloop agents onboard \"Copilot CLI\" --approvals` as the runner user",
			profile.Name,
		)
	}
	return nil
}

// copilotHooksMu serializes concurrent host jobs in one runner so their
// read-modify-write cycles on the hooks file cannot interleave.
var copilotHooksMu sync.Mutex

// ensureCopilotHostExecUsageHooks upserts every usage hook in one pass. A
// Copilot process from a concurrent job may be loading the same file, so the
// file is never truncated in place: an unchanged document is not rewritten
// (the steady state), and a changed one is replaced by rename.
func ensureCopilotHostExecUsageHooks() error {
	copilotHooksMu.Lock()
	defer copilotHooksMu.Unlock()
	path, err := copilotPreloopHooksPath()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("failed to create Copilot hooks directory: %w", err)
	}
	current, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("failed to read %s: %w", path, err)
	}
	doc, err := loadJSONDocumentOrEmpty(path)
	if err != nil {
		return err
	}
	doc["version"] = 1
	hooks := ensureObjectChild(doc, "hooks")
	command := copilotUsageHookCommand()
	for _, key := range copilotUsageHookEvents {
		hooks[key] = []interface{}{copilotCommandHookEntry(command, cursorUsageHookTimeoutSeconds)}
	}
	data, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to encode Copilot hooks: %w", err)
	}
	data = append(data, '\n')
	if bytes.Equal(current, data) {
		return nil
	}
	return writeFileAtomic(path, data, 0o600)
}

// writeFileAtomic writes data to a temp file in the target directory and
// renames it over path, so readers see the old or the new file, never a
// partial one.
func writeFileAtomic(path string, data []byte, mode os.FileMode) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".*.tmp")
	if err != nil {
		return fmt.Errorf("failed to create temp file for %s: %w", path, err)
	}
	name := tmp.Name()
	cleanup := func() { _ = os.Remove(name) }
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		cleanup()
		return fmt.Errorf("failed to write %s: %w", path, err)
	}
	if err := tmp.Chmod(mode); err != nil {
		_ = tmp.Close()
		cleanup()
		return fmt.Errorf("failed to secure %s: %w", path, err)
	}
	if err := tmp.Close(); err != nil {
		cleanup()
		return fmt.Errorf("failed to write %s: %w", path, err)
	}
	if err := os.Rename(name, path); err != nil {
		cleanup()
		return fmt.Errorf("failed to replace %s: %w", path, err)
	}
	return nil
}

// copilotApprovalHookKeys names the command keys Copilot executes on goos:
// bash on POSIX and powershell on Windows, matching what onboarding writes
// (copilotCommandHookEntryFor). An entry under the other key never runs, so
// accepting it would let allow_all_tools start with no approval gate (for
// example a bash entry left by an older onboarding on a Windows host).
// Copilot has no generic "command" key, so none is accepted.
func copilotApprovalHookKeys(goos string) []string {
	if goos == "windows" {
		return []string{"powershell"}
	}
	return []string{"bash"}
}

func copilotApprovalHookInstalled() (bool, error) {
	path, err := copilotPreloopHooksPath()
	if err != nil {
		return false, err
	}
	doc, err := loadJSONDocumentOrEmpty(path)
	if err != nil {
		return false, err
	}
	hooks, _ := doc["hooks"].(map[string]interface{})
	entries, _ := hooks["preToolUse"].([]interface{})
	for _, raw := range entries {
		entry, _ := raw.(map[string]interface{})
		for _, key := range copilotApprovalHookKeys(runtime.GOOS) {
			command, _ := entry[key].(string)
			if strings.Contains(command, "agents permission-hook") {
				return true, nil
			}
		}
	}
	return false, nil
}

// copilotCapture is the bounded state read from Copilot's JSONL stream.
// Only identifiers and counts are kept; prompt and response text are not.
type copilotCapture struct {
	SessionID       string
	Model           string
	Results         int
	ExitCode        *int
	PremiumRequests *float64
	ErrorLine       string
}

type copilotStreamEvent struct {
	Type      string `json:"type"`
	SessionID string `json:"sessionId"`
	ExitCode  *int   `json:"exitCode"`
	Usage     *struct {
		PremiumRequests *float64 `json:"premiumRequests"`
	} `json:"usage"`
	Data json.RawMessage `json:"data"`
}

// applyCopilotLine records one output line and reports whether it should be
// kept in the execution log. Streaming *_delta events repeat the final
// message token by token; dropping them keeps a long run under the log cap.
func applyCopilotLine(capture *copilotCapture, line string) bool {
	trimmed := strings.TrimSpace(line)
	if trimmed == "" {
		return true
	}
	if trimmed[0] != '{' {
		// Copilot prints startup failures (no login, unknown model) as plain
		// text before any JSONL. Keep the first one for the error message.
		if capture.ErrorLine == "" || (!strings.HasPrefix(capture.ErrorLine, "Error:") && strings.HasPrefix(trimmed, "Error:")) {
			trimmed = truncateUTF8(trimmed, copilotMaxErrorBytes)
			capture.ErrorLine = trimmed
		}
		return true
	}
	var event copilotStreamEvent
	if json.Unmarshal([]byte(trimmed), &event) != nil {
		return true
	}
	switch {
	case event.Type == "result":
		capture.Results++
		if copilotSessionIDRe.MatchString(event.SessionID) {
			capture.SessionID = event.SessionID
		}
		capture.ExitCode = event.ExitCode
		if event.Usage != nil && event.Usage.PremiumRequests != nil && *event.Usage.PremiumRequests >= 0 {
			value := *event.Usage.PremiumRequests
			capture.PremiumRequests = &value
		}
	case event.Type == "assistant.message":
		var data struct {
			Model string `json:"model"`
		}
		if json.Unmarshal(event.Data, &data) == nil && hostExecModelRe.MatchString(data.Model) {
			capture.Model = data.Model
		}
	case strings.HasSuffix(event.Type, "_delta"):
		return false
	}
	return true
}

// copilotRunnerResult requires exactly one terminal `result` event with
// exitCode 0 for success. Exit zero without it is a failure the operator sees.
func copilotRunnerResult(capture copilotCapture) (map[string]any, error) {
	if capture.Results != 1 || capture.ExitCode == nil {
		return nil, fmt.Errorf("host execution exited without a valid structured completion result")
	}
	status := "success"
	if *capture.ExitCode != 0 {
		status = "failure"
	}
	result := map[string]any{"status": status, "harness": hostExecHarnessCopilot}
	if capture.SessionID != "" {
		result["session_id"] = capture.SessionID
	}
	if capture.Model != "" {
		result["model"] = capture.Model
	}
	if capture.PremiumRequests != nil {
		result["premium_requests"] = *capture.PremiumRequests
	}
	return result, nil
}

// copilotHostExecFailure turns Copilot's plain-text startup errors into
// named errors. Copilot does not expose a non-interactive list of the
// models a seat offers, so the unavailable-model error lists the models this
// profile maps; the operator aligns model_map with `/model` in copilot.
func copilotHostExecFailure(buffer *runnerLogBuffer, profileName string, job map[string]any, fallback string) string {
	buffer.mu.Lock()
	line := buffer.copilotCapture.ErrorLine
	buffer.mu.Unlock()
	switch {
	case strings.Contains(line, "No authentication information found"):
		return "copilot_not_logged_in: Copilot CLI on this runner has no login; run `copilot login` as the runner user or export COPILOT_GITHUB_TOKEN"
	case strings.Contains(line, "--model") && strings.Contains(line, "not available"):
		requested := jobModelIdentifier(job)
		offered := "none"
		alias := ""
		if profile, err := lookupHostExecProfile(profileName); err == nil {
			alias = profile.ModelMap[requested]
			models := make([]string, 0, len(profile.ModelMap))
			for _, value := range profile.ModelMap {
				models = append(models, value)
			}
			sort.Strings(models)
			models = uniqueSortedStrings(models)
			if len(models) > 0 {
				offered = strings.Join(models, ", ")
			}
		}
		return fmt.Sprintf(
			"copilot_model_unavailable: Copilot model %q (requested %q) is not available to this runner's Copilot seat; models mapped by profile %q: %s",
			alias, requested, profileName, offered,
		)
	case line != "":
		if fallback == "" {
			return line
		}
		return fallback + ": " + line
	default:
		return fallback
	}
}

func uniqueSortedStrings(values []string) []string {
	out := values[:0]
	for i, value := range values {
		if i == 0 || value != values[i-1] {
			out = append(out, value)
		}
	}
	return out
}

// truncateUTF8 cuts s to at most max bytes without splitting a rune.
func truncateUTF8(s string, max int) string {
	if len(s) <= max {
		return s
	}
	cut := max
	for cut > 0 && !utf8.RuneStart(s[cut]) {
		cut--
	}
	return s[:cut]
}
