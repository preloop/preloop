package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/preloop/preloop/cli/internal/config"
)

// permissionHookCredentialFileName is the per-agent file written under
// ~/.preloop/agents/<runtime_principal>/ during --approvals onboarding.
const permissionHookCredentialFileName = "permission_hook.json"

// permissionHookCommandMarker uniquely identifies hook entries this CLI owns,
// so install is idempotent and offboard removes only our entries.
const permissionHookCommandMarker = "agents permission-hook"

// approvalHookTimeoutSeconds bounds how long the agent waits for our hook to
// return. It must exceed the backend's human-approval window (~300s).
const approvalHookTimeoutSeconds = 300

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

	if err := writePermissionHookCredential(agent, source, baseURL, token); err != nil {
		return err
	}

	command := approvalHookCommand(source)
	configPath, err := approvalHookConfigPath(source)
	if err != nil {
		return err
	}

	switch source {
	case permissionSourceClaudeCode:
		if err := upsertNestedCommandHook(configPath, "PreToolUse", "*", command, approvalHookTimeoutSeconds); err != nil {
			return err
		}
	case permissionSourceCodexCLI:
		// Codex's hooks.json uses the same nested matcher/hooks shape as Claude.
		if err := upsertNestedCommandHook(configPath, "PermissionRequest", "*", command, approvalHookTimeoutSeconds); err != nil {
			return err
		}
	case permissionSourceCursor:
		if err := upsertFlatCommandHook(
			configPath,
			[]string{"beforeShellExecution", "beforeMCPExecution"},
			command,
			approvalHookTimeoutSeconds,
		); err != nil {
			return err
		}
	}

	if out != nil {
		fmt.Fprintf(out, "  Mobile approvals: installed %s hook (%s)\n", source, configPath) //nolint:errcheck
		if source == permissionSourceCursor {
			fmt.Fprintln(out, "  Note: Cursor reliably enforces only DENY from a hook; an allow may be overridden by Cursor's in-app allowlist.") //nolint:errcheck
		}
	}
	return nil
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
			[]string{"beforeShellExecution", "beforeMCPExecution"},
			true,
		); err != nil {
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

func writePermissionHookCredential(agent AgentConfig, source, baseURL, token string) error {
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
	cred := permissionHookCredential{
		BaseURL:          resolvedBase,
		Token:            token,
		Source:           source,
		RuntimePrincipal: runtimePrincipalIDForAgent(agent),
		ConfigPath:       agent.ConfigPath,
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
// replacing any existing Preloop entries for each event key.
func upsertFlatCommandHook(path string, eventKeys []string, command string, timeoutSeconds int) error {
	doc, err := loadJSONDocumentOrEmpty(path)
	if err != nil {
		return err
	}
	if _, ok := doc["version"]; !ok {
		doc["version"] = 1
	}
	hooks := ensureObjectChild(doc, "hooks")
	for _, key := range eventKeys {
		list := stripPreloopFlatEntries(asArrayValue(hooks[key]))
		entry := map[string]interface{}{"command": command}
		if timeoutSeconds > 0 {
			entry["timeout"] = timeoutSeconds
		}
		hooks[key] = append(list, entry)
	}
	return writeJSONDocument(path, doc)
}

func removeFlatCommandHook(path string, eventKeys []string, deleteFileIfEmpty bool) error {
	doc, existed, err := loadJSONDocumentIfExists(path)
	if err != nil || !existed {
		return err
	}
	hooks, ok := asObjectMap(doc["hooks"])
	if !ok {
		return nil
	}
	for _, key := range eventKeys {
		list := stripPreloopFlatEntries(asArrayValue(hooks[key]))
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
	out := make([]interface{}, 0, len(list))
	for _, item := range list {
		entry, ok := asObjectMap(item)
		if !ok {
			out = append(out, item)
			continue
		}
		command, _ := entry["command"].(string)
		if strings.Contains(command, permissionHookCommandMarker) {
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
