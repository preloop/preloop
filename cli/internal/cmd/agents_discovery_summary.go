package cmd

import (
	"fmt"
	"os"
	"path/filepath"
)

// completeDiscoveryOutcomes retains discovery order and accounts for agents
// excluded from the onboarding prompt loop, without counting them as attempts.
func completeDiscoveryOutcomes(discovered []AgentConfig, attempted []agentOnboardingOutcome) []agentOnboardingOutcome {
	outcomes := make([]agentOnboardingOutcome, 0, len(discovered))
	for _, agent := range discovered {
		found := false
		for _, outcome := range attempted {
			if outcome.Agent.Name == agent.Name && outcome.Agent.ConfigPath == agent.ConfigPath {
				outcomes = append(outcomes, outcome)
				found = true
				break
			}
		}
		if found {
			continue
		}
		outcome := agentOnboardingOutcome{Agent: agent, Status: "skipped"}
		switch {
		case agentRuntimeMissing(agent):
			outcome.Reason = runtimeMissingListingLabel
			outcome.MissingExecutable = true
		case agent.IsOnboarded && !agent.ReonboardRecommended:
			outcome.Status = agentOnboardingStatusUnchanged
			outcome.Reason = "existing enrollment; " + onboardingStateLabel(agent.OnboardingState)
			capabilities := capabilitiesForAgent(agent)
			outcome.Reason += "; model routing: " + controlSupportLabel(capabilities.ModelRoute)
			if isApprovalHookSupportedAgent(agent) {
				outcome.Reason += fmt.Sprintf("; native approvals supported (not checked); install/repair: preloop agents onboard %s --approvals", shellQuoteAgentName(resolveAgentDisplayName(agent)))
			} else {
				outcome.Reason += "; native approvals unsupported by current adapter"
			}
			outcome.Reason += "; application behavior unverified"
		default:
			outcome.Reason = "onboarding not attempted"
		}
		outcomes = append(outcomes, outcome)
	}
	return outcomes
}

// Retired adapters are available only to undo an existing local enrollment.
func appendRetiredEnrolledAgents(agents []AgentConfig) []AgentConfig {
	home, err := os.UserHomeDir()
	if err != nil {
		return agents
	}
	agent := AgentConfig{Name: "Windsurf", ConfigPath: filepath.Join(home, ".codeium", "windsurf", "mcp_config.json")}
	if state, err := loadLocalEnrollmentState(agent); err == nil {
		agent.DisplayName = state.DisplayName
		agent.RuntimePrincipalID = state.RuntimePrincipalID
		agents = append(agents, agent)
	}
	return agents
}

func detectVSCodeInstall(home string) (string, bool) {
	dirs := userConfigDirs(home)
	if len(dirs) == 0 {
		return "", false
	}
	variants := []struct{ command, app string }{{"code", "Code"}, {"code-insiders", "Code - Insiders"}, {"codium", "VSCodium"}}
	for _, variant := range variants {
		if _, err := runtimeExecutableProbe(variant.command); err == nil {
			return filepath.Join(dirs[0], variant.app, "User", "mcp.json"), true
		}
	}
	for _, dir := range dirs {
		for _, variant := range variants {
			userDir := filepath.Join(dir, variant.app, "User")
			if info, err := os.Stat(userDir); err == nil && info.IsDir() {
				return filepath.Join(userDir, "mcp.json"), true
			}
		}
	}
	return "", false
}
