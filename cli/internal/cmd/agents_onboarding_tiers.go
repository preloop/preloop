package cmd

// Validate-first onboarding tiers.
//
// Quick and interactive batch onboarding used to walk candidates in discovery
// order, so an agent that could only be MCP-governed (or whose model
// credential could not be resolved) was interleaved with agents whose model
// routing works end to end. Onboarding now happens in two tiers:
//
//	Tier 1  agents whose model routing is verified to be workable: the agent
//	        type supports gateway routing AND a model credential was resolved
//	        locally (resolveManagedGatewayUpstream) or is already stored in
//	        the Preloop account (serverHasReusableGatewayCredential). For
//	        OpenClaw — which syncs its own multi-model bindings — the
//	        existing auth-state probe stands in.
//	Tier 2  everything else: MCP-only agent types, and gateway-capable agents
//	        with no verifiable credential. These are onboarded after tier 1,
//	        behind a printed explanation (interactive flows then ask
//	        per-agent, --yes keeps onboarding them for backward
//	        compatibility).
//
// This reuses the existing preflight/planning probes; no new validation path
// is introduced. Live validation still runs after onboarding exactly as
// before.

import (
	"bufio"
	"fmt"
	"io"

	"github.com/preloop/preloop/cli/internal/api"
)

// agentModelRoutingVerified reports whether onboarding this agent can be
// expected to yield working model routing, with a one-line reason when not.
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
		"\nThese agents can be governed for tool calls (MCP), but Preloop couldn't verify model routing for them:",
	)
	for i, agent := range unverified {
		reason := ""
		if i < len(reasons) {
			reason = reasons[i]
		}
		if reason == "" {
			reason = "model routing could not be verified"
		}
		fmt.Fprintf(writer, "  - %s: %s\n", resolveAgentDisplayName(agent), reason) //nolint:errcheck
	}
	if autoApprove {
		fmt.Fprintln( //nolint:errcheck
			writer,
			"Onboarding them as well: MCP governance applies now; model traffic stays direct until routing can be verified.",
		)
		return
	}
	fmt.Fprintln( //nolint:errcheck
		writer,
		"Onboarding them still applies MCP governance now; model traffic stays direct until routing can be verified. Onboard anyway?",
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
			"Onboarding %d agent(s) with verified model routing first.\n",
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
