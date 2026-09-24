package cmd

import (
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestParseCopilotCLIServerMapMCPServers(t *testing.T) {
	servers := parseCopilotCLIServerMap(map[string]interface{}{
		"mcpServers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"type": "http",
				"url":  "https://preloop.example/mcp/v1",
				"headers": map[string]interface{}{
					"Authorization": "Bearer durable-token",
				},
			},
		},
	})
	preloop, ok := servers["preloop"]
	if !ok {
		t.Fatalf("expected preloop server, got %#v", servers)
	}
	if preloop.URL != "https://preloop.example/mcp/v1" {
		t.Fatalf("unexpected URL: %+v", preloop)
	}
	if preloop.Transport != "http" {
		t.Fatalf("expected type=http to populate Transport, got %+v", preloop)
	}
	if preloop.Headers["Authorization"] != "Bearer durable-token" {
		t.Fatalf("unexpected headers: %+v", preloop)
	}
}

func TestParseCopilotCLIServerMapBareTopLevel(t *testing.T) {
	servers := parseCopilotCLIServerMap(map[string]interface{}{
		"filesystem": map[string]interface{}{
			"command": "npx",
			"args":    []interface{}{"-y", "@modelcontextprotocol/server-filesystem"},
		},
		"preloop": map[string]interface{}{
			"type": "http",
			"url":  "https://preloop.example/mcp/v1",
			"headers": map[string]interface{}{
				"Authorization": "Bearer durable-token",
			},
		},
	})
	if len(servers) != 2 {
		t.Fatalf("expected bare map to yield 2 servers, got %#v", servers)
	}
	if servers["preloop"].URL != "https://preloop.example/mcp/v1" {
		t.Fatalf("unexpected preloop entry: %+v", servers["preloop"])
	}
	if servers["filesystem"].Command != "npx" {
		t.Fatalf("unexpected filesystem entry: %+v", servers["filesystem"])
	}
}

func TestParseCopilotCLIServerMapRejectsVSCodeServersOnly(t *testing.T) {
	servers := parseCopilotCLIServerMap(map[string]interface{}{
		"servers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"type": "http",
				"url":  "https://preloop.example/mcp/v1",
			},
		},
	})
	if len(servers) != 0 {
		t.Fatalf("expected VS Code servers-only document to be ignored, got %#v", servers)
	}
}

func TestDiscoverAgentsFindsCopilotCLIConfig(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configDir := filepath.Join(home, ".copilot")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatalf("failed to create .copilot dir: %v", err)
	}
	configPath := filepath.Join(configDir, "mcp-config.json")
	body := `{
  "mcpServers": {
    "demo": {
      "type": "http",
      "url": "https://example.com/mcp"
    }
  }
}`
	if err := os.WriteFile(configPath, []byte(body), 0o644); err != nil {
		t.Fatalf("failed to write Copilot MCP config: %v", err)
	}

	discovered, err := discoverAgents(io.Discard, false)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for _, agent := range discovered {
		if agent.Name != copilotCLIAgentName {
			continue
		}
		if agent.ConfigPath != configPath {
			t.Fatalf("expected config path %q, got %q", configPath, agent.ConfigPath)
		}
		if _, ok := agent.MCPServers["demo"]; !ok {
			t.Fatalf("expected demo MCP server, got %#v", agent.MCPServers)
		}
		if supportsManagedGateway(agent) {
			t.Fatal("Copilot CLI must not support managed gateway rewriting")
		}
		if level := supportLevelForAgent(agent); level != agentSupportLevelMCPOnly {
			t.Fatalf("expected mcp-only support, got %q", level)
		}
		note := mcpOnlyAgentModelNote(agent)
		if !strings.Contains(note, mcpOnlySupportLabel) {
			t.Fatalf("expected generic MCP-only note, got %q", note)
		}
		return
	}
	t.Fatalf("expected Copilot CLI to be discovered, got %#v", discovered)
}

func TestDiscoverAgentsFindsInstalledCopilotCLIWithoutConfig(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	marker := filepath.Join(home, ".copilot")
	if err := os.MkdirAll(marker, 0o755); err != nil {
		t.Fatalf("failed to create .copilot marker: %v", err)
	}

	discovered, err := discoverAgents(io.Discard, false)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for _, agent := range discovered {
		if agent.Name != copilotCLIAgentName {
			continue
		}
		wantPath := filepath.Join(home, ".copilot", "mcp-config.json")
		if agent.ConfigPath != wantPath {
			t.Fatalf("expected synthesized config path %q, got %q", wantPath, agent.ConfigPath)
		}
		if len(agent.MCPServers) != 0 {
			t.Fatalf("expected empty MCP server set, got %+v", agent.MCPServers)
		}
		return
	}
	t.Fatalf("expected Copilot CLI from .copilot install marker, got %#v", discovered)
}

func TestManagedServerSchemaCopilotCLI(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: copilotCLIAgentName})
	entry := adapter.BuildManagedServer("https://preloop.example", "durable-token")
	if entry["type"] != "http" {
		t.Fatalf("expected type=http for Copilot CLI, got %#v", entry)
	}
	if entry["url"] != "https://preloop.example/mcp/v1" {
		t.Fatalf("expected url key, got %#v", entry)
	}
	if _, hasTransport := entry["transport"]; hasTransport {
		t.Fatalf("Copilot CLI entry must not carry transport, got %#v", entry)
	}
	headers, _ := entry["headers"].(map[string]interface{})
	if headers["Authorization"] != "Bearer durable-token" {
		t.Fatalf("expected literal bearer header, got %#v", headers)
	}

	result := adapter.ValidateManagedConfig(map[string]interface{}{
		"mcpServers": map[string]interface{}{"preloop": entry},
	}, "https://preloop.example")
	if result["validation_passed"] != true {
		t.Fatalf("expected Copilot CLI validation to pass, got %+v", result)
	}

	// Bare top-level map must also validate after onboarding.
	bare := map[string]interface{}{"preloop": entry}
	container, err := adapter.EnsureServerContainer(bare)
	if err != nil {
		t.Fatalf("EnsureServerContainer: %v", err)
	}
	if _, ok := container["preloop"]; !ok {
		t.Fatalf("expected bare map to be the server container, got %#v", bare)
	}
	if _, hasNested := bare["mcpServers"]; hasNested {
		t.Fatalf("bare map must not gain a nested mcpServers key, got %#v", bare)
	}
	bareResult := adapter.ValidateManagedConfig(bare, "https://preloop.example")
	if bareResult["validation_passed"] != true {
		t.Fatalf("expected bare-map validation to pass, got %+v", bareResult)
	}
}

func TestCopilotCLIManagedKindAndSourceType(t *testing.T) {
	if got := managedAgentKindForAgent(copilotCLIAgentName); got != "copilot_cli" {
		t.Fatalf("managedAgentKindForAgent = %q, want copilot_cli", got)
	}
	// Source type must stay desktop_agent so existing enrollment fingerprints
	// and the server allowlist are not re-keyed (#123).
	if got := runtimeSessionSourceTypeForAgent(copilotCLIAgentName); got != "desktop_agent" {
		t.Fatalf("runtimeSessionSourceTypeForAgent = %q, want desktop_agent", got)
	}
}

func TestBuildManagedMCPEnrollmentPlanCopilotCLI(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, ".copilot", "mcp-config.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(configPath, []byte(`{"mcpServers":{}}`), 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}

	agent := AgentConfig{Name: copilotCLIAgentName, ConfigPath: configPath}
	plan, err := buildManagedMCPEnrollmentPlan(agent, "https://preloop.example", "durable-token")
	if err != nil {
		t.Fatalf("buildManagedMCPEnrollmentPlan: %v", err)
	}
	servers, _ := plan.ManagedDocument["mcpServers"].(map[string]interface{})
	preloop, _ := servers["preloop"].(map[string]interface{})
	if preloop["type"] != "http" || preloop["url"] != "https://preloop.example/mcp/v1" {
		t.Fatalf("unexpected managed preloop entry: %#v", preloop)
	}
	headers, _ := preloop["headers"].(map[string]interface{})
	if headers["Authorization"] != "Bearer durable-token" {
		t.Fatalf("unexpected auth header: %#v", headers)
	}
	if plan.ManagedServerURL != "https://preloop.example/mcp/v1" {
		t.Fatalf("unexpected ManagedServerURL: %q", plan.ManagedServerURL)
	}
}
