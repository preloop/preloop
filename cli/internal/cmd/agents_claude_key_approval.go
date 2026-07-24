package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// Claude Code records the user's answer to its "Detected a custom API key in
// your environment — do you want to use this API key?" dialog in
// ~/.claude.json under customApiKeyResponses, keyed by the LAST 20 characters
// of the key. Older builds (observed through at least 2.1.124) show that
// dialog for the gateway key onboarding writes into settings env, and the
// pre-selected answer is "No (recommended)" — one Enter keypress silently
// declines the key, after which Claude Code keeps its subscription OAuth
// token, sends it to the gateway base URL, and every model call fails with an
// opaque model error. Pre-approving the fingerprint at onboard time removes
// the dialog entirely (the user already consented by running onboarding) and
// also repairs machines carrying a historical rejection of the same key.
const claudeAPIKeyFingerprintLength = 20

func claudeAPIKeyFingerprint(token string) string {
	trimmed := strings.TrimSpace(token)
	if trimmed == "" {
		return ""
	}
	if len(trimmed) <= claudeAPIKeyFingerprintLength {
		return trimmed
	}
	return trimmed[len(trimmed)-claudeAPIKeyFingerprintLength:]
}

// claudeUserConfigPath derives the ~/.claude.json path from the agent's
// settings path (~/.claude/settings.json → ~/.claude.json) so relocated
// config roots keep working.
func claudeUserConfigPath(agent AgentConfig) string {
	settingsDir := filepath.Dir(agent.ConfigPath)
	return filepath.Join(filepath.Dir(settingsDir), ".claude.json")
}

func claudeAPIKeyResponseLists(doc map[string]interface{}) (map[string]interface{}, []string, []string) {
	responses, ok := asObjectMap(doc["customApiKeyResponses"])
	if !ok {
		responses = map[string]interface{}{}
		doc["customApiKeyResponses"] = responses
	}
	return responses, interfaceListToStrings(responses["approved"]), interfaceListToStrings(responses["rejected"])
}

func interfaceListToStrings(value interface{}) []string {
	items, ok := value.([]interface{})
	if !ok {
		return nil
	}
	result := make([]string, 0, len(items))
	for _, item := range items {
		if s, ok := item.(string); ok {
			result = append(result, s)
		}
	}
	return result
}

func stringsToInterfaceList(values []string) []interface{} {
	result := make([]interface{}, 0, len(values))
	for _, v := range values {
		result = append(result, v)
	}
	return result
}

func removeString(values []string, target string) []string {
	result := make([]string, 0, len(values))
	for _, v := range values {
		if v != target {
			result = append(result, v)
		}
	}
	return result
}

func loadClaudeUserConfig(path string) (map[string]interface{}, error) {
	doc := map[string]interface{}{}
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return doc, nil
		}
		return nil, err
	}
	if len(strings.TrimSpace(string(data))) == 0 {
		return doc, nil
	}
	// A parse failure must NOT fall through to a rewrite: ~/.claude.json holds
	// the user's whole Claude Code state and clobbering it is far worse than
	// leaving the approval dialog in place.
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, fmt.Errorf("could not parse %s: %w", path, err)
	}
	return doc, nil
}

func writeClaudeUserConfig(path string, doc map[string]interface{}) error {
	data, err := json.Marshal(doc)
	if err != nil {
		return err
	}
	mode := os.FileMode(0600)
	if info, statErr := os.Stat(path); statErr == nil {
		mode = info.Mode().Perm()
	}
	return os.WriteFile(path, data, mode)
}

// ensureClaudeAPIKeyPreApproved marks the freshly written gateway key as
// approved in ~/.claude.json. No-op for non-Claude agents and empty tokens.
func ensureClaudeAPIKeyPreApproved(agent AgentConfig, token string, writer io.Writer) error {
	if !isClaudeCodeAgent(agent) {
		return nil
	}
	fingerprint := claudeAPIKeyFingerprint(token)
	if fingerprint == "" {
		return nil
	}
	path := claudeUserConfigPath(agent)
	doc, err := loadClaudeUserConfig(path)
	if err != nil {
		return err
	}
	responses, approved, rejected := claudeAPIKeyResponseLists(doc)
	repairedRejection := len(rejected) != len(removeString(rejected, fingerprint))
	alreadyApproved := false
	for _, entry := range approved {
		if entry == fingerprint {
			alreadyApproved = true
			break
		}
	}
	if alreadyApproved && !repairedRejection {
		return nil
	}
	if !alreadyApproved {
		approved = append(approved, fingerprint)
	}
	responses["approved"] = stringsToInterfaceList(approved)
	responses["rejected"] = stringsToInterfaceList(removeString(rejected, fingerprint))
	if err := writeClaudeUserConfig(path, doc); err != nil {
		return err
	}
	if writer != nil {
		fmt.Fprintf(
			writer,
			"  Pre-approved the gateway key in %s (older Claude Code builds would otherwise prompt and default to \"No\", silently breaking model routing).\n",
			path,
		) //nolint:errcheck
	}
	return nil
}

// removeClaudeAPIKeyApproval strips the approval for the gateway key found in
// the agent's CURRENT settings env. It must run before offboarding restores
// the settings file, while the managed key is still readable.
func removeClaudeAPIKeyApproval(agent AgentConfig) error {
	if !isClaudeCodeAgent(agent) {
		return nil
	}
	current, err := loadAgentConfigDocument(agent)
	if err != nil {
		return nil // settings unreadable → nothing to derive the fingerprint from
	}
	env, ok := asObjectMap(current["env"])
	if !ok {
		return nil
	}
	token, _ := env["ANTHROPIC_API_KEY"].(string)
	fingerprint := claudeAPIKeyFingerprint(token)
	if fingerprint == "" {
		return nil
	}
	path := claudeUserConfigPath(agent)
	doc, err := loadClaudeUserConfig(path)
	if err != nil {
		return err
	}
	responses, approved, _ := claudeAPIKeyResponseLists(doc)
	remaining := removeString(approved, fingerprint)
	if len(remaining) == len(approved) {
		return nil
	}
	responses["approved"] = stringsToInterfaceList(remaining)
	return writeClaudeUserConfig(path, doc)
}
