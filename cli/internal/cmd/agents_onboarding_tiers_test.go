package cmd

import (
	"bytes"
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

func TestPromptToOnboardCandidatesTiered_AutoApproveOrdersVerifiedFirst(t *testing.T) {
	output := &bytes.Buffer{}
	// Discovery order deliberately lists the MCP-only agent first.
	candidates := []AgentConfig{
		{Name: "Cursor", ConfigPath: "/tmp/mcp.json"},
		{Name: "OpenClaw", ConfigPath: "/tmp/openclaw.json", AuthState: string(agentAuthStateReady)},
	}

	var enrolledOrder []string
	outcomes, err := promptToOnboardCandidatesTiered(
		strings.NewReader(""),
		output,
		nil,
		candidates,
		true,
		false,
		func(agent AgentConfig, _ bool) error {
			enrolledOrder = append(enrolledOrder, agent.Name)
			return nil
		},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(outcomes) != 2 {
		t.Fatalf("expected both agents onboarded under --yes, got %#v", outcomes)
	}
	if len(enrolledOrder) != 2 || enrolledOrder[0] != "OpenClaw" || enrolledOrder[1] != "Cursor" {
		t.Fatalf("expected verified agent first, got %v", enrolledOrder)
	}
	rendered := output.String()
	if !strings.Contains(rendered, "couldn't verify model routing") {
		t.Fatalf("expected tier explanation, got %q", rendered)
	}
	if !strings.Contains(rendered, "Cursor: "+mcpOnlySupportLabel) {
		t.Fatalf("expected per-agent reason, got %q", rendered)
	}
	if !strings.Contains(rendered, "Onboarding them as well") {
		t.Fatalf("expected --yes continuation note, got %q", rendered)
	}
	// The explanation must precede the second-tier enrollment, i.e. appear
	// after the first tier's heading.
	if strings.Index(rendered, "verified model routing first") > strings.Index(rendered, "couldn't verify model routing") {
		t.Fatalf("expected tier-1 heading before tier-2 explanation, got %q", rendered)
	}
}

func TestPromptToOnboardCandidatesTiered_InteractiveTwoStepAsk(t *testing.T) {
	output := &bytes.Buffer{}
	candidates := []AgentConfig{
		{Name: "Cursor", ConfigPath: "/tmp/mcp.json"},
		{Name: "OpenClaw", ConfigPath: "/tmp/openclaw.json", AuthState: string(agentAuthStateReady)},
	}

	// OpenClaw (tier 1): confirm yes + keep default name. Cursor (tier 2):
	// decline after the explanation.
	input := strings.NewReader("y\n\nn\n")
	var enrolledOrder []string
	outcomes, err := promptToOnboardCandidatesTiered(
		input,
		output,
		nil,
		candidates,
		false,
		false,
		func(agent AgentConfig, _ bool) error {
			enrolledOrder = append(enrolledOrder, agent.Name)
			return nil
		},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(enrolledOrder) != 1 || enrolledOrder[0] != "OpenClaw" {
		t.Fatalf("expected only the verified agent enrolled, got %v", enrolledOrder)
	}
	if len(outcomes) != 1 || outcomes[0].Agent.Name != "OpenClaw" {
		t.Fatalf("expected a single OpenClaw outcome, got %#v", outcomes)
	}
	rendered := output.String()
	if !strings.Contains(rendered, "couldn't verify model routing") ||
		!strings.Contains(rendered, "Onboard anyway?") {
		t.Fatalf("expected interactive tier-2 explanation, got %q", rendered)
	}
	if !strings.Contains(rendered, "Onboard Cursor (Cursor) into managed Preloop access now?") {
		t.Fatalf("expected tier-2 per-agent prompt after the explanation, got %q", rendered)
	}
}
