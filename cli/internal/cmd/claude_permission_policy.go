package cmd

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
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

// claudeManagedSettingsPath returns the enterprise managed-settings file Claude
// Code consults on this platform. It is a package variable so tests can point
// it at a temp file.
var claudeManagedSettingsPath = func() string {
	switch runtime.GOOS {
	case "darwin":
		return "/Library/Application Support/ClaudeCode/managed-settings.json"
	case "windows":
		return `C:\ProgramData\ClaudeCode\managed-settings.json`
	default:
		return "/etc/claude-code/managed-settings.json"
	}
}

// claudeSettingsPaths returns every settings file Claude Code merges for the
// given session cwd, ordered lowest-precedence first: user, user local,
// project, project local, managed (enterprise). Reading in this order lets a
// simple last-writer-wins loop implement the defaultMode precedence
// managed > project local > project > user local > user.
func claudeSettingsPaths(cwd string) ([]string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, fmt.Errorf("failed to resolve home directory: %w", err)
	}
	paths := []string{
		filepath.Join(home, ".claude", "settings.json"),
		filepath.Join(home, ".claude", "settings.local.json"),
	}
	if cwd = strings.TrimSpace(cwd); cwd != "" {
		paths = append(paths,
			filepath.Join(cwd, ".claude", "settings.json"),
			filepath.Join(cwd, ".claude", "settings.local.json"),
		)
	}
	return append(paths, claudeManagedSettingsPath()), nil
}

// loadClaudePermissionPolicy reads every settings file Claude Code would merge
// for a session running in cwd (user, user local, project, project local, and
// enterprise managed settings) into a single policy. Allow/deny/ask lists are
// unioned across all files — deny is evaluated first at decision time, so a
// managed or project deny always wins regardless of merge order. defaultMode
// is last-writer-wins in ascending precedence order (managed wins). A missing
// settings file is not an error — it simply contributes nothing.
func loadClaudePermissionPolicy(cwd string) (claudePermissionPolicy, error) {
	paths, err := claudeSettingsPaths(cwd)
	if err != nil {
		return claudePermissionPolicy{}, err
	}
	policy := claudePermissionPolicy{}
	for _, path := range paths {
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
//
// Workspace edits deliberately have no auto-allow step here: stock Claude
// Code prompts for Edit/Write in default permission mode, so the mirror asks
// too. Only Cursor's policy path consults the workspace root (see
// cursorPermissionClientDecision / isLocalWorkspaceEdit), because
// auto-applying edits IS Cursor's default behavior.
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
	// Workspace edits deliberately stay "ask": in default permission mode
	// Claude Code itself prompts for Edit/Write, so auto-allowing them here
	// would swallow approval requests the operator expects to see in Preloop.
	// The agent's own acceptEdits mode (handled above) and explicit allow
	// rules remain the ways edits skip approval — mirroring, never widening,
	// the agent's real policy. (Cursor keeps its own workspace-edit allow in
	// cursorPermissionClientDecision because auto-applying edits IS Cursor's
	// default behavior.)
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
	if strings.EqualFold(strings.TrimSpace(ruleTool), "Bash") {
		return matchClaudeBashSpecifier(specifier, target)
	}
	return globMatch(specifier, target)
}

// matchClaudeBashSpecifier implements Claude Code's Bash rule semantics:
//
//   - "Bash(foo)"    — exact match: the command must equal "foo".
//   - "Bash(foo:*)"  — prefix match: the trailing ":*" is Claude Code's
//     prefix-match marker, NOT a literal colon + glob. It matches the command
//     "foo" itself or "foo" continued at a token boundary.
//   - "Bash(git *)"  — legacy space-style glob, preserved for backwards compat
//     with rules users already wrote against our earlier matcher.
func matchClaudeBashSpecifier(specifier, command string) bool {
	if strings.HasSuffix(specifier, ":*") {
		return matchClaudeBashPrefix(strings.TrimSuffix(specifier, ":*"), command)
	}
	return globMatch(specifier, command)
}

// matchClaudeBashPrefix reports whether command matches the "Bash(prefix:*)"
// prefix rule. Semantics decision: the prefix match is token-wise, not raw
// byte-wise. The command matches when it equals the prefix exactly, or starts
// with the prefix followed by a token boundary — a space (new argument, e.g.
// "npm run test -- --watch") or a colon (script-name continuation, e.g.
// "npm run test:unit" under "Bash(npm run test:*)"). A raw continuation of the
// last word ("npm run testx") does NOT match: it is a different command word,
// and treating it as a match would let "Bash(git:*)" cover "github ...".
func matchClaudeBashPrefix(prefix, command string) bool {
	if prefix == "" {
		// "Bash(:*)" degenerates to match-everything, same as "Bash(*)".
		return true
	}
	if command == prefix {
		return true
	}
	if !strings.HasPrefix(command, prefix) {
		return false
	}
	switch command[len(prefix)] {
	case ' ', ':':
		return true
	default:
		return false
	}
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
	case "read", "edit", "write", "multiedit", "notebookedit", "editnotebook",
		"strreplace", "search_replace":
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
	case "edit", "write", "multiedit", "notebookedit", "editnotebook",
		"strreplace", "search_replace":
		return true
	default:
		return false
	}
}

// isLocalWorkspaceEdit reports whether an edit tool targets a relative path or
// a path beneath one of the workspace roots. An empty path or a tilde path
// cannot be verified as local, so both deliberately remain would-ask calls.
func isLocalWorkspaceEdit(
	toolName string,
	toolInput map[string]interface{},
	workspaceRoots ...string,
) bool {
	if !isClaudeEditTool(toolName) {
		return false
	}
	path := claudeRuleTarget(toolName, toolInput)
	if path == "" || strings.HasPrefix(path, "~") {
		return false
	}
	if !isRootedPath(path) {
		// Relative paths are treated as workspace-local, but reject escapes
		// like "../.ssh/id_rsa" that Clean would still leave outside the tree.
		cleanRel := filepath.Clean(path)
		if cleanRel == ".." ||
			strings.HasPrefix(cleanRel, ".."+string(filepath.Separator)) {
			return false
		}
		return true
	}
	cleanPath := filepath.Clean(path)
	for _, root := range workspaceRoots {
		root = strings.TrimSpace(root)
		if root == "" || !isRootedPath(root) {
			continue
		}
		rel, err := filepath.Rel(filepath.Clean(root), cleanPath)
		if err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			return true
		}
	}
	return false
}

// isRootedPath reports absolute-intent paths. filepath.IsAbs alone is not
// enough on Windows: "/etc/passwd" has no drive letter so IsAbs is false
// there, which would send slash-rooted paths down the relative branch and
// auto-allow them as "workspace-local" on Windows only. Agents emit
// slash-rooted paths regardless of host OS, so treat any slash- or
// backslash-prefixed path as rooted everywhere.
func isRootedPath(path string) bool {
	return filepath.IsAbs(path) ||
		strings.HasPrefix(path, "/") ||
		strings.HasPrefix(path, "\\")
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
