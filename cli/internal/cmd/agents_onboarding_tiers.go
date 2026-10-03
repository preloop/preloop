package cmd

// Credential-ready onboarding tiers. These planning probes establish adapter
// support and available credentials, not that the application consumed config.
// A direct gateway route/accounting probe runs separately after onboarding.

import (
	"bufio"
	"fmt"
	"io"

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

// printModelRoutingTierExplanation introduces the tier-2 group once per run,
// before any of its agents are onboarded or prompted for.
func printModelRoutingTierExplanation(
	writer io.Writer,
	unverified []AgentConfig,
	reasons []string,
	autoApprove bool,
) {
	if len(unverified) == 0 {
		return
	}
	fmt.Fprintln( //nolint:errcheck
		writer,
		"\nThese agents lack automatic model-routing support or usable provider credentials; application behavior unverified:",
	)
	for i, agent := range unverified {
		reason := ""
		if i < len(reasons) {
			reason = reasons[i]
		}
		if reason == "" {
			reason = "model-routing credentials are not ready"
		}
		fmt.Fprintf(writer, "  - %s: %s\n", resolveAgentDisplayName(agent), reason) //nolint:errcheck
	}
	if autoApprove {
		fmt.Fprintln( //nolint:errcheck
			writer,
			"Onboarding them as well: supported integrations will be configured. Only calls routed through the managed MCP entry reach Preloop; application behavior unverified.",
		)
		return
	}
	fmt.Fprintln( //nolint:errcheck
		writer,
		"Onboarding configures supported integrations. Only calls routed through the managed MCP entry reach Preloop; application behavior unverified. Onboard anyway?",
	)
}

// promptToOnboardCandidatesTiered wraps promptToOnboardCandidates with the
// two-tier ordering: verified-model-routing agents first, then the
// MCP-only/unverified tier behind its explanation. A single buffered reader is
// shared across both passes so no interactive input is lost between them
// (bufio.NewReader returns an existing *bufio.Reader unchanged).
func promptToOnboardCandidatesTiered(
	reader io.Reader,
	writer io.Writer,
	client *api.Client,
	candidates []AgentConfig,
	autoApprove bool,
	askApprovals bool,
	enroll func(agent AgentConfig, approvals bool) error,
) ([]agentOnboardingOutcome, error) {
	verified, unverified, reasons := partitionCandidatesByModelRouting(client, candidates)
	bufferedReader := bufio.NewReader(reader)

	if len(verified) > 0 && len(unverified) > 0 {
		fmt.Fprintf( //nolint:errcheck
			writer,
			"Onboarding %d agent(s) with model-routing support and available credentials first (application behavior unverified).\n",
			len(verified),
		)
	}
	outcomes, err := promptToOnboardCandidates(bufferedReader, writer, verified, autoApprove, askApprovals, enroll)
	if err != nil {
		return outcomes, err
	}
	if len(unverified) == 0 {
		return outcomes, nil
	}
	printModelRoutingTierExplanation(writer, unverified, reasons, autoApprove)
	secondTier, err := promptToOnboardCandidates(bufferedReader, writer, unverified, autoApprove, askApprovals, enroll)
	outcomes = append(outcomes, secondTier...)
	return outcomes, err
}
