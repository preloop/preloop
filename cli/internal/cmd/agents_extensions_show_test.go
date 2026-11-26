package cmd

import (
	"net/http"
	"path/filepath"
	"strings"
	"testing"
)

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
