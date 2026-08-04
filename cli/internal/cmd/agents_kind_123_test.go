package cmd

import (
	"crypto/sha256"
	"encoding/hex"
	"path/filepath"
	"testing"
)

// TestManagedAgentKindForAgent pins the product kinds that previously all
// collapsed into the generic "desktop_agent" bucket (#123).
func TestManagedAgentKindForAgent(t *testing.T) {
	cases := map[string]string{
		"Cursor":           "cursor",
		"Windsurf":         "windsurf",
		"VSCode / Copilot": "vscode",
		"Antigravity":      "antigravity",
		"Devin":            "devin",
		// Agents whose product and transport already agree.
		"Claude Code":    "claude_code",
		"Claude Desktop": "claude_desktop",
		"Codex CLI":      "codex",
		"Gemini CLI":     "gemini_cli",
		"OpenCode":       "opencode",
		"OpenClaw":       "openclaw",
		// Unknown agents keep the generic transport kind.
		"Totally Unknown Agent": "desktop_agent",
	}
	for name, want := range cases {
		if got := managedAgentKindForAgent(name); got != want {
			t.Errorf("managedAgentKindForAgent(%q) = %q, want %q", name, got, want)
		}
	}
}

// TestSourceTypeUnchangedForRekeyedProducts is the regression guard for the
// identity trap: the durable v2 principal id is derived from the *source
// type*, so these agents must keep reporting "desktop_agent" forever. If this
// test fails, every existing Cursor/Windsurf/VS Code enrollment in the field
// has just been silently re-keyed.
func TestSourceTypeUnchangedForRekeyedProducts(t *testing.T) {
	for _, name := range []string{"Cursor", "Windsurf", "VSCode / Copilot", "Antigravity", "Devin"} {
		if got := runtimeSessionSourceTypeForAgent(name); got != "desktop_agent" {
			t.Errorf("runtimeSessionSourceTypeForAgent(%q) = %q, want desktop_agent", name, got)
		}
	}
}

// TestPrincipalIDStableAcrossKindChange proves the v2 identity of an existing
// Cursor install is byte-identical before and after this change.
func TestPrincipalIDStableAcrossKindChange(t *testing.T) {
	const configPath = "/home/u/.cursor/mcp.json"
	agent := AgentConfig{Name: "Cursor", ConfigPath: configPath}

	// Recompute the pre-fix derivation independently rather than hardcoding a
	// digest: the id embeds the local hostname, so a literal golden value
	// would only hold on the machine that captured it. The path must go
	// through filepath.Clean for the same reason the production derivation
	// does, since it rewrites separators on Windows.
	host, _ := enrollmentHostnameLabel()
	nul := string([]byte{0})
	sum := sha256.Sum256([]byte("v2" + nul + host + nul + "desktop_agent" + nul + filepath.Clean(configPath)))
	want := "desktop-agent-" + hex.EncodeToString(sum[:6])

	if got := stableRuntimePrincipalIDForAgent(agent, ""); got != want {
		t.Fatalf("v2 principal id changed: got %q want %q (existing enrollments would fork)", got, want)
	}
	if got := principalIdentityForAgent(agent).SourceType; got != "desktop_agent" {
		t.Fatalf("principal identity source type = %q, want desktop_agent", got)
	}
	// Lookup must still search the transport bucket the agent is stored under.
	if got := managedAgentLookupSourceTypes(agent); len(got) != 1 || got[0] != "desktop_agent" {
		t.Fatalf("lookup source types = %v, want [desktop_agent]", got)
	}
}
