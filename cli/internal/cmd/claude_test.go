package cmd

import (
	"strings"
	"testing"
)

func TestSupportsAgentControlChannelIncludesClaudeCode(t *testing.T) {
	if !supportsAgentControlChannel(AgentConfig{Name: "Claude Code"}) {
		t.Fatal("Claude Code must support the Agent Control channel")
	}
	if supportsAgentControlChannel(AgentConfig{Name: "Cursor"}) {
		t.Fatal("Cursor is not an Agent Control runtime")
	}
}

func TestClaudePluginInstallMetadata(t *testing.T) {
	agent := AgentConfig{Name: "Claude Code"}
	if got := agentControlPluginSourceDirName(agent); got != "claude-preloop" {
		t.Fatalf("source dir: %q", got)
	}
	if got := agentControlPluginPackageName(agent); got != "@preloop-ai/claude-plugin" {
		t.Fatalf("package: %q", got)
	}
	if got := agentControlPluginInstallerCommand(agent); got != "npm" {
		t.Fatalf("installer: %q", got)
	}
	if got := agentControlPluginVerifyCommand(agent); got != "preloop-claude-plugin" {
		t.Fatalf("verify: %q", got)
	}
}

func TestPrintClaudePairingHintIncludesConsolePath(t *testing.T) {
	var buf strings.Builder
	printClaudePairingHint(&buf)
	if !strings.Contains(buf.String(), "/console/agents") {
		t.Fatalf("expected pairing URL, got %q", buf.String())
	}
}

func TestClaudeIPCRoundTrip(t *testing.T) {
	msg := claudeIPCMessage{Type: "switch", SessionID: "abc"}
	if msg.Type != "switch" || msg.SessionID != "abc" {
		t.Fatalf("unexpected %+v", msg)
	}
}

func TestXmlEscapeAttr(t *testing.T) {
	got := xmlEscapeAttr(`/tmp/Preloop & Co/preloop`)
	if !strings.Contains(got, "&amp;") {
		t.Fatalf("expected XML escape, got %q", got)
	}
	if strings.Contains(got, " & ") {
		t.Fatalf("raw ampersand survived: %q", got)
	}
}
