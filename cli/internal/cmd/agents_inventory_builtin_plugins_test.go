package cmd

import (
	"path/filepath"
	"testing"
)

// Fixture profiles elsewhere in this package model installs without Claude
// Code's compiled-in plugins; the tests below opt back in.
func init() { claudeDefaultBuiltinPlugins = nil }

func withClaudeBuiltinPlugins(t *testing.T) {
	t.Helper()
	previous := claudeDefaultBuiltinPlugins
	claudeDefaultBuiltinPlugins = []string{"cc-plugin-agents-md", "cc-plugin-plugin-authoring", "cc-plugin-privacy"}
	t.Cleanup(func() { claudeDefaultBuiltinPlugins = previous })
}

// protects=built-in plugins Claude Code lists without an install record;
// fails_when=they are missing, carry a marketplace source, or ignore a
// settings opt-out; no_claim=gated built-ins Claude Code does not list.
func TestClaudeBuiltinPluginsAreReportedAsBuiltin(t *testing.T) {
	withClaudeBuiltinPlugins(t)
	home := filepath.Join(t.TempDir(), "profile")
	writeInventoryFixture(t, home, ".claude/settings.json", `{"enabledPlugins":{"cc-plugin-privacy@builtin":false}}`)

	extensions, errors := collectInventoryExtensions(home, "claude-code", "salt", false)
	if len(errors) != 0 {
		t.Fatalf("errors = %v", errors)
	}
	got := map[string]inventoryExtension{}
	for _, ext := range extensions {
		if ext.Kind == "plugin" {
			got[ext.ID] = ext
		}
	}
	for _, id := range []string{"cc-plugin-agents-md@builtin", "cc-plugin-plugin-authoring@builtin", "cc-plugin-privacy@builtin"} {
		ext, ok := got[id]
		if !ok {
			t.Fatalf("%s missing from %+v", id, extensions)
		}
		if ext.SourceKind != "builtin" || ext.Scope != "user" || ext.IDHashed {
			t.Fatalf("%s = %+v, want a clear user-scope builtin", id, ext)
		}
	}
	if got["cc-plugin-agents-md@builtin"].Enabled != "true" || got["cc-plugin-privacy@builtin"].Enabled != "false" {
		t.Fatalf("enabled state: %+v", got)
	}
}

func TestClaudeBuiltinPluginsNeedAClaudeProfile(t *testing.T) {
	withClaudeBuiltinPlugins(t)
	home := filepath.Join(t.TempDir(), "profile")
	writeInventoryFixture(t, home, ".codex/config.toml", "")

	extensions, _ := collectInventoryExtensions(home, "claude-code", "salt", false)
	for _, ext := range extensions {
		if ext.SourceKind == "builtin" {
			t.Fatalf("unexpected %+v without ~/.claude", ext)
		}
	}
}
