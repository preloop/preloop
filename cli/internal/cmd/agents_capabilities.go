package cmd

import (
	"fmt"
	"io"
	"strings"
)

// controlSupport describes adapter support, never installation or runtime evidence.
type controlSupport uint8

const (
	controlUnknown controlSupport = iota
	controlUnsupported
	controlSupported
)

// agentCapabilities keeps the three independent control paths separate.
// It is internal display metadata, not part of the discovery JSON contract.
type agentCapabilities struct {
	ModelRoute       controlSupport
	NativeActionGate controlSupport
	ManagedMCP       controlSupport
}

func capabilitiesForAgent(agent AgentConfig) agentCapabilities {
	known := isExtensionHarness(agent)
	managedMCP := known
	for _, spec := range agentSpecs {
		if strings.EqualFold(strings.TrimSpace(agent.Name), spec.Name) {
			known = true
			managedMCP = spec.Parser != nil
			break
		}
	}
	if !known {
		return agentCapabilities{}
	}
	capabilities := agentCapabilities{
		ModelRoute:       controlUnsupported,
		NativeActionGate: controlUnsupported,
		ManagedMCP:       controlUnsupported,
	}
	if managedMCP {
		capabilities.ManagedMCP = controlSupported
	}
	// Claude Desktop routes through the OS managed configuration, which the
	// default onboarding installs on this device (or --model-route prints
	// for an MDM).
	if supportsManagedGateway(agent) || isOpenClawAgent(agent) || isClaudeDesktopAgent(agent) {
		capabilities.ModelRoute = controlSupported
	}
	// Local permission hooks and the runtime plugin/control-channel adapters
	// are distinct dispatch paths for native action gates.
	if isApprovalHookSupportedAgent(agent) || supportsAgentControlChannel(agent) {
		capabilities.NativeActionGate = controlSupported
	}
	return capabilities
}

func controlSupportLabel(support controlSupport) string {
	switch support {
	case controlSupported:
		return "supported"
	case controlUnsupported:
		return "unsupported by current adapter"
	default:
		return "unknown"
	}
}

func agentSupportListingLabel(agent AgentConfig) string {
	capabilities := capabilitiesForAgent(agent)
	return fmt.Sprintf(
		"Model routing: %s; native action gate: %s; managed MCP: %s (managed entry only). Adapter support; application behavior unverified.",
		controlSupportLabel(capabilities.ModelRoute),
		controlSupportLabel(capabilities.NativeActionGate),
		controlSupportLabel(capabilities.ManagedMCP),
	)
}

func agentDiscoverySearchLabel() string {
	names := make([]string, 0, len(agentSpecs))
	for _, spec := range agentSpecs {
		names = append(names, spec.Name)
	}
	return "Looked for: " + strings.Join(names, ", ")
}

const mcpOnlySupportLabel = "model routing unsupported by current Preloop adapter"

const directGatewayProbeEvidence = "direct gateway route/accounting probe passed; application behavior unverified"

func printAgentStatusDisclosure(writer io.Writer, agent AgentConfig) {
	fmt.Fprintf(writer, "Adapter support: %s\n", agentSupportListingLabel(agent))                                                                                                 //nolint:errcheck
	fmt.Fprintln(writer, "Enrollment and config records describe configuration; application behavior unverified. Only calls routed through the managed MCP entry reach Preloop.") //nolint:errcheck
	if label := agentNoteDeliveryLabel(agent); label != "" {
		fmt.Fprintf(writer, "Operator notes: %s\n", label) //nolint:errcheck
	}
}

// cursorHeadlessStopEvidenceVersion is the cursor-agent build on which a
// headless --print run was observed to emit sessionStart and sessionEnd but no
// stop hook (#1684).
const cursorHeadlessStopEvidenceVersion = "2026.10.01-e373342"

// agentNoteDeliveryLabel discloses operator-note delivery limits that the
// adapter support line cannot express. It is empty when there is nothing to
// add beyond the operator-note guide.
func agentNoteDeliveryLabel(agent AgentConfig) string {
	if permissionSourceForAgent(agent) != permissionSourceCursor {
		return ""
	}
	return "delivered at startup and tool calls; headless --print runs fire no stop hook " +
		"(cursor-agent " + cursorHeadlessStopEvidenceVersion + "), so a session note queued " +
		"after the last tool call is not rescued at stop and is marked undeliverable at " +
		"session end; interactive mode unverified."
}

func gatewayProbeStatusLabel(status string) string {
	if status == "passed" {
		return directGatewayProbeEvidence
	}
	return "direct gateway route/accounting probe: " + status + "; application behavior unverified"
}
