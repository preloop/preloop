package cmd

import (
	"encoding/json"
	"fmt"
	"github.com/preloop/preloop/cli/internal/testenv"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// A relative fixture home keeps platform drive letters and temporary directory
// names out of metadata tests. Separate privacy tests cover those segments.
func extensionMetadataHome(t *testing.T) string {
	t.Helper()
	t.Chdir(t.TempDir())
	return testenv.SetHome(t, "fixture-profile")
}

func TestExtensionDescriptiveMetadata(t *testing.T) {
	home := extensionMetadataHome(t)
	writeInventoryFixture(t, home, ".claude/skills/careful/SKILL.md", "---\nname: careful\ndescription: |\n  Review changes\n  before acting.\n---\nPRIVATE BODY\n")
	writeInventoryFixture(t, home, ".claude/skills/careful/scripts/check.py", "PRIVATE SCRIPT")
	writeInventoryFixture(t, home, ".claude/commands/review.md", "---\ndescription: Review a change\n---\nPRIVATE COMMAND")
	writeInventoryFixture(t, home, ".claude/settings.json", `{"hooks":{"PreToolUse":[{"matcher":"Bash|Edit","hooks":[{"type":"command","command":"/opt/bin/preloop agents permission-hook --token PRIVATE"},{"type":"command","command":"python3 /private/check.py --secret PRIVATE"}]}]}}`)
	extensions, _ := collectInventoryExtensions(home, "claude-code", "salt", false)
	raw, _ := json.Marshal(extensions)
	for _, secret := range []string{"PRIVATE", "/opt/bin", "/private/check.py"} {
		if strings.Contains(string(raw), secret) {
			t.Fatalf("private value escaped: %s", secret)
		}
	}
	found := map[string]bool{}
	for _, ext := range extensions {
		if ext.ID == "careful" {
			found["skill"] = true
			if ext.Details == nil || ext.Details.Description != "Review changes before acting." || ext.Details.Invocation != "/careful; loaded by the model when relevant" || ext.Details.ScriptCount == nil || *ext.Details.ScriptCount != 1 {
				t.Fatalf("skill: %+v", ext.Details)
			}
		}
		if ext.ID == "review" {
			found["command"] = true
			if ext.Details == nil || ext.Details.Invocation != "/review" {
				t.Fatalf("command: %+v", ext.Details)
			}
		}
		if ext.Kind == "hook" {
			found["hook"] = true
			groups := ext.Details.HookMatchers
			if len(groups) != 1 || groups[0].Matcher != "Bash|Edit" || groups[0].Count != 2 || groups[0].Commands[0].Executable != "preloop" || !groups[0].Commands[0].PreloopManaged || groups[0].Commands[1].Executable != "python3" {
				t.Fatalf("hooks: %+v", groups)
			}
		}
	}
	if len(found) != 3 {
		t.Fatalf("missing metadata: %v", found)
	}
}

func TestExtensionDescriptionPrivacy(t *testing.T) {
	scan := &extensionScan{home: "/Users/jane"}
	for _, value := range []string{"Ask Jane before running", "Uses /Users/shared/file", "Read https://example.com/docs", "Read /opt/project/file"} {
		if clean, withheld := scan.detailText(value); clean != "" || !withheld {
			t.Fatalf("unsafe text accepted: %q", value)
		}
	}
	clean, withheld := scan.detailText("hello\x00  world\n" + strings.Repeat("界", 400))
	if withheld || len([]rune(clean)) != 300 || !strings.HasPrefix(clean, "hello world ") {
		t.Fatalf("normalization: %q %v", clean, withheld)
	}
}

func TestCodexSkillInvocation(t *testing.T) {
	home := extensionMetadataHome(t)
	writeInventoryFixture(t, home, ".codex/skills/careful/SKILL.md", "---\nname: careful\ndescription: Review changes\n---\nPRIVATE")
	extensions, _ := collectInventoryExtensions(home, "codex-cli", "", false)
	if len(extensions) != 1 || extensions[0].Details == nil || extensions[0].Details.Invocation != "$careful (or /skills); loaded by the model when relevant" {
		t.Fatalf("unexpected invocation: %+v", extensions)
	}
}

func TestExtensionDetailsOptOut(t *testing.T) {
	home := extensionMetadataHome(t)
	writeInventoryFixture(t, home, ".claude/skills/careful/SKILL.md", "---\nname: careful\ndescription: Review changes\n---\nPRIVATE")
	deps := inventoryTestDependencies(t, home)
	deps.NoExtensionDetails = true
	scan := collectAgentInventory(deps)
	for _, app := range scan.Apps {
		for _, ext := range app.Extensions {
			if ext.Details != nil {
				t.Fatal("details survived opt-out")
			}
		}
	}
}

func TestPluginDescriptionsAndContents(t *testing.T) {
	home := extensionMetadataHome(t)
	writeClaudePlugin(t, home, "reviewer", "community", "1.0.0")
	base := ".claude/plugins/marketplaces/community/plugins/reviewer/"
	writeInventoryFixture(t, home, base+".claude-plugin/plugin.json", `{"name":"reviewer","description":"Review helpers","author":{"name":"Jane Doe","email":"private@example.com"},"skills":["./extra-skills"],"commands":"./extra-commands"}`)
	for _, dir := range []string{"skills/basic", "extra-skills/careful"} {
		writeInventoryFixture(t, home, base+dir+"/SKILL.md", "---\nname: careful\ndescription: Review changes\n---\nPRIVATE")
	}
	writeInventoryFixture(t, home, base+"extra-commands/check.md", "---\ndescription: Check a change\n---\nPRIVATE")
	writeInventoryFixture(t, home, base+"agents/reviewer.md", "PRIVATE")
	writeInventoryFixture(t, home, base+"hooks/hooks.json", `{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"bash /private/hook.sh"}]}]}}`)
	writeInventoryFixture(t, home, base+".mcp.json", `{"mcpServers":{"first":{"url":"https://example.com"},"second":{"command":"node","args":["PRIVATE"]}}}`)
	exts, codes := collectInventoryExtensions(home, "claude-code", "", false)
	if len(codes) > 0 {
		t.Fatalf("errors: %v", codes)
	}
	var plugin inventoryExtension
	for _, ext := range exts {
		if ext.Kind == "plugin" {
			plugin = ext
		}
	}
	d := plugin.Details
	if d == nil || d.Description != "Review helpers" || d.Author != "Jane Doe" || *d.Bundles != (extensionBundleCounts{Skills: 2, Commands: 1, Hooks: 1, MCPServers: 2, Agents: 1}) {
		t.Fatalf("plugin: %+v", d)
	}
	raw, _ := json.Marshal(exts)
	for _, secret := range []string{"private@example.com", "https://example.com", "PRIVATE", "/private/hook.sh"} {
		if strings.Contains(string(raw), secret) {
			t.Fatal("private plugin data escaped")
		}
	}
}

func TestSkillInvocationControlsAndModernCodexRoots(t *testing.T) {
	home := extensionMetadataHome(t)
	writeInventoryFixture(t, home, ".claude/skills/background/SKILL.md", "---\nname: background\ndescription: Check a change\nuser-invocable: false\n---\nPRIVATE")
	writeInventoryFixture(t, home, ".claude/skills/manual/SKILL.md", "---\nname: manual\ndescription: Check a change\ndisable-model-invocation: true\n---\nPRIVATE")
	exts, _ := collectInventoryExtensions(home, "claude-code", "", false)
	for _, ext := range exts {
		if ext.ID == "background" && ext.Details.Invocation != "loaded by the model when relevant" {
			t.Fatalf("background: %+v", ext.Details)
		}
		if ext.ID == "manual" && ext.Details.Invocation != "/manual" {
			t.Fatalf("manual: %+v", ext.Details)
		}
	}
	project := t.TempDir()
	writeInventoryFixture(t, home, ".agents/skills/careful/SKILL.md", "---\nname: careful\ndescription: Review changes\n---\nPRIVATE")
	writeInventoryFixture(t, home, ".agents/skills/careful/agents/openai.yaml", "policy:\n  allow_implicit_invocation: false\n")
	writeInventoryFixture(t, project, ".agents/skills/project-check/SKILL.md", "---\nname: project-check\ndescription: Review changes\n---\nPRIVATE")
	exts, _ = collectInventoryExtensions(home, "codex-cli", "salt", false, project)
	if len(exts) != 2 {
		t.Fatalf("extensions: %+v", exts)
	}
	for _, ext := range exts {
		if ext.ID == "careful" && ext.Details.Invocation != "$careful (or /skills)" {
			t.Fatalf("explicit Codex: %+v", ext.Details)
		}
		if ext.ID == "project-check" && (ext.Scope != "project" || ext.ProjectRefHash == "") {
			t.Fatal("missing project scope")
		}
	}
}

func TestHookExecutableClassification(t *testing.T) {
	scan := extensionScan{home: "/Users/jane"}
	for _, tc := range []struct {
		command, executable string
		managed             bool
	}{
		{`"C:\Program Files\Python\python3.exe" -c "PRIVATE"`, "python3.exe", false},
		{`'/opt/Example Tools/preloop' agents permission-hook --source claude_code`, "preloop", true},
		{`preloop agents permission-hook-other --token PRIVATE`, "preloop", false},
		{`TOKEN=PRIVATE bash -c 'echo PRIVATE'`, "unknown", false},
		{`bash -c 'preloop agents permission-hook'`, "bash", false},
	} {
		got := scan.hookCommand(tc.command)
		if got.Executable != tc.executable || got.PreloopManaged != tc.managed {
			t.Fatalf("classification: %+v", got)
		}
	}
}

func TestExtensionDetailsBoundEmptyDirectoryWalk(t *testing.T) {
	home := extensionMetadataHome(t)
	scan := &extensionScan{home: home, errors: map[string]bool{}}
	for parent := 0; parent < 16; parent++ {
		for child := 0; child < 160; child++ {
			if err := os.MkdirAll(filepath.Join(home, fmt.Sprintf("group-%02d/empty-%03d", parent, child)), 0700); err != nil {
				t.Fatal(err)
			}
		}
	}
	root := scan.open(home)
	defer root.root.Close()
	root.fileList(".")
	if !scan.errors["extension_limit"] {
		t.Fatal("empty directory traversal was not bounded")
	}
}
