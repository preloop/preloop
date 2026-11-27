package cmd

import (
	"path/filepath"
	"strings"
	"testing"
)

func TestAgentModelRoutingVerified(t *testing.T) {
	t.Run("mcp-only agent type is never verified", func(t *testing.T) {
		ok, reason := agentModelRoutingVerified(nil, AgentConfig{
			Name:       "Cursor",
			ConfigPath: "/tmp/mcp.json",
		})
		if ok {
			t.Fatal("expected mcp-only agent to be unverified")
		}
		if reason != mcpOnlySupportLabel {
			t.Fatalf("expected shared mcp-only label, got %q", reason)
		}
	})

	t.Run("openclaw uses the auth-state probe", func(t *testing.T) {
		ready, _ := agentModelRoutingVerified(nil, AgentConfig{
			Name:       "OpenClaw",
			ConfigPath: "/tmp/openclaw.json",
			AuthState:  string(agentAuthStateReady),
		})
		if !ready {
			t.Fatal("expected auth-ready OpenClaw to be verified")
		}
		notReady, reason := agentModelRoutingVerified(nil, AgentConfig{
			Name:       "OpenClaw",
			ConfigPath: "/tmp/openclaw.json",
			AuthState:  string(agentAuthStateNotLoggedIn),
		})
		if notReady {
			t.Fatal("expected OpenClaw without provider auth to be unverified")
		}
		if !strings.Contains(reason, "no provider credentials") {
			t.Fatalf("expected credential reason, got %q", reason)
		}
	})

	t.Run("gateway-capable agent without a resolvable credential is unverified", func(t *testing.T) {
		home := t.TempDir()
		t.Setenv("HOME", home)
		t.Setenv("CODEX_HOME", filepath.Join(home, ".codex"))
		ok, reason := agentModelRoutingVerified(nil, AgentConfig{
			Name:       "Codex CLI",
			ConfigPath: filepath.Join(home, ".codex", "config.toml"),
		})
		if ok {
			t.Fatal("expected Codex CLI without config/credentials to be unverified")
		}
		if !strings.Contains(reason, "model routing supported") {
			t.Fatalf("expected model-routing reason, got %q", reason)
		}
	})
}

func TestPartitionCandidatesByModelRouting(t *testing.T) {
	candidates := []AgentConfig{
		{Name: "Cursor", ConfigPath: "/tmp/mcp.json"},
		{Name: "OpenClaw", ConfigPath: "/tmp/openclaw.json", AuthState: string(agentAuthStateReady)},
	}
	verified, unverified, reasons := partitionCandidatesByModelRouting(nil, candidates)
	if len(verified) != 1 || verified[0].Name != "OpenClaw" {
		t.Fatalf("expected OpenClaw in the verified tier, got %#v", verified)
	}
	if len(unverified) != 1 || unverified[0].Name != "Cursor" {
		t.Fatalf("expected Cursor in the unverified tier, got %#v", unverified)
	}
	if len(reasons) != 1 || reasons[0] != mcpOnlySupportLabel {
		t.Fatalf("expected parallel mcp-only reason, got %#v", reasons)
	}
}
