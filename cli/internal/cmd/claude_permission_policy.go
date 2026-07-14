package cmd

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// claudePermissionPolicy models the subset of Claude Code's
// ~/.claude/settings.json that governs whether a tool call is auto-allowed,
// auto-denied, or escalated to a human ("ask"). We deliberately parse only the
// fields we need to compute a client_decision for the Preloop approval seam;
// everything else in settings.json is ignored.
type claudePermissionPolicy struct {
	Allow       []string
	Deny        []string
	Ask         []string
	DefaultMode string
}

type claudeSettingsDocument struct {
	Permissions struct {
		Allow       []string `json:"allow"`
		Deny        []string `json:"deny"`
		Ask         []string `json:"ask"`
		DefaultMode string   `json:"defaultMode"`
	} `json:"permissions"`
}

// loadClaudePermissionPolicy reads ~/.claude/settings.json and, when present,
// ~/.claude/settings.local.json, merging them into a single policy. The local
// file takes precedence for defaultMode and its rules are unioned in. A missing
// settings file is not an error — it simply yields an empty policy (everything
// escalates to "ask").
func loadClaudePermissionPolicy() (claudePermissionPolicy, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return claudePermissionPolicy{}, fmt.Errorf("failed to resolve home directory: %w", err)
	}
	policy := claudePermissionPolicy{}
	for _, name := range []string{"settings.json", "settings.local.json"} {
		path := filepath.Join(home, ".claude", name)
		doc, ok, err := readClaudeSettingsDocument(path)
		if err != nil {
			return claudePermissionPolicy{}, err
		}
		if !ok {
			continue
		}
		policy.Allow = append(policy.Allow, doc.Permissions.Allow...)
		policy.Deny = append(policy.Deny, doc.Permissions.Deny...)
		policy.Ask = append(policy.Ask, doc.Permissions.Ask...)
		if strings.TrimSpace(doc.Permissions.DefaultMode) != "" {
			policy.DefaultMode = strings.TrimSpace(doc.Permissions.DefaultMode)
		}
	}
	return policy, nil
}

func readClaudeSettingsDocument(path string) (claudeSettingsDocument, bool, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return claudeSettingsDocument{}, false, nil
		}
		return claudeSettingsDocument{}, false, fmt.Errorf("failed to read %s: %w", path, err)
	}
	if len(strings.TrimSpace(string(data))) == 0 {
		return claudeSettingsDocument{}, false, nil
	}
	var doc claudeSettingsDocument
	if err := json.Unmarshal(data, &doc); err != nil {
		return claudeSettingsDocument{}, false, fmt.Errorf("failed to parse %s: %w", path, err)
	}
	return doc, true, nil
}

// evaluateClaudePermissionPolicy decides, using the user's own configuration,
// whether a tool call would be auto-allowed, auto-denied, or prompted for.
// It returns one of "allow", "deny", or "ask". The runtime permission mode
// (from the hook event) overrides the configured defaultMode when set.
//
// Precedence mirrors Claude Code's own evaluation order:
//  1. bypassPermissions mode  -> allow everything
//  2. a matching deny rule    -> deny
//  3. a matching ask rule     -> ask (takes precedence over allow)
//  4. acceptEdits mode + edit tool -> allow
//  5. a matching allow rule   -> allow
//  6. safe read/search tools  -> allow (Claude's practical default)
//  7. otherwise               -> ask (the default "would prompt" case)
func evaluateClaudePermissionPolicy(
	policy claudePermissionPolicy,
	mode string,
	toolName string,
	toolInput map[string]interface{},
) string {
	effectiveMode := strings.TrimSpace(mode)
	if effectiveMode == "" {
		effectiveMode = strings.TrimSpace(policy.DefaultMode)
	}
	if strings.EqualFold(effectiveMode, "bypassPermissions") {
		return "allow"
	}
	if matchAnyClaudeRule(policy.Deny, toolName, toolInput) {
		return "deny"
	}
	if matchAnyClaudeRule(policy.Ask, toolName, toolInput) {
		return "ask"
	}
	if strings.EqualFold(effectiveMode, "acceptEdits") && isClaudeEditTool(toolName) {
		return "allow"
	}
	if matchAnyClaudeRule(policy.Allow, toolName, toolInput) {
		return "allow"
	}
	if isClaudeSafeReadTool(toolName) {
		return "allow"
	}
	return "ask"
}

func matchAnyClaudeRule(rules []string, toolName string, toolInput map[string]interface{}) bool {
	for _, rule := range rules {
		if matchClaudePermissionRule(rule, toolName, toolInput) {
			return true
		}
	}
	return false
}

// matchClaudePermissionRule reports whether a single Claude permission rule
// (e.g. "Bash", "Bash(npm run test:*)", "Read(~/.zshrc)") matches the given
// tool call. A rule with no "(specifier)" matches every call to that tool.
func matchClaudePermissionRule(rule, toolName string, toolInput map[string]interface{}) bool {
	ruleTool, specifier, hasSpecifier := splitClaudePermissionRule(rule)
	if ruleTool == "" {
		return false
	}
	if !strings.EqualFold(ruleTool, strings.TrimSpace(toolName)) {
		return false
	}
	if !hasSpecifier {
		return true
	}
	target := claudeRuleTarget(toolName, toolInput)
	return globMatch(specifier, target)
}

// splitClaudePermissionRule parses "Tool(specifier)" into its parts. The second
// return is the specifier (may be empty), the third reports whether a
// parenthesised specifier was present at all (an empty specifier "Tool()" is
// treated as tool-wide, same as "Tool").
func splitClaudePermissionRule(rule string) (tool, specifier string, hasSpecifier bool) {
	rule = strings.TrimSpace(rule)
	if rule == "" {
		return "", "", false
	}
	open := strings.Index(rule, "(")
	if open < 0 {
		return rule, "", false
	}
	tool = strings.TrimSpace(rule[:open])
	inner := rule[open+1:]
	if close := strings.LastIndex(inner, ")"); close >= 0 {
		inner = inner[:close]
	}
	inner = strings.TrimSpace(inner)
	if inner == "" {
		return tool, "", false
	}
	return tool, inner, true
}

// claudeRuleTarget extracts the string a specifier is matched against for a
// given tool. Bash matches the command, file tools match the path, and web
// tools match the URL. For anything else we fall back to the most common
// identifying field, then the empty string (which only an unrestricted rule
// would match).
func claudeRuleTarget(toolName string, toolInput map[string]interface{}) string {
	switch strings.ToLower(strings.TrimSpace(toolName)) {
	case "bash":
		return stringField(toolInput, "command")
	case "read", "edit", "write", "multiedit", "notebookedit":
		return firstNonEmptyString(
			stringField(toolInput, "file_path"),
			stringField(toolInput, "path"),
			stringField(toolInput, "notebook_path"),
		)
	case "webfetch", "websearch":
		return firstNonEmptyString(
			stringField(toolInput, "url"),
			stringField(toolInput, "domain"),
			stringField(toolInput, "query"),
		)
	default:
		return firstNonEmptyString(
			stringField(toolInput, "command"),
			stringField(toolInput, "file_path"),
			stringField(toolInput, "path"),
			stringField(toolInput, "url"),
		)
	}
}

func isClaudeEditTool(toolName string) bool {
	switch strings.ToLower(strings.TrimSpace(toolName)) {
	case "edit", "write", "multiedit", "notebookedit":
		return true
	default:
		return false
	}
}

// isClaudeSafeReadTool reports tools Claude Code typically auto-allows without
// prompting (read/search/list). Explicit deny/ask rules still win above.
func isClaudeSafeReadTool(toolName string) bool {
	switch strings.ToLower(strings.TrimSpace(toolName)) {
	case "read", "grep", "glob", "ls", "notebookread", "search", "semanticsearch":
		return true
	default:
		return false
	}
}

func stringField(input map[string]interface{}, key string) string {
	if input == nil {
		return ""
	}
	if value, ok := input[key].(string); ok {
		return strings.TrimSpace(value)
	}
	return ""
}

// globMatch performs a simple, case-sensitive glob match supporting the "*"
// wildcard (matching any run of characters, including empty). This covers
// Claude's common rule shapes such as "npm run test:*" and "https://api.*".
// A pattern with no "*" must match exactly.
func globMatch(pattern, s string) bool {
	if !strings.Contains(pattern, "*") {
		return pattern == s
	}
	parts := strings.Split(pattern, "*")
	// Anchor the first segment as a prefix.
	if first := parts[0]; first != "" {
		if !strings.HasPrefix(s, first) {
			return false
		}
		s = s[len(first):]
	}
	// Anchor the last segment as a suffix.
	last := parts[len(parts)-1]
	// Middle segments must appear in order.
	for _, part := range parts[1 : len(parts)-1] {
		if part == "" {
			continue
		}
		idx := strings.Index(s, part)
		if idx < 0 {
			return false
		}
		s = s[idx+len(part):]
	}
	if last == "" {
		return true
	}
	return strings.HasSuffix(s, last)
}
