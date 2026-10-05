package cmd

import "github.com/spf13/cobra"

// discoveryJSON is deliberately separate from AgentConfig, which retains raw
// configuration for onboarding. Never add free-form fields to this DTO.
type discoveryJSON struct {
	Name                 string `json:"name"`
	AppID                string `json:"app_id"`
	MCPServerCount       int    `json:"mcp_server_count"`
	IsOnboarded          bool   `json:"is_onboarded,omitempty"`
	OnboardingState      string `json:"onboarding_state,omitempty"`
	AuthState            string `json:"auth_state,omitempty"`
	SupportLevel         string `json:"support_level,omitempty"`
	RuntimeState         string `json:"runtime_state,omitempty"`
	ConfigDrift          bool   `json:"config_drift,omitempty"`
	ReonboardRecommended bool   `json:"reonboard_recommended,omitempty"`
}

// Fixed IDs are part of the inventory wire contract, independent of display
// names, user-configured names, or runtime principal identifiers.
var inventoryAppIDs = map[string]string{
	"Pi": "pi", "DeepSeek Harness": "deepseek-harness",
	"Claude Code": "claude-code", "Claude Desktop": "claude-desktop",
	"Cursor": "cursor", "Windsurf": "windsurf", "VSCode / Copilot": "vscode-copilot",
	"Gemini CLI": "gemini-cli", "OpenCode": "opencode", "Codex CLI": "codex-cli",
	"OpenClaw": "openclaw", "Hermes": "hermes", "Antigravity": "antigravity",
	"Devin": "devin", "Copilot CLI": "copilot-cli",
}

func safeDiscoveryJSON(agents []AgentConfig) []discoveryJSON {
	result := make([]discoveryJSON, 0, len(agents))
	for _, agent := range agents {
		id, known := inventoryAppIDs[agent.Name]
		if !known {
			continue
		}
		result = append(result, discoveryJSON{
			Name: agent.Name, AppID: id, MCPServerCount: len(agent.MCPServers),
			IsOnboarded:     agent.IsOnboarded,
			OnboardingState: allowDiscoveryEnum(agent.OnboardingState, "fully_onboarded", "mcp_proxy_only", "gateway_only", "incomplete"),
			AuthState:       allowDiscoveryEnum(agent.AuthState, "ready", "not_logged_in", "unknown"),
			SupportLevel:    allowDiscoveryEnum(agent.SupportLevel, "full", "mcp-only"),
			RuntimeState:    allowDiscoveryEnum(agent.RuntimeState, "present", "missing", "unknown"),
			ConfigDrift:     agent.ConfigDrift, ReonboardRecommended: agent.ReonboardRecommended,
		})
	}
	return result
}

func allowDiscoveryEnum(value string, allowed ...string) string {
	for _, candidate := range allowed {
		if value == candidate {
			return value
		}
	}
	return ""
}

func isSafeDiscoveryJSONCommand(cmd *cobra.Command) bool {
	if cmd == nil || cmd.Name() != "discover" || cmd.Parent() == nil || cmd.Parent().Name() != "agents" {
		return false
	}
	asJSON, _ := cmd.Flags().GetBool("json")
	return asJSON
}

// isPromptFreeJSONCommand reports agent commands whose stdout is a JSON
// document. The daily update check writes its prompt to stdout and can block
// on stdin, which corrupts that document and hangs a redirected run.
func isPromptFreeJSONCommand(cmd *cobra.Command) bool {
	if cmd != nil && cmd.Parent() != nil && cmd.Parent().Name() == "ci" {
		return true
	}
	if isSafeDiscoveryJSONCommand(cmd) {
		return true
	}
	if cmd == nil || cmd.Parent() == nil || cmd.Parent().Name() != "agents" {
		return false
	}
	if cmd.Name() != "status" && cmd.Name() != "list" {
		return false
	}
	asJSON, err := cmd.Flags().GetBool("json")
	return err == nil && asJSON
}
