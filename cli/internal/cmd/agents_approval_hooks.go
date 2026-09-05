package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
)

// permissionHookCredentialFileName is the per-agent file written under
// ~/.preloop/agents/<runtime_principal>/ during --approvals onboarding.
const permissionHookCredentialFileName = "permission_hook.json"

// permissionHookCommandMarker uniquely identifies hook entries this CLI owns,
// so install is idempotent and offboard removes only our entries.
const permissionHookCommandMarker = "agents permission-hook"

// usageHookCommandMarker identifies the Cursor usage/session hook entries
// installed by onboarding, kept distinct from the permission hook marker so
// each set is upserted and removed independently.
const usageHookCommandMarker = "usage hook"

// cursorUsageHookTimeoutSeconds bounds each usage hook process. The hook
// posts once with a 3s client timeout and is fail-open, so 5s is ample.
const cursorUsageHookTimeoutSeconds = 5

// cursorUsageHookEvents are the Cursor hook events wired to
// `preloop usage hook --from cursor`. beforeSubmitPrompt is included only
// so the first prompt line can become the session title; the command never
// posts for it and ignores the prompt otherwise.
var cursorUsageHookEvents = []string{
	"sessionStart",
	"sessionEnd",
	"subagentStart",
	"subagentStop",
	"stop",
	"preCompact",
	"beforeSubmitPrompt",
}

// approvalHookTimeoutSeconds is the legacy fallback name kept for callers that
// still reference a constant; prefer resolveApprovalHookTimeoutSeconds().
const approvalHookTimeoutSeconds = defaultApprovalHookTimeoutSeconds

// permissionSourceForAgent maps a discovered agent to its permission-check
// source, or "" if the agent is not supported by the local hook adapters.
func permissionSourceForAgent(agent AgentConfig) string {
	switch {
	case isClaudeCodeAgent(agent):
		return permissionSourceClaudeCode
	case isCodexCLIAgent(agent):
		return permissionSourceCodexCLI
	case strings.EqualFold(strings.TrimSpace(agent.Name), "Cursor"):
		return permissionSourceCursor
	default:
		return ""
	}
}

func isApprovalHookSupportedAgent(agent AgentConfig) bool {
	return permissionSourceForAgent(agent) != ""
}

// promptForApprovalsOptIn asks the user whether to install the native
// tool-approvals hook for a supported agent when onboarding was started
// without --approvals. Unsupported agents never prompt and never opt in.
func promptForApprovalsOptIn(
	reader io.Reader,
	writer io.Writer,
	agent AgentConfig,
) (bool, error) {
	if !isApprovalHookSupportedAgent(agent) {
		return false, nil
	}
	return confirmActionDefaultYes(
		reader,
		writer,
		fmt.Sprintf(
			"Route %s's native tool calls (shell commands, file edits) through Preloop approvals? (Y/n): ",
			resolveAgentDisplayName(agent),
		),
	)
}

func permissionHookAgentsDir() (string, error) {
	baseDir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(baseDir, "agents"), nil
}

func permissionHookCredentialPath(agent AgentConfig) (string, error) {
	dir, err := permissionHookAgentsDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, runtimePrincipalIDForAgent(agent), permissionHookCredentialFileName), nil
}

// approvalHookConfigPath returns the agent-specific hook configuration file for
// the given source.
func approvalHookConfigPath(source string) (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	switch source {
	case permissionSourceClaudeCode:
		return filepath.Join(home, ".claude", "settings.json"), nil
	case permissionSourceCodexCLI:
		return filepath.Join(home, ".codex", "hooks.json"), nil
	case permissionSourceCursor:
		return filepath.Join(home, ".cursor", "hooks.json"), nil
	default:
		return "", fmt.Errorf("unsupported approval hook source %q", source)
	}
}

// preloopExecutableForHooks returns an absolute path to this binary so hook
// commands keep working regardless of the agent's PATH. It falls back to
// "preloop" if the executable path cannot be resolved.
func preloopExecutableForHooks() string {
	exe, err := os.Executable()
	if err != nil {
		return "preloop"
	}
	if resolved, err := filepath.EvalSymlinks(exe); err == nil {
		exe = resolved
	}
	if strings.TrimSpace(exe) == "" {
		return "preloop"
	}
	return exe
}

func approvalHookCommand(source string) string {
	return fmt.Sprintf("%s agents permission-hook --source %s", preloopExecutableForHooks(), source)
}

// resolveApprovalHookTimeoutSeconds returns the account default workflow
// timeout when reachable, otherwise defaultApprovalHookTimeoutSeconds.
func resolveApprovalHookTimeoutSeconds() int {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil || !client.IsAuthenticated() {
		return defaultApprovalHookTimeoutSeconds
	}
	var policies []ApprovalWorkflow
	if err := client.Get(approvalPoliciesPath, &policies); err != nil {
		return defaultApprovalHookTimeoutSeconds
	}
	for _, policy := range policies {
		if policy.IsDefault && policy.TimeoutSeconds > 0 {
			return policy.TimeoutSeconds
		}
	}
	for _, policy := range policies {
		if policy.TimeoutSeconds > 0 {
			return policy.TimeoutSeconds
		}
	}
	return defaultApprovalHookTimeoutSeconds
}

// discoverAgentPolicyPaths returns native policy file paths for the agent
// source so onboard can record them and the hook can re-read them later.
func discoverAgentPolicyPaths(source string, workspaceRoot string) []string {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil
	}
	switch source {
	case permissionSourceCursor:
		roots := []string{}
		if workspaceRoot != "" {
			roots = append(roots, workspaceRoot)
		}
		if cwd, cwdErr := os.Getwd(); cwdErr == nil && cwd != "" {
			roots = append(roots, cwd)
		}
		return discoverCursorPermissionPolicyPaths(roots)
	case permissionSourceClaudeCode:
		paths := []string{
			filepath.Join(home, ".claude", "settings.json"),
			filepath.Join(home, ".claude", "settings.local.json"),
		}
		if workspaceRoot != "" {
			paths = append(paths,
				filepath.Join(workspaceRoot, ".claude", "settings.json"),
				filepath.Join(workspaceRoot, ".claude", "settings.local.json"),
			)
		}
		return append(paths, claudeManagedSettingsPath())
	case permissionSourceCodexCLI:
		return []string{filepath.Join(home, ".codex", "config.toml")}
	default:
		return nil
	}
}

func workspaceRootForAgent(agent AgentConfig) string {
	if cwd, err := os.Getwd(); err == nil && strings.TrimSpace(cwd) != "" {
		return cwd
	}
	if strings.TrimSpace(agent.ConfigPath) == "" {
		return ""
	}
	abs, err := filepath.Abs(agent.ConfigPath)
	if err != nil {
		return ""
	}
	return filepath.Dir(abs)
}

// installApprovalHooks writes the per-agent credential file and registers the
// native pre-tool hook for the agent. It is idempotent: re-running replaces our
// existing entries rather than duplicating them.
func installApprovalHooks(agent AgentConfig, baseURL, token string, out io.Writer) error {
	source := permissionSourceForAgent(agent)
	if source == "" {
		return nil
	}
	if strings.TrimSpace(token) == "" {
		return fmt.Errorf("cannot install approval hook without a durable credential token")
	}

	timeoutSeconds := resolveApprovalHookTimeoutSeconds()
	workspaceRoot := workspaceRootForAgent(agent)
	policyPaths := discoverAgentPolicyPaths(source, workspaceRoot)

	if err := writePermissionHookCredential(
		agent, source, baseURL, token, timeoutSeconds, policyPaths, workspaceRoot,
	); err != nil {
		return err
	}

	command := approvalHookCommand(source)
	configPath, err := approvalHookConfigPath(source)
	if err != nil {
		return err
	}

	switch source {
	case permissionSourceClaudeCode:
		if err := upsertNestedCommandHook(configPath, "PreToolUse", "*", command, timeoutSeconds); err != nil {
			return err
		}
	case permissionSourceCodexCLI:
		// Codex's hooks.json uses the same nested matcher/hooks shape as Claude.
		if err := upsertNestedCommandHook(configPath, "PermissionRequest", "*", command, timeoutSeconds); err != nil {
			return err
		}
	case permissionSourceCursor:
		// beforeShellExecution / beforeMCPExecution cover terminal + MCP.
		// preToolUse is required for native file tools (Write / StrReplace /
		// Edit) — the path that Claude Code's PreToolUse was incorrectly
		// gating when Cursor loaded ~/.claude/settings.json as third-party.
		if err := upsertFlatCommandHook(
			configPath,
			[]string{"beforeShellExecution", "beforeMCPExecution", "preToolUse"},
			command,
			timeoutSeconds,
		); err != nil {
			return err
		}
	}

	if out != nil {
		fmt.Fprintf(out, "  Mobile approvals: installed %s hook (%s)\n", source, configPath)                                                //nolint:errcheck
		fmt.Fprintf(out, "  Approval wait timeout: %ds (re-run onboard --approvals after changing the workflow timeout)\n", timeoutSeconds) //nolint:errcheck
		printAgentPolicySummary(out, source, policyPaths, workspaceRoot)
		if source == permissionSourceClaudeCode {
			// Cursor loads ~/.claude/settings.json as third-party hooks; the
			// permission-hook auto-allows under Cursor so this install cannot
			// accidentally gate Cursor Agent. Spell that out so operators know
			// how to govern Cursor separately.
			fmt.Fprintln(out, "  Note: Cursor may load this Claude Code hook as a third-party config.")               //nolint:errcheck
			fmt.Fprintln(out, "        Preloop auto-allows those Cursor invocations so Cursor Agent is not blocked.") //nolint:errcheck
			fmt.Fprintln(out, "        To govern Cursor itself, run: preloop agents onboard Cursor --approvals")      //nolint:errcheck
		}
		if source == permissionSourceCursor {
			fmt.Fprintln(out, "  Note: Cursor reliably enforces only DENY from a hook; an allow may be overridden by Cursor's in-app allowlist.") //nolint:errcheck
		}
	}
	return nil
}

// enablePermissionPromptBuiltin flips the permission_prompt builtin on for
// exactly the agent being onboarded. The tool is default-disabled
// account-wide because it is only useful to headless Claude Code runs and
// would otherwise cost every MCP client of the account tools/list context
// (issue #128), so approvals onboarding creates an agent-scoped
// ToolConfiguration row instead of an account-wide one.
//
// A 400 from the create endpoint means a row for this exact scope already
// exists (idempotent re-onboarding); an explicit operator disable is
// deliberately not overridden.
func enablePermissionPromptBuiltin(client *api.Client, managedAgentID string, out io.Writer) error {
	if client == nil || strings.TrimSpace(managedAgentID) == "" {
		return nil
	}
	payload := map[string]interface{}{
		"tool_name":        "permission_prompt",
		"tool_source":      "builtin",
		"account_id":       "", // derived server-side from the authenticated session
		"is_enabled":       true,
		"managed_agent_id": managedAgentID,
	}
	var response map[string]interface{}
	err := client.Post("/api/v1/tool-configurations", payload, &response)
	alreadyConfigured := err != nil && api.IsStatus(err, http.StatusBadRequest)
	if err != nil && !alreadyConfigured {
		return fmt.Errorf("failed to enable the permission_prompt builtin: %w", err)
	}
	if out != nil {
		if alreadyConfigured {
			fmt.Fprintln(out, "  Headless approvals: permission_prompt builtin already configured for this agent") //nolint:errcheck
		} else {
			fmt.Fprintln(out, "  Headless approvals: enabled the permission_prompt builtin (scoped to this agent only)") //nolint:errcheck
		}
		fmt.Fprintln(out, "  For headless (claude -p) runs, route permission prompts through Preloop with:")      //nolint:errcheck
		fmt.Fprintln(out, "    claude -p --permission-prompt-tool mcp__preloop__permission_prompt \"your task\"") //nolint:errcheck
	}
	return nil
}

func printAgentPolicySummary(out io.Writer, source string, policyPaths []string, workspaceRoot string) {
	if out == nil {
		return
	}
	switch source {
	case permissionSourceCursor:
		policy, err := loadCursorPermissionPolicy(policyPaths)
		if err != nil {
			fmt.Fprintf(out, "  Mobile approvals: failed to load Cursor policy: %v\n", err) //nolint:errcheck
			return
		}
		fmt.Fprintln(out, "  Mobile approvals: loaded Cursor policy") //nolint:errcheck
		for _, line := range summarizeCursorPermissionPolicy(policy, policyPaths) {
			fmt.Fprintf(out, "    %s\n", line) //nolint:errcheck
		}
	case permissionSourceClaudeCode:
		policy, err := loadClaudePermissionPolicy(workspaceRoot)
		if err != nil {
			fmt.Fprintf(out, "  Mobile approvals: failed to load Claude policy: %v\n", err) //nolint:errcheck
			return
		}
		fmt.Fprintln(out, "  Mobile approvals: loaded Claude Code policy")      //nolint:errcheck
		fmt.Fprintf(out, "    allow: %d, deny: %d, ask: %d, defaultMode: %s\n", //nolint:errcheck
			len(policy.Allow), len(policy.Deny), len(policy.Ask),
			firstNonEmptyString(policy.DefaultMode, "(unset)"),
		)
	case permissionSourceCodexCLI:
		fmt.Fprintln(out, "  Mobile approvals: Codex PermissionRequest only fires for would-prompt calls") //nolint:errcheck
	}
}

// removeApprovalHooks removes our hook entries and the per-agent credential
// file. It is safe to call even when nothing was installed.
func removeApprovalHooks(agent AgentConfig, out io.Writer) error {
	source := permissionSourceForAgent(agent)
	if source == "" {
		return nil
	}

	configPath, err := approvalHookConfigPath(source)
	if err != nil {
		return err
	}

	switch source {
	case permissionSourceClaudeCode:
		// settings.json holds unrelated user settings, so never delete the file.
		if err := removeNestedCommandHook(configPath, "PreToolUse", false); err != nil {
			return err
		}
	case permissionSourceCodexCLI:
		if err := removeNestedCommandHook(configPath, "PermissionRequest", true); err != nil {
			return err
		}
	case permissionSourceCursor:
		if err := removeFlatCommandHook(
			configPath,
			[]string{"beforeShellExecution", "beforeMCPExecution", "preToolUse"},
			true,
		); err != nil {
			return err
		}
		// Offboarding removes the usage/session hooks installed alongside.
		if err := removeCursorUsageHooks(agent, out); err != nil {
			return err
		}
	}

	if err := removePermissionHookCredential(agent); err != nil {
		return err
	}
	if out != nil {
		fmt.Fprintf(out, "  Mobile approvals: removed %s hook\n", source) //nolint:errcheck
	}
	return nil
}

// cursorUsageHookCommand is the hooks.json command for the usage hook. The
// opt-in flag is persisted in the command itself so the choice survives
// without a credential file.
func cursorUsageHookCommand(storeTranscript bool) string {
	command := fmt.Sprintf("%s usage hook --from cursor", preloopExecutableForHooks())
	if storeTranscript {
		command += " --store-transcript"
	}
	return command
}

// installCursorUsageHooks wires `preloop usage hook` into Cursor's
// lifecycle events so conversations are stored as runtime sessions with a
// transcript-derived token estimate. Idempotent: re-running replaces our
// entries. Non-Cursor agents are a no-op.
func installCursorUsageHooks(agent AgentConfig, storeTranscript bool, out io.Writer) error {
	if permissionSourceForAgent(agent) != permissionSourceCursor {
		return nil
	}
	configPath, err := approvalHookConfigPath(permissionSourceCursor)
	if err != nil {
		return err
	}
	if err := upsertFlatCommandHookMarked(
		configPath,
		cursorUsageHookEvents,
		cursorUsageHookCommand(storeTranscript),
		cursorUsageHookTimeoutSeconds,
		usageHookCommandMarker,
	); err != nil {
		return err
	}
	if err := setPermissionHookCredentialStoreTranscript(agent, storeTranscript); err != nil {
		return err
	}
	if out != nil {
		fmt.Fprintf(out, "  Usage hooks: installed Cursor session and token-estimate hooks (%s)\n", configPath) //nolint:errcheck
		if storeTranscript {
			fmt.Fprintln(out, "  Transcript storage: on (transcript text is shipped as session activities)") //nolint:errcheck
		} else {
			fmt.Fprintln(out, "  Transcript storage: off (counts, title and a short summary only; re-run with --store-transcript to opt in)") //nolint:errcheck
		}
	}
	return nil
}

// removeCursorUsageHooks removes the usage hook entries installed by
// installCursorUsageHooks, leaving any other entries (including our
// permission hooks) untouched.
func removeCursorUsageHooks(agent AgentConfig, out io.Writer) error {
	if permissionSourceForAgent(agent) != permissionSourceCursor {
		return nil
	}
	configPath, err := approvalHookConfigPath(permissionSourceCursor)
	if err != nil {
		return err
	}
	if err := removeFlatCommandHookMarked(configPath, cursorUsageHookEvents, usageHookCommandMarker, true); err != nil {
		return err
	}
	if out != nil {
		fmt.Fprintln(out, "  Usage hooks: removed Cursor session and token-estimate hooks") //nolint:errcheck
	}
	return nil
}

// setPermissionHookCredentialStoreTranscript records the transcript opt-in
// in the per-agent credential file when one exists (approvals onboarding
// writes it). Without a credential file the hooks.json command carries the
// flag, so a missing file is not an error.
func setPermissionHookCredentialStoreTranscript(agent AgentConfig, storeTranscript bool) error {
	path, err := permissionHookCredentialPath(agent)
	if err != nil {
		return err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return fmt.Errorf("failed to read permission hook credential: %w", err)
	}
	var cred permissionHookCredential
	if err := json.Unmarshal(data, &cred); err != nil {
		return fmt.Errorf("failed to parse permission hook credential: %w", err)
	}
	cred.StoreTranscript = &storeTranscript
	encoded, err := json.MarshalIndent(cred, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to encode permission hook credential: %w", err)
	}
	if err := os.WriteFile(path, encoded, 0600); err != nil {
		return fmt.Errorf("failed to write permission hook credential: %w", err)
	}
	return nil
}

func writePermissionHookCredential(
	agent AgentConfig,
	source, baseURL, token string,
	timeoutSeconds int,
	policyPaths []string,
	workspaceRoot string,
) error {
	path, err := permissionHookCredentialPath(agent)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return fmt.Errorf("failed to create permission hook directory: %w", err)
	}
	resolvedBase := strings.TrimRight(strings.TrimSpace(baseURL), "/")
	if resolvedBase == "" {
		resolvedBase = config.DefaultAPIURL
	}
	if timeoutSeconds <= 0 {
		timeoutSeconds = defaultApprovalHookTimeoutSeconds
	}
	// Cursor's real allowlist lives in opaque IDE state, so without the
	// read-only auto-allow an absent permissions.json would turn every "ls"
	// into a blocking approval. Claude Code's policy is fully readable from
	// its settings files, so the hook mirrors it exactly: every call the
	// agent itself would prompt for is routed to Preloop as an approval
	// request — no hook-side widening. Users can flip the flag by hand.
	safeReadAutoAllow := source == permissionSourceCursor
	cred := permissionHookCredential{
		BaseURL:           resolvedBase,
		Token:             token,
		Source:            source,
		RuntimePrincipal:  runtimePrincipalIDForAgent(agent),
		ConfigPath:        agent.ConfigPath,
		TimeoutSeconds:    timeoutSeconds,
		PolicyPaths:       policyPaths,
		WorkspaceRoot:     workspaceRoot,
		SafeReadAutoAllow: &safeReadAutoAllow,
	}
	data, err := json.MarshalIndent(cred, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to encode permission hook credential: %w", err)
	}
	if err := os.WriteFile(path, data, 0600); err != nil {
		return fmt.Errorf("failed to write permission hook credential: %w", err)
	}
	return nil
}

func removePermissionHookCredential(agent AgentConfig) error {
	path, err := permissionHookCredentialPath(agent)
	if err != nil {
		return err
	}
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("failed to remove permission hook credential: %w", err)
	}
	// Clean up the now-empty per-agent directory (best effort).
	_ = os.Remove(filepath.Dir(path))
	return nil
}

// upsertNestedCommandHook installs a matcher/hooks-shaped command hook (used by
// Claude Code PreToolUse and Codex PermissionRequest), replacing any existing
// Preloop entry under eventKey.
func upsertNestedCommandHook(path, eventKey, matcher, command string, timeoutSeconds int) error {
	doc, err := loadJSONDocumentOrEmpty(path)
	if err != nil {
		return err
	}
	hooks := ensureObjectChild(doc, "hooks")
	list := stripPreloopNestedEntries(asArrayValue(hooks[eventKey]))

	inner := map[string]interface{}{
		"type":    "command",
		"command": command,
	}
	if timeoutSeconds > 0 {
		inner["timeout"] = timeoutSeconds
	}
	entry := map[string]interface{}{
		"matcher": matcher,
		"hooks":   []interface{}{inner},
	}
	hooks[eventKey] = append(list, entry)
	return writeJSONDocument(path, doc)
}

func removeNestedCommandHook(path, eventKey string, deleteFileIfEmpty bool) error {
	doc, existed, err := loadJSONDocumentIfExists(path)
	if err != nil || !existed {
		return err
	}
	hooks, ok := asObjectMap(doc["hooks"])
	if !ok {
		return nil
	}
	list := stripPreloopNestedEntries(asArrayValue(hooks[eventKey]))
	if len(list) == 0 {
		delete(hooks, eventKey)
	} else {
		hooks[eventKey] = list
	}
	if len(hooks) == 0 {
		delete(doc, "hooks")
	}
	return finalizeHookDocument(path, doc, deleteFileIfEmpty)
}

// upsertFlatCommandHook installs flat {"command": ...} entries (used by Cursor),
// replacing any existing Preloop permission-hook entries for each event key.
func upsertFlatCommandHook(path string, eventKeys []string, command string, timeoutSeconds int) error {
	return upsertFlatCommandHookMarked(path, eventKeys, command, timeoutSeconds, permissionHookCommandMarker)
}

// upsertFlatCommandHookMarked is upsertFlatCommandHook for an arbitrary
// ownership marker: only entries whose command contains marker are
// replaced, so the permission and usage hook sets never clobber each other.
func upsertFlatCommandHookMarked(path string, eventKeys []string, command string, timeoutSeconds int, marker string) error {
	doc, err := loadJSONDocumentOrEmpty(path)
	if err != nil {
		return err
	}
	if _, ok := doc["version"]; !ok {
		doc["version"] = 1
	}
	hooks := ensureObjectChild(doc, "hooks")
	for _, key := range eventKeys {
		list := stripFlatEntriesMatching(asArrayValue(hooks[key]), marker)
		entry := map[string]interface{}{"command": command}
		if timeoutSeconds > 0 {
			entry["timeout"] = timeoutSeconds
		}
		hooks[key] = append(list, entry)
	}
	return writeJSONDocument(path, doc)
}

func removeFlatCommandHook(path string, eventKeys []string, deleteFileIfEmpty bool) error {
	return removeFlatCommandHookMarked(path, eventKeys, permissionHookCommandMarker, deleteFileIfEmpty)
}

func removeFlatCommandHookMarked(path string, eventKeys []string, marker string, deleteFileIfEmpty bool) error {
	doc, existed, err := loadJSONDocumentIfExists(path)
	if err != nil || !existed {
		return err
	}
	hooks, ok := asObjectMap(doc["hooks"])
	if !ok {
		return nil
	}
	for _, key := range eventKeys {
		list := stripFlatEntriesMatching(asArrayValue(hooks[key]), marker)
		if len(list) == 0 {
			delete(hooks, key)
		} else {
			hooks[key] = list
		}
	}
	if len(hooks) == 0 {
		delete(doc, "hooks")
		// A lone version marker is meaningless without hooks.
		delete(doc, "version")
	}
	return finalizeHookDocument(path, doc, deleteFileIfEmpty)
}

func finalizeHookDocument(path string, doc map[string]interface{}, deleteFileIfEmpty bool) error {
	if deleteFileIfEmpty && len(doc) == 0 {
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("failed to remove empty hook config %s: %w", path, err)
		}
		return nil
	}
	return writeJSONDocument(path, doc)
}

// stripPreloopNestedEntries drops Preloop-owned commands from a matcher/hooks
// list, removing any matcher entry whose inner hooks become empty.
func stripPreloopNestedEntries(list []interface{}) []interface{} {
	out := make([]interface{}, 0, len(list))
	for _, item := range list {
		entry, ok := asObjectMap(item)
		if !ok {
			out = append(out, item)
			continue
		}
		inner := stripPreloopFlatEntries(asArrayValue(entry["hooks"]))
		if len(inner) == 0 {
			// Drop the whole matcher entry only if it originally had hooks and
			// they were all ours; otherwise preserve a matcher with no hooks.
			if _, hadHooks := entry["hooks"]; hadHooks {
				continue
			}
		}
		entry["hooks"] = inner
		out = append(out, entry)
	}
	return out
}

func stripPreloopFlatEntries(list []interface{}) []interface{} {
	return stripFlatEntriesMatching(list, permissionHookCommandMarker)
}

func stripFlatEntriesMatching(list []interface{}, marker string) []interface{} {
	out := make([]interface{}, 0, len(list))
	for _, item := range list {
		entry, ok := asObjectMap(item)
		if !ok {
			out = append(out, item)
			continue
		}
		command, _ := entry["command"].(string)
		if strings.Contains(command, marker) {
			continue
		}
		out = append(out, item)
	}
	return out
}

func ensureObjectChild(doc map[string]interface{}, key string) map[string]interface{} {
	if child, ok := asObjectMap(doc[key]); ok {
		return child
	}
	child := map[string]interface{}{}
	doc[key] = child
	return child
}

func asArrayValue(value interface{}) []interface{} {
	if arr, ok := value.([]interface{}); ok {
		return arr
	}
	return nil
}

func loadJSONDocumentOrEmpty(path string) (map[string]interface{}, error) {
	doc, existed, err := loadJSONDocumentIfExists(path)
	if err != nil {
		return nil, err
	}
	if !existed {
		return map[string]interface{}{}, nil
	}
	return doc, nil
}

func loadJSONDocumentIfExists(path string) (map[string]interface{}, bool, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return map[string]interface{}{}, false, nil
		}
		return nil, false, fmt.Errorf("failed to read %s: %w", path, err)
	}
	if len(strings.TrimSpace(string(data))) == 0 {
		return map[string]interface{}{}, true, nil
	}
	var doc map[string]interface{}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, false, fmt.Errorf("failed to parse %s: %w", path, err)
	}
	if doc == nil {
		doc = map[string]interface{}{}
	}
	return doc, true, nil
}
