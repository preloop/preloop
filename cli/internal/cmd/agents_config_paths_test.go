package cmd

import (
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func specByName(t *testing.T, name string) agentSpec {
	t.Helper()
	for _, spec := range agentSpecs {
		if strings.EqualFold(spec.Name, name) {
			return spec
		}
	}
	t.Fatalf("agent spec %q not found", name)
	return agentSpec{}
}

func containsPath(paths []string, want string) bool {
	for _, path := range paths {
		if filepath.Clean(path) == filepath.Clean(want) {
			return true
		}
	}
	return false
}

// Claude Desktop on Windows keeps its config under %APPDATA%\Claude, which the
// $HOME-relative ConfigPaths cannot express, so discovery used to miss it.
func TestClaudeDesktopConfigPathsIncludeAppData(t *testing.T) {
	appData := filepath.Join(t.TempDir(), "AppData", "Roaming")
	t.Setenv("APPDATA", appData)

	home := t.TempDir()
	paths := configPathsForAgentSpec(home, specByName(t, "Claude Desktop"))

	want := filepath.Join(appData, "Claude", "claude_desktop_config.json")
	if !containsPath(paths, want) {
		t.Fatalf("expected %q in discovered paths, got %v", want, paths)
	}
}

// The historical POSIX locations must keep working alongside the new ones.
func TestClaudeDesktopConfigPathsKeepPOSIXLocations(t *testing.T) {
	home := t.TempDir()
	paths := configPathsForAgentSpec(home, specByName(t, "Claude Desktop"))

	for _, rel := range []string{
		filepath.Join(".claude", "claude_desktop_config.json"),
		filepath.Join(".config", "claude", "claude_desktop_config.json"),
	} {
		if want := filepath.Join(home, rel); !containsPath(paths, want) {
			t.Errorf("expected %q in discovered paths, got %v", want, paths)
		}
	}
}

// On macOS Claude Desktop uses ~/Library/Application Support/Claude.
func TestClaudeDesktopConfigPathsIncludeMacApplicationSupport(t *testing.T) {
	if runtime.GOOS != "darwin" {
		t.Skip("macOS-specific config location")
	}
	home := t.TempDir()
	paths := configPathsForAgentSpec(home, specByName(t, "Claude Desktop"))

	want := filepath.Join(home, "Library", "Application Support", "Claude", "claude_desktop_config.json")
	if !containsPath(paths, want) {
		t.Fatalf("expected %q in discovered paths, got %v", want, paths)
	}
}

// Other agents must not pick up the Claude Desktop locations.
func TestConfigPathsForOtherAgentsUnaffected(t *testing.T) {
	appData := filepath.Join(t.TempDir(), "AppData", "Roaming")
	t.Setenv("APPDATA", appData)

	home := t.TempDir()
	paths := configPathsForAgentSpec(home, specByName(t, "Cursor"))

	for _, path := range paths {
		if strings.Contains(path, "claude_desktop_config.json") {
			t.Fatalf("Cursor discovery leaked a Claude Desktop path: %v", paths)
		}
	}
	if want := filepath.Join(home, ".cursor", "mcp.json"); !containsPath(paths, want) {
		t.Fatalf("expected %q in discovered paths, got %v", want, paths)
	}
}

// userConfigDirs must ignore unusable values rather than emitting them.
func TestUserConfigDirsSkipsRelativeAndDuplicateEntries(t *testing.T) {
	t.Setenv("APPDATA", "not-an-absolute-path")
	t.Setenv("XDG_CONFIG_HOME", "")

	home := t.TempDir()
	dirs := userConfigDirs(home)

	seen := map[string]struct{}{}
	for _, dir := range dirs {
		if !filepath.IsAbs(dir) {
			t.Errorf("non-absolute config dir returned: %q", dir)
		}
		if _, dup := seen[dir]; dup {
			t.Errorf("duplicate config dir returned: %q", dir)
		}
		seen[dir] = struct{}{}
	}
}
