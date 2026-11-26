package cmd

import (
	"encoding/json"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
)

func TestExtensionsShowEscapesTerminalControls(t *testing.T) {
	home := extensionHome(t)
	control := "\x1b]52;c;c3ludGhldGlj\a\u009b2J\r"
	writeInventoryFixture(t, home, ".claude/skills/careful/SKILL.md", "Readable\n\ttext "+control)
	command, _ := json.Marshal("bash example.sh " + control)
	writeInventoryFixture(t, home, ".claude/settings.json", `{"hooks":{"PreToolUse":[{"command":`+string(command)+`}]}}`)
	for _, pair := range [][2]string{{"skill", "careful"}, {"hook", "PreToolUse"}} {
		stdout, _, err := runExtensions(t, "show", pair[0], pair[1], "--agent", "claude-code")
		if err != nil {
			t.Fatal(err)
		}
		for _, unsafe := range []string{"\x1b", "\a", "\u009b", "\r"} {
			if strings.Contains(stdout, unsafe) {
				t.Fatalf("terminal control was not escaped: %q", unsafe)
			}
		}
		if !strings.Contains(stdout, `\u001b]52;c;c3ludGhldGlj\u0007\u009b2J\u000d`) {
			t.Fatalf("escaped content missing: %q", stdout)
		}
	}
}

func TestExtensionsShowLocalOnly(t *testing.T) {
	home := extensionHome(t)
	writeInventoryFixture(t, home, ".claude/skills/careful/SKILL.md", "---\nname: careful\ndescription: Review changes\n---\nPRIVATE SKILL BODY")
	writeInventoryFixture(t, home, ".claude/skills/careful/scripts/check.py", "PRIVATE SCRIPT")
	writeInventoryFixture(t, home, ".claude/settings.json", `{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"command":"bash /private/check.sh --token PRIVATE"}]}]}}`)
	original := http.DefaultTransport
	spy := &inventoryNetworkSpy{}
	http.DefaultTransport = spy
	t.Cleanup(func() { http.DefaultTransport = original })
	for _, tc := range []struct{ kind, id, want, file string }{
		{"skill", "careful", "PRIVATE SKILL BODY", "scripts/check.py"},
		{"hook", "PreToolUse", "bash /private/check.sh --token PRIVATE", "settings.json"},
	} {
		stdout, _, err := runExtensions(t, "show", tc.kind, tc.id, "--agent", "claude-code")
		if err != nil || !strings.HasPrefix(stdout, "Local only: nothing is sent to Preloop.\n") || !strings.Contains(stdout, tc.want) || !strings.Contains(stdout, filepath.FromSlash(tc.file)) {
			t.Fatalf("show result: %q %v", stdout, err)
		}
	}
	if spy.calls != 0 {
		t.Fatalf("show made %d network calls", spy.calls)
	}
}
