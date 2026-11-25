package cmd

import (
	"bytes"

	"encoding/json"
	"github.com/spf13/cobra"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func projectSkillFixture(t *testing.T) (string, string) {
	t.Helper()
	home, project := extensionHome(t), t.TempDir()
	writeClaudePlugin(t, home, "reviewer", "community", "1.0.0")
	registry, err := json.Marshal(map[string]any{"version": 2, "plugins": map[string]any{"reviewer@community": []any{map[string]any{"scope": "project", "projectPath": project, "installPath": ".claude/plugins/marketplaces/community/plugins/reviewer", "version": "1.0.0"}}}})
	if err != nil {
		t.Fatal(err)
	}
	writeInventoryFixture(t, home, ".claude/plugins/installed_plugins.json", string(registry))
	writeInventoryFixture(t, project, ".claude/skills/project-policy/SKILL.md", "---\nname: project-policy\ndescription: Synthetic project policy\n---\nUse the project policy.\n")
	writeInventoryFixture(t, project, ".claude/settings.json", `{"enabledPlugins":{"reviewer@community":true}}`)
	return home, project
}

// protects=unregistered project skills cannot disappear behind complete coverage;
// fails_when=the install registry proves a project exists but only its plugin is
// listed, or local and reported extension coverage disagree;
// no_claim=synthetic copy of the observed layout, no runtime loading assertion.
func TestProjectSkillScopeCoverage(t *testing.T) {
	for _, registered := range []bool{false, true} {
		name := "unregistered"
		if registered {
			name = "registered"
		}
		t.Run(name, func(t *testing.T) {
			home, project := projectSkillFixture(t)
			if registered {
				projects, err := json.Marshal(map[string]any{"projects": map[string]any{project: map[string]any{}}})
				if err != nil {
					t.Fatal(err)
				}
				writeInventoryFixture(t, home, ".claude.json", string(projects))
			}
			stdout, _, err := runExtensions(t, "--agent", "claude-code", "--json")
			var listing extensionsDocument
			if decodeErr := json.Unmarshal([]byte(stdout), &listing); decodeErr != nil {
				t.Fatal(decodeErr)
			}
			want := "partial"
			if registered {
				want = "complete"
			}
			if listing.Completeness != want {
				t.Fatalf("listing=%s err=%v", stdout, err)
			}
			skillFound := false
			for _, app := range listing.Apps {
				for _, ext := range app.Extensions {
					if ext.Kind == "skill" && ext.ID == "project-policy" {
						skillFound = true
					}
				}
			}
			if skillFound != registered {
				t.Fatalf("skill found=%v registered=%v", skillFound, registered)
			}
			stdout, _, err = runExtensions(t, "--agent", "claude-code")
			if !registered && (err == nil || strings.Contains(stdout, "Scan complete.") || !strings.Contains(stdout, "--project .")) {
				t.Fatalf("missing scope warning: %s err=%v", stdout, err)
			}
			report := buildDiscoveryReport("synthetic-salt", "synthetic-device", home, []AgentConfig{{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}})
			if err := addDiscoveryExtensions(&report, "synthetic-salt", home, inventoryTestDependencies(t, home)); err != nil {
				t.Fatal(err)
			}
			if report.Evidence.ExtensionCompleteness != listing.Completeness {
				t.Fatalf("local=%s report=%s", listing.Completeness, report.Evidence.ExtensionCompleteness)
			}
		})
	}
}

// protects=unsupported discovery candidates do not distort supported extension
// coverage; fails_when=an unrelated absent app makes the console disagree with
// the local list; no_claim=only Claude Code and Codex extension locations.
func TestExtensionCoverageIgnoresUnsupportedCandidates(t *testing.T) {
	home := extensionHome(t)
	writeInventoryFixture(t, home, ".claude/skills/project-policy/SKILL.md", "Synthetic skill")
	report := buildDiscoveryReport("salt", "device", home, []AgentConfig{{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}, {Name: "Cursor", ConfigPath: filepath.Join(home, ".cursor", "mcp.json")}})
	if err := addDiscoveryExtensions(&report, "salt", home, inventoryTestDependencies(t, home)); err != nil {
		t.Fatal(err)
	}
	if report.Evidence.ExtensionCompleteness != "complete" {
		t.Fatalf("coverage=%s", report.Evidence.ExtensionCompleteness)
	}
}

// protects=explicit project scope reaches local JSON and the reported payload;
// fails_when=--project is ignored, duplicate roots duplicate rows, or paths leak;
// no_claim=mock HTTP transport and synthetic files, no hosted console assertion.
func TestExplicitProjectScope(t *testing.T) {
	claudeSpec := specByName(t, "Claude Code")
	setupDiscoveryHome(t)
	agentSpecs = []agentSpec{claudeSpec}
	claudeKeychainPresenceProbe = func() bool { return false }
	home, project := projectSkillFixture(t)
	writeInventoryFixture(t, home, ".claude/settings.json", `{}`)
	t.Chdir(project)
	before := inventorySnapshot(t, home)
	stdout, _, err := runExtensions(t, "--agent", "claude-code", "--project", ".", "--project", project, "--json")
	if err != nil {
		t.Fatalf("listing=%s err=%v", stdout, err)
	}
	var listing extensionsDocument
	if err := json.Unmarshal([]byte(stdout), &listing); err != nil {
		t.Fatal(err)
	}
	if listing.Completeness != "complete" || len(listing.Apps) != 1 || len(listing.Apps[0].Extensions) != 2 {
		t.Fatalf("listing=%s", stdout)
	}
	found := false
	skillHash := ""
	for _, ext := range listing.Apps[0].Extensions {
		if ext.Kind == "skill" && ext.ID == "project-policy" && ext.Scope == "project" && ext.HashStatus == contentHashOK {
			skillHash = ext.ContentHash
			found = true
		}
	}
	if !found {
		t.Fatalf("missing project skill: %s", stdout)
	}
	server, rec := newDiscoveryServer(t)
	useDiscoveryServer(t, server.URL)
	old := inventoryProbes
	inventoryProbes = inventoryTestDependencies(t, home)
	t.Cleanup(func() { inventoryProbes = old })
	cmd := newDiscoverTestCmd(t, "--json", "--no-onboard-prompt", "--report", "--project", ".")
	captureStdout(t, func() error { return runAgentsDiscover(cmd, nil) })
	var report discoveryReportRequest
	if err := json.Unmarshal(rec.report, &report); err != nil {
		t.Fatal(err)
	}
	if report.Evidence.ExtensionCompleteness != listing.Completeness {
		t.Fatalf("local=%s report=%s", listing.Completeness, report.Evidence.ExtensionCompleteness)
	}
	found = false
	for _, app := range report.Evidence.Applications {
		if app.AgentKind == "claude_code" && app.Extensions != nil {
			for _, ext := range *app.Extensions {
				if ext.Kind == "skill" && ext.ContentHash == skillHash && ext.Scope == "project" && ext.ProjectRefHash == "sha256:"+configPathHash(testDiscoverySalt, home, project) {
					found = true
				}
			}
		}
	}
	if !found {
		t.Fatalf("project skill missing from report: %s", rec.report)
	}
	for _, private := range []string{home, project, "Use the project policy."} {
		if strings.Contains(string(rec.report), private) {
			t.Fatalf("private value escaped: %q", private)
		}
	}
	if !reflect.DeepEqual(before, inventorySnapshot(t, home)) {
		t.Fatal("project selection modified configuration")
	}
}

// protects=an unregistered current project without any installed plugin is
// identified as omitted; fails_when=the command says complete without walking
// the skill root; no_claim=no search of unknown projects outside the cwd.
func TestCurrentProjectWithoutRegistry(t *testing.T) {
	home := extensionHome(t)
	project := t.TempDir()
	writeInventoryFixture(t, project, ".claude/skills/project-policy/SKILL.md", "Synthetic policy")
	t.Chdir(project)
	extensions, codes := collectInventoryExtensions(home, "claude-code", "salt", false)
	if len(extensions) != 0 || !containsExtensionError(codes, "extension_project_unscanned") {
		t.Fatalf("extensions=%v codes=%v", extensions, codes)
	}
	_, codes = collectInventoryExtensions(home, "claude-code", "salt", true)
	if len(codes) != 0 {
		t.Fatalf("all-users examined caller project: %v", codes)
	}
}

// protects=invalid or missing explicit roots cannot claim successful coverage;
// fails_when=the new option silently broadens all-users or ignores missing paths;
// no_claim=synthetic argument and filesystem failures.
func TestExplicitProjectInvalidScope(t *testing.T) {
	extensionHome(t)
	for _, args := range [][]string{{"--project", ""}, {"--all-users", "--project", "."}, {"--project", filepath.Join(t.TempDir(), "missing")}} {
		stdout, _, err := runExtensions(t, args...)
		if err == nil || strings.Contains(stdout, "Scan complete.") {
			t.Fatalf("args=%v stdout=%s err=%v", args, stdout, err)
		}
	}
	cmd := newDiscoverTestCmd(t, "--project", ".")
	if err := runAgentsDiscover(cmd, nil); err == nil || !strings.Contains(err.Error(), "--inventory or --report") {
		t.Fatalf("err=%v", err)
	}
}

// protects=offline inventory uses exactly the explicitly selected project roots;
// fails_when=the --inventory path ignores --project or changes local state;
// no_claim=synthetic home and no network or runtime probes.
func TestExplicitProjectOfflineInventory(t *testing.T) {
	home, project := projectSkillFixture(t)
	old := inventoryProbes
	inventoryProbes = inventoryTestDependencies(t, home)
	t.Cleanup(func() { inventoryProbes = old })
	cmd := &cobra.Command{Use: "discover"}
	cmd.Flags().Bool("all-users", false, "")
	cmd.Flags().StringArray("project", nil, "")
	if err := cmd.Flags().Set("project", project); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	cmd.SetOut(&out)
	t.Setenv(discoveryReportEnv, "")
	if err := runAgentsInventory(cmd); err != nil {
		t.Fatal(err)
	}
	var inventory inventoryEnvelope
	if err := json.Unmarshal(out.Bytes(), &inventory); err != nil {
		t.Fatal(err)
	}
	found := false
	for _, app := range inventory.Apps {
		if app.AppID == "claude-code" {
			if app.Completeness != "complete" || len(app.Extensions) != 2 {
				t.Fatalf("app=%+v", app)
			}
			found = true
		}
	}
	if !found {
		t.Fatal("Claude inventory absent")
	}
}

// protects=collected extensions omitted from report candidates remain partial;
// fails_when=decoupling unrelated probes overclaims coverage after dropping rows;
// no_claim=synthetic discovery/probe mismatch, no agent runtime assertion.
func TestExtensionCoverageUnreportedSupportedApp(t *testing.T) {
	home := extensionHome(t)
	writeInventoryFixture(t, home, ".claude/skills/project-policy/SKILL.md", "Synthetic policy")
	report := buildDiscoveryReport("salt", "device", home, []AgentConfig{{Name: "Cursor", ConfigPath: filepath.Join(home, ".cursor", "mcp.json")}})
	if err := addDiscoveryExtensions(&report, "salt", home, inventoryTestDependencies(t, home)); err != nil {
		t.Fatal(err)
	}
	if report.Evidence.ExtensionCompleteness != "partial" {
		t.Fatal("unreported extension rows claimed complete coverage")
	}
}

// protects=the already-scanned user root is not misclassified as an omitted
// project when invoked from home; fails_when=home listing exits 2 or reporting
// becomes partial; no_claim=synthetic profile, not arbitrary ancestor projects.
func TestHomeDirectoryIsNotUnscannedProject(t *testing.T) {
	for _, explicit := range []bool{false, true} {
		name := "implicit"
		if explicit {
			name = "explicit"
		}
		t.Run(name, func(t *testing.T) {
			home := extensionHome(t)
			writeInventoryFixture(t, home, ".claude/skills/user-policy/SKILL.md", "Synthetic user skill")
			t.Chdir(home)
			args := []string{"--agent", "claude-code", "--json"}
			if explicit {
				args = append(args, "--project", ".")
			}
			stdout, _, err := runExtensions(t, args...)
			if err != nil {
				t.Fatalf("home listing failed: %v; %s", err, stdout)
			}
			var listing extensionsDocument
			if err := json.Unmarshal([]byte(stdout), &listing); err != nil {
				t.Fatal(err)
			}
			if listing.Completeness != "complete" || len(listing.Apps) != 1 || len(listing.Apps[0].Extensions) != 1 || listing.Apps[0].Extensions[0].Scope != "user" {
				t.Fatalf("listing=%s", stdout)
			}
			report := buildDiscoveryReport("salt", "device", home, []AgentConfig{{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}})
			deps := inventoryTestDependencies(t, home)
			if explicit {
				deps.ExtensionProjects = []string{home}
			}
			if err := addDiscoveryExtensions(&report, "salt", home, deps); err != nil {
				t.Fatal(err)
			}
			if report.Evidence.ExtensionCompleteness != listing.Completeness || len(*report.Candidates[0].Extensions) != 1 || (*report.Candidates[0].Extensions)[0].Scope != "user" {
				t.Fatalf("home report=%+v", report.Evidence)
			}
		})
	}
}
