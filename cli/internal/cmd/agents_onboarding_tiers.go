package cmd

// Credential-ready onboarding tiers. These planning probes establish adapter
// support and available credentials, not that the application consumed config.
// A direct gateway route/accounting probe runs separately after onboarding.

import (
	"github.com/preloop/preloop/cli/internal/api"
)

// agentModelRoutingVerified is a legacy internal name for credential readiness.
// It does not verify application routing or behavior.
func agentModelRoutingVerified(client *api.Client, agent AgentConfig) (bool, string) {
	if supportLevelForAgent(agent) != agentSupportLevelFull {
		return false, mcpOnlySupportLabel
	}
	if isOpenClawAgent(agent) {
		// OpenClaw routes through its own multi-model binding sync; the
		// pre-onboarding auth probe already says whether provider
		// credentials exist.
		if resolvedAgentAuthState(agent) == agentAuthStateReady {
			return true, ""
		}
		return false, "model routing supported, but no provider credentials are configured yet"
	}
	upstream, err := resolveManagedGatewayUpstream(agent)
	if err != nil {
		return false, "model routing supported, but resolving the local model credential failed: " + firstErrorLine(err)
	}
	if upstream != nil && upstream.CanRouteThroughGateway() {
		return true, ""
	}
	if serverHasReusableGatewayCredential(client, agent, upstream) {
		return true, ""
	}
	return false, "model routing supported, but no usable model credential was found locally"
}

// partitionCandidatesByModelRouting splits onboarding candidates into the
// verified tier and the unverified tier; reasons is parallel to unverified.
func partitionCandidatesByModelRouting(
	client *api.Client,
	candidates []AgentConfig,
) (verified, unverified []AgentConfig, reasons []string) {
	for _, agent := range candidates {
		ok, reason := agentModelRoutingVerified(client, agent)
		if ok {
			verified = append(verified, agent)
			continue
		}
		unverified = append(unverified, agent)
		reasons = append(reasons, reason)
	}
	return verified, unverified, reasons
}
