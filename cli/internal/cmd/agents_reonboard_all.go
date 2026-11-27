package cmd

// Re-onboarding covers every agent, not only the never-onboarded ones.
//
// A batch onboarding run (`preloop agents onboard` without an agent, `--all`,
// and the discover-driven prompt) used to drop every agent that already had
// local enrollment state or a complete server record, unless its MCP config
// had drifted. Rebuilding the CLI and re-running onboarding therefore left
// most agents on stale hooks, agent-control plugins and MCP config, and the
// summary did not mention them. Now every discovered agent is a candidate:
// already-onboarded ones are labelled current or outdated (with the reasons)
// and the operator is asked per agent; --yes re-onboards them all.

import (
	"fmt"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
)

const (
	// agentOnboardingStatusUnchanged marks an onboarded agent whose
	// re-onboarding was declined or not attempted.
	agentOnboardingStatusUnchanged = "unchanged"
	// agentOnboardingStatusNotFound marks a server-side agent enrolled from
	// this device that discovery did not find locally.
	agentOnboardingStatusNotFound = "not found locally"
)

// selectOnboardingRunCandidates returns every discovered agent for a batch
// onboarding run, annotated with whether it is already onboarded and, if so,
// why it is outdated. Agents are never dropped for being onboarded. Managed
// agents are listed once for the whole batch.
func selectOnboardingRunCandidates(client *api.Client, discovered []AgentConfig) []AgentConfig {
	var items []managedAgentSummary
	if client != nil && client.IsAuthenticated() {
		items, _ = listManagedAgents(client)
	}
	return selectOnboardingRunCandidatesWith(items, discovered, onboardingRefreshReasons)
}

func selectOnboardingRunCandidatesWith(
	items []managedAgentSummary,
	discovered []AgentConfig,
	refreshReasons func(AgentConfig) []string,
) []AgentConfig {
	candidates := make([]AgentConfig, 0, len(discovered))
	for _, agent := range discovered {
		agent = normalizeDiscoveredAgent(agent)
		if enriched, err := enrichDiscoveredAgentWithRemote(agent, findManagedAgentForDiscovered(items, agent)); err == nil {
			agent = enriched
		}
		if agent.IsOnboarded {
			for _, reason := range refreshReasons(agent) {
				if !containsString(agent.DriftReasons, reason) {
					agent.DriftReasons = append(agent.DriftReasons, reason)
				}
			}
			if len(agent.DriftReasons) > 0 {
				agent.ReonboardRecommended = true
			}
		}
		candidates = append(candidates, agent)
	}
	return candidates
}

// onboardingRefreshReasons lists outdated managed pieces beyond the MCP
// config drift enrichDiscoveredAgent already reports.
func onboardingRefreshReasons(agent AgentConfig) []string {
	var reasons []string
	if supportsAgentControlChannel(agent) && agentControlNeedsOnboardingRefresh(agent) {
		reasons = append(reasons, "Agent Control plugin or config is missing or outdated.")
	}
	return reasons
}

// appendServerOnlyDeviceAgents adds a summary row for every active agent the
// server says was enrolled from this host that local discovery did not find,
// so the summary accounts for every agent onboarded on this device.
func appendServerOnlyDeviceAgents(
	client *api.Client,
	discovered []AgentConfig,
	outcomes []agentOnboardingOutcome,
) []agentOnboardingOutcome {
	if client == nil || !client.IsAuthenticated() {
		return outcomes
	}
	items, err := listManagedAgents(client)
	if err != nil {
		return outcomes
	}
	host, fallback := enrollmentHostnameLabel()
	if fallback {
		// "unknown-host" would match every other host without a name.
		return outcomes
	}
	return appendServerOnlyDeviceAgentsFrom(items, host, discovered, outcomes)
}

func appendServerOnlyDeviceAgentsFrom(
	items []managedAgentSummary,
	host string,
	discovered []AgentConfig,
	outcomes []agentOnboardingOutcome,
) []agentOnboardingOutcome {
	host = strings.TrimSpace(host)
	if host == "" {
		return outcomes
	}
	matched := map[string]bool{}
	for _, agent := range discovered {
		if item := findManagedAgentForDiscovered(items, normalizeDiscoveredAgent(agent)); item != nil {
			matched[item.ID] = true
		}
	}
	for _, item := range items {
		if matched[item.ID] || !strings.EqualFold(strings.TrimSpace(item.EnrollmentHostname), host) {
			continue
		}
		switch strings.ToLower(strings.TrimSpace(item.LifecycleState)) {
		case "", "active":
		default:
			continue
		}
		name := strings.TrimSpace(item.DisplayName)
		if name == "" {
			name = item.SessionSourceType
		}
		outcomes = append(outcomes, agentOnboardingOutcome{
			Agent:  AgentConfig{Name: name, DisplayName: name},
			Status: agentOnboardingStatusNotFound,
			Reason: fmt.Sprintf(
				"enrolled from this device in Preloop but not detected here; reinstall it or run: preloop agents remove %s",
				shellQuoteAgentName(name),
			),
		})
	}
	return outcomes
}
