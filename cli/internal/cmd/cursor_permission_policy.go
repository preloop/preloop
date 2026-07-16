package cmd

import (
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
)

// cursorPermissionPolicy models the subset of Cursor's permissions.json that
// governs whether a shell or MCP call would auto-run without prompting.
// See https://cursor.com/docs/reference/permissions
type cursorPermissionPolicy struct {
	TerminalAllowlist []string
	MCPAllowlist      []string
	HasTerminalKey    bool
	HasMCPKey         bool
}

type cursorPermissionsDocument struct {
	TerminalAllowlist []string `json:"terminalAllowlist"`
	MCPAllowlist      []string `json:"mcpAllowlist"`
}

// discoverCursorPermissionPolicyPaths returns the absolute paths Cursor would
// consult for allowlists: the per-user file plus any per-workspace files under
// the given workspace roots (cwd / workspace_roots from the hook event).
func discoverCursorPermissionPolicyPaths(workspaceRoots []string) []string {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil
	}
	paths := []string{filepath.Join(home, ".cursor", "permissions.json")}
	seen := map[string]struct{}{paths[0]: {}}
	for _, root := range workspaceRoots {
		root = strings.TrimSpace(root)
		if root == "" {
			continue
		}
		path := filepath.Join(root, ".cursor", "permissions.json")
		if _, ok := seen[path]; ok {
			continue
		}
		seen[path] = struct{}{}
		paths = append(paths, path)
	}
	return paths
}

// loadCursorPermissionPolicy reads and merges permissions.json files. Arrays
// are concatenated (Cursor's semantics). Missing files are skipped.
func loadCursorPermissionPolicy(paths []string) (cursorPermissionPolicy, error) {
	policy := cursorPermissionPolicy{}
	for _, path := range paths {
		doc, ok, err := readCursorPermissionsDocument(path)
		if err != nil {
			return cursorPermissionPolicy{}, err
		}
		if !ok {
			continue
		}
		if doc.terminalKeySet {
			policy.HasTerminalKey = true
			policy.TerminalAllowlist = append(policy.TerminalAllowlist, doc.TerminalAllowlist...)
		}
		if doc.mcpKeySet {
			policy.HasMCPKey = true
			policy.MCPAllowlist = append(policy.MCPAllowlist, doc.MCPAllowlist...)
		}
	}
	return policy, nil
}

type cursorPermissionsDocWithFlags struct {
	cursorPermissionsDocument
	terminalKeySet bool
	mcpKeySet      bool
}

func readCursorPermissionsDocument(path string) (cursorPermissionsDocWithFlags, bool, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return cursorPermissionsDocWithFlags{}, false, nil
		}
		return cursorPermissionsDocWithFlags{}, false, fmt.Errorf("failed to read %s: %w", path, err)
	}
	trimmed := strings.TrimSpace(string(data))
	if trimmed == "" {
		return cursorPermissionsDocWithFlags{}, false, nil
	}
	// Strip JSONC-style // comments loosely enough for Cursor's format.
	cleaned := stripJSONCComments(trimmed)
	var raw map[string]json.RawMessage
	if err := json.Unmarshal([]byte(cleaned), &raw); err != nil {
		return cursorPermissionsDocWithFlags{}, false, fmt.Errorf("failed to parse %s: %w", path, err)
	}
	out := cursorPermissionsDocWithFlags{}
	if value, ok := raw["terminalAllowlist"]; ok {
		out.terminalKeySet = true
		_ = json.Unmarshal(value, &out.TerminalAllowlist)
	}
	if value, ok := raw["mcpAllowlist"]; ok {
		out.mcpKeySet = true
		_ = json.Unmarshal(value, &out.MCPAllowlist)
	}
	return out, true, nil
}

// stripJSONCComments removes // line comments outside of strings. Good enough
// for Cursor's permissions.json; block comments are left untouched.
func stripJSONCComments(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	inString := false
	escaped := false
	for i := 0; i < len(s); i++ {
		ch := s[i]
		if inString {
			b.WriteByte(ch)
			if escaped {
				escaped = false
				continue
			}
			if ch == '\\' {
				escaped = true
				continue
			}
			if ch == '"' {
				inString = false
			}
			continue
		}
		if ch == '"' {
			inString = true
			b.WriteByte(ch)
			continue
		}
		if ch == '/' && i+1 < len(s) && s[i+1] == '/' {
			for i < len(s) && s[i] != '\n' {
				i++
			}
			if i < len(s) {
				b.WriteByte('\n')
			}
			continue
		}
		b.WriteByte(ch)
	}
	return b.String()
}

// evaluateCursorPermissionPolicy returns "allow" or "ask" for a Cursor hook
// event. Precedence matches the remote-approvals plan:
//  1. Preloop-owned MCP tools → allow (MCP firewall already gates them)
//  2. terminalAllowlist / mcpAllowlist match → allow
//  3. sandboxed shell → allow
//  4. otherwise → ask
func evaluateCursorPermissionPolicy(
	policy cursorPermissionPolicy,
	toolName string,
	toolInput map[string]interface{},
	event map[string]interface{},
	baseURL string,
) string {
	command := firstNonEmptyString(
		stringField(toolInput, "command"),
		firstStringField(event, "command"),
	)
	isShell := strings.EqualFold(strings.TrimSpace(toolName), "Shell") ||
		(strings.TrimSpace(toolName) == "" && command != "")

	if !isShell && isPreloopMCPTool(event, toolName, baseURL) {
		return "allow"
	}
	if isShell {
		if matchCursorTerminalAllowlist(policy.TerminalAllowlist, command) {
			return "allow"
		}
		if sandboxTruthy(event["sandbox"]) {
			return "allow"
		}
		return "ask"
	}
	if matchCursorMCPAllowlist(policy.MCPAllowlist, event, toolName) {
		return "allow"
	}
	return "ask"
}

func sandboxTruthy(value interface{}) bool {
	switch typed := value.(type) {
	case bool:
		return typed
	case string:
		switch strings.ToLower(strings.TrimSpace(typed)) {
		case "1", "true", "yes", "on":
			return true
		}
	}
	return false
}

// matchCursorTerminalAllowlist implements Cursor's prefix / cmd:args* matching.
func matchCursorTerminalAllowlist(allowlist []string, command string) bool {
	command = strings.TrimSpace(command)
	if command == "" || len(allowlist) == 0 {
		return false
	}
	for _, entry := range allowlist {
		entry = strings.TrimSpace(entry)
		if entry == "" {
			continue
		}
		if matchCursorTerminalEntry(entry, command) {
			return true
		}
	}
	return false
}

func matchCursorTerminalEntry(entry, command string) bool {
	// "npm:install*" → base "npm", args glob "install*"
	if colon := strings.Index(entry, ":"); colon >= 0 && !strings.Contains(entry[:colon], " ") {
		base := entry[:colon]
		argsGlob := entry[colon+1:]
		fields := strings.Fields(command)
		if len(fields) == 0 || fields[0] != base {
			return false
		}
		rest := strings.Join(fields[1:], " ")
		return globMatch(argsGlob, rest)
	}
	if command == entry {
		return true
	}
	return strings.HasPrefix(command, entry+" ")
}

// matchCursorMCPAllowlist matches server:tool patterns (case-insensitive, * wildcards).
func matchCursorMCPAllowlist(allowlist []string, event map[string]interface{}, toolName string) bool {
	if len(allowlist) == 0 {
		return false
	}
	server := cursorMCPServerName(event)
	tool := strings.TrimSpace(toolName)
	if tool == "" {
		return false
	}
	for _, entry := range allowlist {
		entry = strings.TrimSpace(entry)
		if entry == "" || !strings.Contains(entry, ":") {
			continue
		}
		parts := strings.SplitN(entry, ":", 2)
		if len(parts) != 2 {
			continue
		}
		if globMatchFold(parts[0], server) && globMatchFold(parts[1], tool) {
			return true
		}
	}
	return false
}

func cursorMCPServerName(event map[string]interface{}) string {
	if name := firstStringField(event, "server_name", "server", "mcp_server"); name != "" {
		return name
	}
	if command := firstStringField(event, "command"); command != "" {
		base := filepath.Base(command)
		return strings.TrimSuffix(base, filepath.Ext(base))
	}
	if rawURL := firstStringField(event, "url"); rawURL != "" {
		if parsed, err := url.Parse(rawURL); err == nil && parsed.Host != "" {
			return parsed.Host
		}
	}
	return ""
}

// isPreloopMCPTool reports whether this MCP invocation targets the Preloop
// firewall (already governed by require_approval), so the Cursor hook should
// not double-gate it.
func isPreloopMCPTool(event map[string]interface{}, toolName, baseURL string) bool {
	server := strings.ToLower(cursorMCPServerName(event))
	if strings.Contains(server, "preloop") {
		return true
	}
	if strings.Contains(strings.ToLower(strings.TrimSpace(toolName)), "preloop") {
		return true
	}
	rawURL := firstStringField(event, "url")
	if rawURL == "" {
		return false
	}
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return false
	}
	host := strings.ToLower(parsed.Host)
	if strings.Contains(host, "preloop") {
		return true
	}
	base := strings.TrimSpace(baseURL)
	if base == "" {
		return false
	}
	baseParsed, err := url.Parse(base)
	if err != nil {
		return false
	}
	return host != "" && strings.EqualFold(host, baseParsed.Host)
}

// safeReadShellCommands are leading tokens that are obviously non-mutating
// regardless of arguments (redirections and command chaining are rejected
// before this list is consulted). env, find, and git need argument checks and
// are handled in isSafeReadPipelineStage instead.
var safeReadShellCommands = map[string]bool{
	"ls":       true,
	"pwd":      true,
	"cat":      true,
	"head":     true,
	"tail":     true,
	"less":     true,
	"wc":       true,
	"echo":     true,
	"which":    true,
	"whoami":   true,
	"printenv": true,
	"grep":     true,
	"rg":       true,
	"stat":     true,
	"file":     true,
	"du":       true,
	"df":       true,
	"uname":    true,
	"date":     true,
}

// isSafeReadShellCommand reports whether a shell command consists solely of
// obviously read-only commands — either a single safe command or a plain "|"
// pipeline whose every stage leads with a safe command. Anything that could
// chain or hide a mutation (";", "&&", "||", "&", redirections, backticks,
// "$(", "<(", newlines) is rejected outright. The parse is deliberately
// conservative: when in ANY doubt, return false and let the call escalate.
func isSafeReadShellCommand(command string) bool {
	command = strings.TrimSpace(command)
	if command == "" {
		return false
	}
	// Reject chaining/redirection metacharacters. "&" covers "&&" and
	// backgrounding; "<" covers "<(" process substitution; "$(" and backticks
	// cover command substitution. "||" survives these checks but produces an
	// empty pipeline stage below, which is rejected.
	for _, meta := range []string{";", "&", ">", "<", "`", "$(", "\n", "\r"} {
		if strings.Contains(command, meta) {
			return false
		}
	}
	for _, stage := range strings.Split(command, "|") {
		if !isSafeReadPipelineStage(stage) {
			return false
		}
	}
	return true
}

// isSafeReadPipelineStage checks one pipeline stage against the built-in
// read-only allowlist.
func isSafeReadPipelineStage(stage string) bool {
	fields := strings.Fields(stage)
	if len(fields) == 0 {
		return false
	}
	head, args := fields[0], fields[1:]
	switch head {
	case "env":
		// "env" alone prints the environment; with args it runs a command.
		return len(args) == 0
	case "find":
		// find is read-only unless asked to act on matches.
		for _, arg := range args {
			switch arg {
			case "-delete", "-exec", "-execdir", "-ok", "-okdir":
				return false
			}
		}
		return true
	case "git":
		return isSafeReadGitArgs(args)
	default:
		return safeReadShellCommands[head]
	}
}

// isSafeReadGitArgs allows only the read-only git subcommands: status, log,
// diff, show, and branch listing ("git branch" with only dash-flags, none of
// the mutating ones). "--output" flags are rejected because they write files.
func isSafeReadGitArgs(args []string) bool {
	if len(args) == 0 {
		return false
	}
	switch args[0] {
	case "status", "log", "diff", "show":
		for _, arg := range args[1:] {
			if strings.HasPrefix(arg, "--output") {
				return false
			}
		}
		return true
	case "branch":
		for _, arg := range args[1:] {
			if !strings.HasPrefix(arg, "-") {
				return false
			}
			switch arg {
			case "-d", "-D", "-m", "-M", "-c", "-C", "-f", "--force",
				"--delete", "--move", "--copy", "--edit-description",
				"--set-upstream-to", "--unset-upstream", "-u", "-t", "--track":
				return false
			}
		}
		return true
	default:
		return false
	}
}

func globMatchFold(pattern, s string) bool {
	return globMatch(strings.ToLower(pattern), strings.ToLower(s))
}

// workspaceRootsFromEvent extracts cwd + workspace_roots from a Cursor hook event.
func workspaceRootsFromEvent(event map[string]interface{}) []string {
	var roots []string
	if cwd := firstStringField(event, "cwd"); cwd != "" {
		roots = append(roots, cwd)
	}
	switch typed := event["workspace_roots"].(type) {
	case []interface{}:
		for _, item := range typed {
			if text, ok := item.(string); ok {
				if trimmed := strings.TrimSpace(text); trimmed != "" {
					roots = append(roots, trimmed)
				}
			}
		}
	case []string:
		for _, text := range typed {
			if trimmed := strings.TrimSpace(text); trimmed != "" {
				roots = append(roots, trimmed)
			}
		}
	}
	return roots
}

// summarizeCursorPermissionPolicy returns a short onboard-facing summary.
func summarizeCursorPermissionPolicy(policy cursorPermissionPolicy, paths []string) []string {
	existing := 0
	for _, path := range paths {
		if _, err := os.Stat(path); err == nil {
			existing++
		}
	}
	lines := []string{
		fmt.Sprintf("policy files: %d readable of %d discovered", existing, len(paths)),
		fmt.Sprintf("terminalAllowlist: %d entries", len(policy.TerminalAllowlist)),
		fmt.Sprintf("mcpAllowlist: %d entries", len(policy.MCPAllowlist)),
		"built-in safe-read allowlist: read-only shell commands (ls, cat, git status, ...)",
		"auto-allow without approval (disable via safe_read_auto_allow in permission_hook.json)",
	}
	if existing == 0 {
		lines = append(lines,
			"Note: IDE-only allowlists (not in permissions.json) are not readable;",
			"export them to ~/.cursor/permissions.json to honor them remotely.",
		)
	}
	return lines
}
