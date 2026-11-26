package cmd

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestExtensionDescriptiveMetadata(t *testing.T) {
	home := extensionHome(t)
	installClaudeLayoutFixture(t, home)
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
	home := extensionHome(t)
	writeInventoryFixture(t, home, ".codex/skills/careful/SKILL.md", "---\nname: careful\ndescription: Review changes\n---\nPRIVATE")
	extensions, _ := collectInventoryExtensions(home, "codex-cli", "", false)
	if len(extensions) != 1 || extensions[0].Details == nil || extensions[0].Details.Invocation != "$careful (or /skills); loaded by the model when relevant" {
		t.Fatalf("unexpected invocation: %+v", extensions)
	}
}

func TestExtensionDetailsOptOut(t *testing.T) {
	home := extensionHome(t)
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
