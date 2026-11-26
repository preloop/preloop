package cmd

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func installedPluginFixture(t *testing.T, agent, name, home string) string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", "installed-plugins", agent, name))
	if err != nil {
		t.Fatal(err)
	}
	// Substitute JSON-escaped paths, including Windows drive separators.
	escaped, _ := json.Marshal(home)
	return strings.ReplaceAll(string(data), "{{HOME}}", string(escaped[1:len(escaped)-1]))
}

func installClaudeLayoutFixture(t *testing.T, home string) {
	t.Helper()
	for _, name := range []string{"installed_plugins.json", "known_marketplaces.json"} {
		writeInventoryFixture(t, home, ".claude/plugins/"+name, installedPluginFixture(t, "claude", name, home))
	}
	writeInventoryFixture(t, home, ".claude/settings.json", installedPluginFixture(t, "claude", "settings.json", home))
	for _, p := range []struct{ name, market, version string }{{"code-review", "claude-plugins-official", "1.0.1"}, {"superpowers", "superpowers-marketplace", "7.0.2"}} {
		path := filepath.Join(".claude", "plugins", "cache", p.market, p.name, p.version)
		writeInventoryFixture(t, home, filepath.Join(path, ".claude-plugin", "plugin.json"), installedPluginFixture(t, "claude", p.name+"-manifest.json", home))
		writeInventoryFixture(t, home, filepath.Join(path, "skills", "example", "SKILL.md"), "synthetic private plugin content")
	}
}

// protects=real plugin-manager installs reach the listing and v3 with hashes;
// fails_when=registries are ignored or private metadata escapes;
// no_claim=scrubbed Claude Code 2.1.332 and Codex 0.172.0 fixtures, no runtime load.
func TestInstalledPluginLayouts(t *testing.T) {
	for _, agent := range []string{"claude-code", "codex-cli"} {
		t.Run(agent, func(t *testing.T) {
			home := extensionHome(t)
			want := 1
			if agent == "claude-code" {
				installClaudeLayoutFixture(t, home)
				want = 4
			} else {
				writeInventoryFixture(t, home, ".codex/config.toml", installedPluginFixture(t, "codex", "config.toml", home))
				writeInventoryFixture(t, home, ".codex/plugins/cache/superpowers-marketplace/superpowers/7.0.2/.codex-plugin/plugin.json", installedPluginFixture(t, "codex", "plugin.json", home))
			}
			extensions, codes := collectInventoryExtensions(home, agent, "salt", false)
			if len(codes) != 0 || len(extensions) != want {
				t.Fatalf("extensions=%+v codes=%v", extensions, codes)
			}
			for _, ext := range extensions {
				if ext.Kind == "skill" {
					if ext.Details == nil || ext.Details.ScriptCount == nil {
						t.Fatal("missing bundled skill metadata")
					}
					// The parent can be withheld when it contains a private
					// home-path segment, including a temporary-directory name.
					if ext.Details.Plugin != "" && ext.Details.Plugin != "code-review@claude-plugins-official" && ext.Details.Plugin != "superpowers@superpowers-marketplace" {
						t.Fatal("unexpected plugin parent")
					}
					continue
				}
				kind, version := "third_party_marketplace", "7.0.2"
				if ext.ID == "code-review@claude-plugins-official" {
					kind, version = "official_marketplace", "1.0.1"
				} else if ext.ID != "superpowers@superpowers-marketplace" {
					t.Fatalf("unexpected plugin: %+v", ext)
				}
				if ext.SourceKind != kind || ext.Version != version || ext.Enabled != "true" || ext.HashStatus != contentHashOK || !extensionContentHashPattern.MatchString(ext.ContentHash) {
					t.Fatalf("plugin=%+v", ext)
				}
			}
			stdout, _, err := runExtensions(t, "--agent", agent, "--kind", "plugin")
			if err != nil || !strings.Contains(stdout, "superpowers@superpowers-marketplace") || !strings.Contains(stdout, "Scan complete.") {
				t.Fatalf("listing: %s %v", stdout, err)
			}
			scan := collectAgentInventory(inventoryTestDependencies(t, home))
			found := false
			for _, app := range scan.Apps {
				if app.AppID == agent {
					found = true
					if app.Completeness != "complete" || len(app.Extensions) != want {
						t.Fatalf("app=%+v", app)
					}
				}
			}
			if !found {
				t.Fatal("app missing from v3")
			}
			data, _ := json.Marshal(scan)
			for _, private := range []string{home, "example.com", "example/official-plugins", "synthetic private plugin content", "installPath", "gitCommitSha"} {
				if strings.Contains(string(data), private) {
					t.Fatalf("private field escaped: %s", private)
				}
			}
		})
	}
}

// protects=registry failures cannot claim a complete empty scan;
// fails_when=malformed, unsupported or unreadable installs disappear silently;
// no_claim=permissions test skips root and Windows.
func TestInstalledPluginRegistryFailures(t *testing.T) {
	for _, tc := range []struct{ name, body, code string }{
		{"invalid-json", "{", "extension_malformed"},
		{"unknown-version", `{"version":99,"plugins":{}}`, "extension_unsupported_version"},
		{"missing-version", `{"plugins":{}}`, "extension_malformed"},
		{"missing-plugins", `{"version":2}`, "extension_malformed"},
		{"null-plugins", `{"version":2,"plugins":null}`, "extension_malformed"},
		{"wrong-records", `{"version":2,"plugins":{"p@m":{}}}`, "extension_malformed"},
		{"null-record", `{"version":2,"plugins":{"p@m":[null]}}`, "extension_malformed"},
		{"empty-records", `{"version":2,"plugins":{"p@m":[]}}`, "extension_malformed"},
		{"missing-path", `{"version":2,"plugins":{"p@m":[{"scope":"user"}]}}`, "extension_malformed"},
		{"unreadable", `{"version":2,"plugins":{}}`, "extension_unreadable"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			home := extensionHome(t)
			writeInventoryFixture(t, home, ".claude/settings.json", `{}`)
			path := writeInventoryFixture(t, home, ".claude/plugins/installed_plugins.json", tc.body)
			if tc.name == "unreadable" {
				if runtime.GOOS == "windows" || os.Geteuid() == 0 {
					t.Skip("permission modes unavailable")
				}
				if err := os.Chmod(path, 0); err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() { _ = os.Chmod(path, 0600) })
			}
			_, codes := collectInventoryExtensions(home, "claude-code", "", false)
			if !containsExtensionError(codes, tc.code) {
				t.Fatalf("codes=%v want %s", codes, tc.code)
			}
			stdout, _, err := runExtensions(t, "--agent", "claude-code")
			if ProcessExitCode(err) != 2 || !strings.Contains(stdout, "Scan incomplete:") {
				t.Fatalf("listing=%s err=%v", stdout, err)
			}
			scan := collectAgentInventory(inventoryTestDependencies(t, home))
			for _, app := range scan.Apps {
				if app.AppID == "claude-code" && app.Completeness != "partial" {
					t.Fatalf("app=%+v", app)
				}
			}
			found := false
			for _, probe := range scan.ProbeResults {
				if probe.AppID == "claude-code" && probe.Kind == "extensions" && probe.ErrorCode == tc.code {
					found = true
				}
			}
			if !found {
				t.Fatalf("reason missing: %+v", scan.ProbeResults)
			}
		})
	}
}

// writeClaudePluginRecord keeps synthetic policy/hash tests on the real registry
// shape; the captured fixtures above independently test the vendor layout.
func writeClaudePluginRecord(t *testing.T, home, name, body string) {
	t.Helper()
	var record map[string]any
	if err := json.Unmarshal([]byte(body), &record); err != nil {
		t.Fatal(err)
	}
	if market, ok := record["marketplace"].(string); ok && market != "" {
		name += "@" + market
	}
	record["scope"] = "user"
	if record["installPath"] == nil {
		path := filepath.Join(".claude", "plugins", "cache", "fixture", strings.Split(name, "@")[0], "local")
		record["installPath"] = path
		if err := os.MkdirAll(filepath.Join(home, path), 0700); err != nil {
			t.Fatal(err)
		}
	}
	path := filepath.Join(home, ".claude", "plugins", "installed_plugins.json")
	registry := struct {
		Version int                         `json:"version"`
		Plugins map[string][]map[string]any `json:"plugins"`
	}{Version: 2, Plugins: map[string][]map[string]any{}}
	if data, err := os.ReadFile(path); err == nil {
		if err := json.Unmarshal(data, &registry); err != nil {
			t.Fatal(err)
		}
	} else if !os.IsNotExist(err) {
		t.Fatal(err)
	}
	registry.Plugins[name] = []map[string]any{record}
	data, err := json.Marshal(registry)
	if err != nil {
		t.Fatal(err)
	}
	writeInventoryFixture(t, home, ".claude/plugins/installed_plugins.json", string(data))
}

// protects=installed versions and enabled states, not abandoned cache entries;
// fails_when=stale versions win, disabled plugins disappear or content is unhashed;
// no_claim=cache mutations are synthetic, base registry captured from real installs.
func TestInstalledPluginStateAndContent(t *testing.T) {
	home := t.TempDir()
	installClaudeLayoutFixture(t, home)
	writeInventoryFixture(t, home, ".claude/settings.json", `{"enabledPlugins":{"code-review@claude-plugins-official":false,"superpowers@superpowers-marketplace":true}}`)
	writeInventoryFixture(t, home, ".claude/plugins/cache/claude-plugins-official/unused/99.0.0/.claude-plugin/plugin.json", `{"name":"unused","version":"99.0.0"}`)
	writeInventoryFixture(t, home, ".claude/plugins/store/legacy.json", `{"name":"legacy","marketplace":"example"}`)
	first, codes := collectInventoryExtensions(home, "claude-code", "", false)
	if len(codes) != 0 || len(first) != 4 || first[0].Enabled != "false" {
		t.Fatalf("extensions=%+v codes=%v", first, codes)
	}
	writeInventoryFixture(t, home, ".claude/plugins/cache/claude-plugins-official/code-review/1.0.1/skills/example/SKILL.md", "changed content")
	next, codes := collectInventoryExtensions(home, "claude-code", "", false)
	if len(codes) != 0 || next[0].ContentHash == first[0].ContentHash || next[1].ContentHash != first[1].ContentHash {
		t.Fatalf("hashes: %+v %+v %v", first, next, codes)
	}
}

// protects=project installs retain their own version and project reference;
// fails_when=a user install or override replaces another project's plugin;
// no_claim=project/local scope variants are synthetic registry records.
func TestInstalledPluginProjectScopes(t *testing.T) {
	home := t.TempDir()
	project := t.TempDir()
	writeClaudePlugin(t, home, "reviewer", "community", "1.0.0")
	path := filepath.Join(home, ".claude", "plugins", "installed_plugins.json")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var registry map[string]any
	if err := json.Unmarshal(data, &registry); err != nil {
		t.Fatal(err)
	}
	plugins := registry["plugins"].(map[string]any)
	plugins["reviewer@community"] = append(plugins["reviewer@community"].([]any), map[string]any{"scope": "project", "projectPath": project, "installPath": ".claude/plugins/cache/community/reviewer/2.0.0", "version": "2.0.0"})
	data, _ = json.Marshal(registry)
	writeInventoryFixture(t, home, ".claude/plugins/installed_plugins.json", string(data))
	writeInventoryFixture(t, home, ".claude/plugins/cache/community/reviewer/2.0.0/.claude-plugin/plugin.json", `{"version":"2.0.0"}`)
	writeInventoryFixture(t, project, ".claude/settings.json", `{"enabledPlugins":{"reviewer@community":true}}`)
	writeInventoryFixture(t, project, ".claude/settings.local.json", `{"enabledPlugins":{"reviewer@community":false}}`)
	data, _ = json.Marshal(map[string]any{"projects": map[string]any{project: map[string]any{}}})
	writeInventoryFixture(t, home, ".claude.json", string(data))
	exts, codes := collectInventoryExtensions(home, "claude-code", "salt", false)
	if len(codes) != 0 || len(exts) != 2 {
		t.Fatalf("%+v %v", exts, codes)
	}
	for _, ext := range exts {
		if ext.Scope == "project" && (ext.Version != "2.0.0" || ext.Enabled != "false" || ext.ProjectRefHash != "sha256:"+configPathHash("salt", home, project)) {
			t.Fatalf("project=%+v", ext)
		}
	}
	exts, codes = collectInventoryExtensions(home, "claude-code", "salt", true)
	if len(codes) != 0 || len(exts) != 1 || exts[0].Scope != "user" {
		t.Fatalf("profile scan: %+v %v", exts, codes)
	}
}

// protects=Codex selects active cache versions and reads the right manifest;
// fails_when=lexical ordering picks 2.9 over 2.10, a legacy manifest wins, or disabled state is lost;
// no_claim=synthetic versions of the captured Codex layout.
func TestInstalledPluginCodexVersions(t *testing.T) {
	home := t.TempDir()
	writeInventoryFixture(t, home, ".codex/config.toml", "[plugins.\"reviewer@community\"]\nenabled=false\n")
	base := ".codex/plugins/cache/community/reviewer/"
	for _, v := range []string{"2.9.0", "2.10.0", "2.10.0-beta.1"} {
		writeInventoryFixture(t, home, base+v+"/.codex-plugin/plugin.json", `{"version":"`+v+`"}`)
	}
	writeInventoryFixture(t, home, base+"2.10.0/.claude-plugin/plugin.json", `{"version":"0.0.1"}`)
	exts, codes := collectInventoryExtensions(home, "codex-cli", "", false)
	if len(codes) != 0 || len(exts) != 1 || exts[0].Version != "2.10.0" || exts[0].Enabled != "false" {
		t.Fatalf("%+v %v", exts, codes)
	}
	writeInventoryFixture(t, home, base+"local/plugin.json", `{"name":"reviewer","version":"3.0.0"}`)
	writeInventoryFixture(t, home, base+"local/.codex-plugin/plugin.json", `{"version":"0.0.1"}`)
	exts, codes = collectInventoryExtensions(home, "codex-cli", "", false)
	if len(codes) != 0 || len(exts) != 1 || exts[0].Version != "3.0.0" {
		t.Fatalf("local: %+v %v", exts, codes)
	}
}

// protects=malformed enabled maps and marketplace records make coverage partial;
// fails_when=bad settings are silently interpreted as unknown or no plugins;
// no_claim=synthetic corruption of captured agent files.
func TestInstalledPluginAuxiliaryFailures(t *testing.T) {
	for _, tc := range []struct{ agent, path, body string }{
		{"claude-code", ".claude/plugins/known_marketplaces.json", "{"},
		{"claude-code", ".claude/settings.json", `{"enabledPlugins":{"reviewer@community":"yes"}}`},
		{"codex-cli", ".codex/config.toml", "[plugins.\"reviewer@community\"]\nenabled='yes'\n"},
	} {
		t.Run(tc.agent+tc.path, func(t *testing.T) {
			home := t.TempDir()
			writeInventoryFixture(t, home, tc.path, tc.body)
			_, codes := collectInventoryExtensions(home, tc.agent, "", false)
			if !containsExtensionError(codes, "extension_malformed") {
				t.Fatalf("codes=%v", codes)
			}
		})
	}
}

// protects=untrusted registry paths stay within the profile without symlink reads;
// fails_when=outside content is hashed or registry limits look complete;
// no_claim=permission failures have separate tests, no concurrent writer fixture.
func TestInstalledPluginBoundedReads(t *testing.T) {
	for _, mode := range []string{"escape", "symlink", "oversized", "directory"} {
		t.Run(mode, func(t *testing.T) {
			home := t.TempDir()
			installClaudeLayoutFixture(t, home)
			registry := filepath.Join(home, ".claude", "plugins", "installed_plugins.json")
			want := "extension_unsafe_path"
			switch mode {
			case "escape":
				writeInventoryFixture(t, home, ".claude/plugins/installed_plugins.json", `{"version":2,"plugins":{"p@m":[{"scope":"user","installPath":"../outside"}]}}`)
			case "symlink":
				outside := t.TempDir()
				writeInventoryFixture(t, outside, "plugin/.claude-plugin/plugin.json", `{"version":"1.0.0"}`)
				if err := os.Symlink(outside, filepath.Join(home, "linked")); err != nil {
					t.Skipf("symlink unavailable: %v", err)
				}
				writeInventoryFixture(t, home, ".claude/plugins/installed_plugins.json", `{"version":2,"plugins":{"p@m":[{"scope":"user","installPath":"linked/plugin"}]}}`)
				want = "extension_symlink"
			case "oversized":
				writeInventoryFixture(t, home, ".claude/plugins/installed_plugins.json", strings.Repeat(" ", inventoryConfigLimit+1))
				want = "extension_file_limit"
			case "directory":
				if err := os.Remove(registry); err != nil {
					t.Fatal(err)
				}
				if err := os.Mkdir(registry, 0700); err != nil {
					t.Fatal(err)
				}
				want = "extension_unreadable"
			}
			exts, codes := collectInventoryExtensions(home, "claude-code", "", false)
			if !containsExtensionError(codes, want) {
				t.Fatalf("codes=%v want=%s", codes, want)
			}
			for _, ext := range exts {
				if ext.ContentHash != "" || ext.HashStatus != contentHashUnreadable {
					t.Fatalf("unsafe content hashed: %+v", ext)
				}
			}
		})
	}
}

// protects=local project enablement overrides for user-installed plugins;
// fails_when=a local-only override is omitted or project settings beat local settings;
// no_claim=synthetic known projects, no registry project install in this fixture.
func TestInstalledPluginUserProjectEnabled(t *testing.T) {
	for _, projectSetting := range []bool{false, true} {
		name := "local-only"
		if projectSetting {
			name = "local-overrides-project"
		}
		t.Run(name, func(t *testing.T) {
			home, project := t.TempDir(), t.TempDir()
			writeClaudePlugin(t, home, "reviewer", "community", "1.0.0")
			writeInventoryFixture(t, home, ".claude/settings.json", `{"enabledPlugins":{"reviewer@community":true}}`)
			data, err := json.Marshal(map[string]any{"projects": map[string]any{project: map[string]any{}}})
			if err != nil {
				t.Fatal(err)
			}
			writeInventoryFixture(t, home, ".claude.json", string(data))
			if projectSetting {
				writeInventoryFixture(t, project, ".claude/settings.json", `{"enabledPlugins":{"reviewer@community":true}}`)
			}
			writeInventoryFixture(t, project, ".claude/settings.local.json", `{"enabledPlugins":{"reviewer@community":false}}`)
			exts, codes := collectInventoryExtensions(home, "claude-code", "salt", false)
			if len(codes) != 0 || len(exts) != 2 {
				t.Fatalf("extensions=%+v codes=%v", exts, codes)
			}
			for _, ext := range exts {
				if ext.Scope == "project" && (ext.Enabled != "false" || ext.ProjectRefHash != "sha256:"+configPathHash("salt", home, project)) {
					t.Fatalf("project=%+v", ext)
				}
				if ext.Scope == "user" && ext.Enabled != "true" {
					t.Fatalf("user=%+v", ext)
				}
			}
		})
	}
}
