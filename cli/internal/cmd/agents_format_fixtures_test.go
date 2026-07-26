package cmd

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// These tests pin the CLI's recent-model resolvers to verbatim on-disk
// format samples captured from real agent installations (see
// testdata/agent-formats/README.md). The files the resolvers read are not
// stable interfaces of the agents, and when a format changes the resolvers
// fail *silently* — inference returns "", and onboarding degrades to
// MCP-proxy-only with no error (this shipped: OpenCode 1.18 changed its LLM
// log tag and gateway onboarding quietly stopped resolving). Fixtures turn
// that drift into a test failure with a actionable name.

func copyFixture(t *testing.T, fixtureRelPath, destPath string) {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", "agent-formats", fixtureRelPath))
	if err != nil {
		t.Fatalf("failed to read fixture %s: %v", fixtureRelPath, err)
	}
	if err := os.MkdirAll(filepath.Dir(destPath), 0o755); err != nil {
		t.Fatalf("failed to create fixture dest dir: %v", err)
	}
	if err := os.WriteFile(destPath, data, 0o644); err != nil {
		t.Fatalf("failed to write fixture to %s: %v", destPath, err)
	}
}

func TestOpenCodeRecentModelFormatFixtures(t *testing.T) {
	cases := []struct {
		name    string
		fixture string
		want    string
	}{
		{
			// service=llm tag; last matching line wins (reverse scan).
			name:    "legacy 1.17 service=llm",
			fixture: "opencode/log@1.17.txt",
			want:    "zai/glm-5-turbo",
		},
		{
			// message=stream tag introduced in 1.18.
			name:    "1.18.5 message=stream",
			fixture: "opencode/log@1.18.5.txt",
			want:    "moonshotai/kimi-k3",
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			home := t.TempDir()
			testenv.SetHome(t, home)
			copyFixture(
				t,
				testCase.fixture,
				filepath.Join(home, ".local", "share", "opencode", "log", "opencode.log"),
			)
			if got := resolveOpenCodeRecentModelRef(); got != testCase.want {
				t.Fatalf(
					"resolveOpenCodeRecentModelRef() = %q, want %q — OpenCode log format drift? See testdata/agent-formats/README.md",
					got,
					testCase.want,
				)
			}
		})
	}
}

func TestClaudeRecentModelFormatFixture(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	copyFixture(
		t,
		"claude-code/session@2.1.jsonl",
		filepath.Join(home, ".claude", "projects", "-tmp-w", "session.jsonl"),
	)
	if got := resolveClaudeRecentModelRef(); got != "claude-opus-4-6" {
		t.Fatalf(
			"resolveClaudeRecentModelRef() = %q, want %q — Claude Code session format drift? See testdata/agent-formats/README.md",
			got,
			"claude-opus-4-6",
		)
	}
}

func TestCodexRecentModelFormatFixture(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	codexHome := filepath.Join(home, ".codex")
	t.Setenv("CODEX_HOME", codexHome)
	copyFixture(
		t,
		"codex-cli/session@0.46.jsonl",
		filepath.Join(codexHome, "sessions", "2026", "07", "15", "rollout-2026-07-15T23-20-01-x.jsonl"),
	)
	// Codex rollouts store bare model IDs; provider resolution happens in
	// the caller (config model_provider / ChatGPT auth mode → openai).
	if got := resolveCodexRecentModelRef(); got != "gpt-5.6-sol" {
		t.Fatalf(
			"resolveCodexRecentModelRef() = %q, want %q — Codex session format drift? See testdata/agent-formats/README.md",
			got,
			"gpt-5.6-sol",
		)
	}
}

func TestGeminiRecentModelFormatFixture(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	copyFixture(
		t,
		"gemini-cli/chat@0.21.json",
		filepath.Join(home, ".gemini", "tmp", "deadbeef", "chats", "session-1.json"),
	)
	if got := resolveGeminiRecentModelRef(); got != "gemini-3-pro-preview" {
		t.Fatalf(
			"resolveGeminiRecentModelRef() = %q, want %q — Gemini chat format drift? See testdata/agent-formats/README.md",
			got,
			"gemini-3-pro-preview",
		)
	}
}
