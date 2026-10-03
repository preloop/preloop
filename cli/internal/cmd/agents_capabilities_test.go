package cmd

import (
	"bytes"
	"path/filepath"
	"strings"
	"testing"
)

func TestAgentCapabilitiesRegistry(t *testing.T) {
	expected := map[string]agentCapabilities{
		"Pi":               {controlSupported, controlSupported, controlSupported},
		"DeepSeek Harness": {controlSupported, controlSupported, controlSupported},
		"Claude Code":      {controlSupported, controlSupported, controlSupported},
		"Claude Desktop":   {controlUnsupported, controlUnsupported, controlSupported},
		"Cursor":           {controlUnsupported, controlSupported, controlSupported},
		"Windsurf":         {controlUnsupported, controlUnsupported, controlSupported},
		"VSCode / Copilot": {controlUnsupported, controlUnsupported, controlSupported},
		"Gemini CLI":       {controlSupported, controlUnsupported, controlSupported},
		"OpenCode":         {controlSupported, controlSupported, controlSupported},
		"Codex CLI":        {controlSupported, controlSupported, controlSupported},
		"OpenClaw":         {controlSupported, controlSupported, controlSupported},
		"Hermes":           {controlSupported, controlSupported, controlSupported},
		"Antigravity":      {controlUnsupported, controlUnsupported, controlSupported},
		"Devin":            {controlUnsupported, controlUnsupported, controlSupported},
		"Copilot CLI":      {controlUnsupported, controlSupported, controlSupported},
	}
	if len(expected) != len(agentSpecs) {
		t.Fatalf("update capability coverage for registry: %d expectations, %d registered", len(expected), len(agentSpecs))
	}
	for _, spec := range agentSpecs {
		t.Run(spec.Name, func(t *testing.T) {
			want, ok := expected[spec.Name]
			if !ok {
				t.Fatal("registered agent missing from support matrix")
			}
			agent := AgentConfig{Name: spec.Name}
			if got := capabilitiesForAgent(agent); got != want {
				t.Fatalf("capabilities = %#v, want %#v", got, want)
			}
			label := agentSupportListingLabel(agent)
			for _, axis := range []string{"Model routing:", "native action gate:", "managed MCP:"} {
				if !strings.Contains(label, axis) {
					t.Fatalf("support label missing %s: %s", axis, label)
				}
			}
			if strings.Contains(label, "governed") || strings.Contains(label, "Full") || strings.Contains(label, "configured") {
				t.Fatalf("discovery label implies observed governance: %s", label)
			}
		})
	}
	if got := capabilitiesForAgent(AgentConfig{Name: "Unknown app"}); got != (agentCapabilities{}) {
		t.Fatalf("unknown app must not inherit capabilities: %#v", got)
	}
	for _, alias := range []string{"deepseek", "dsh", " PI "} {
		if got := capabilitiesForAgent(AgentConfig{Name: alias}); got != expected["Pi"] {
			t.Fatalf("harness alias %q: %#v", alias, got)
		}
	}
}

func TestControlDisclosureText(t *testing.T) {
	setPreflightTestEnv(t)
	note := mcpOnlyAgentModelNote(AgentConfig{Name: "Claude Desktop"})
	for _, fragment := range []string{"current Preloop adapter", "managed MCP bridge", "does not configure", "separately verified adapter", "https://code.claude.com/docs/en/claude-apps-gateway", "only calls routed through the managed MCP entry"} {
		if !strings.Contains(note, fragment) {
			t.Fatalf("Desktop disclosure missing %q: %s", fragment, note)
		}
	}
	for _, forbidden := range []string{"by design", "cannot be repointed", "fixed to", "tool calls are governed"} {
		if strings.Contains(note, forbidden) {
			t.Fatalf("unsupported vendor claim %q: %s", forbidden, note)
		}
	}
	var summary bytes.Buffer
	printAgentOnboardingSummary(&summary, []agentOnboardingOutcome{
		classifyAgentOnboardingOutcome(AgentConfig{Name: "Cursor", AuthState: "unknown"}, nil),
		classifyAgentOnboardingOutcome(AgentConfig{Name: "Claude Code", AuthState: "ready"}, nil),
	})
	if !strings.Contains(summary.String(), "application behavior unverified") {
		t.Fatalf("summary must distinguish onboarding from verification: %s", summary.String())
	}
	for _, state := range []string{"fully_onboarded", "mcp_proxy_only", "gateway_only", "incomplete"} {
		if !strings.Contains(onboardingStateNote(state), "application behavior unverified") {
			t.Fatalf("status overstates configuration evidence for %s: %s", state, onboardingStateNote(state))
		}
	}
	var status bytes.Buffer
	printAgentStatusDisclosure(&status, AgentConfig{Name: "Cursor"})
	for _, fragment := range []string{"native action gate: supported", "Enrollment and config records describe configuration", "application behavior unverified", "Only calls routed through the managed MCP entry"} {
		if !strings.Contains(status.String(), fragment) {
			t.Fatalf("status disclosure missing %q: %s", fragment, status.String())
		}
	}
}

func TestControlDisclosureHelpMatchesDispatch(t *testing.T) {
	for _, spec := range agentSpecs {
		if !strings.Contains(agentDiscoverySearchLabel(), spec.Name) {
			t.Fatalf("empty discovery message missing registered app %s", spec.Name)
		}
		if !strings.Contains(agentsDiscoverCmd.Long, spec.Name) {
			t.Fatalf("discovery help missing registered app %s", spec.Name)
		}
		if isApprovalHookSupportedAgent(AgentConfig{Name: spec.Name}) && !strings.Contains(agentsEnrollCmd.Flags().Lookup("approvals").Usage, spec.Name) {
			t.Fatalf("approvals help missing supported app %s", spec.Name)
		}
	}
	if !strings.Contains(agentsEnrollCmd.Flags().Lookup("live-validate").Usage, "direct gateway") {
		t.Fatal("live validation help must identify direct probe")
	}
}

func TestControlDisclosureDiscoveryAndStatusOutput(t *testing.T) {
	home := setPreflightTestEnv(t)
	t.Setenv("PATH", t.TempDir())
	t.Setenv("PRELOOP_TOKEN", "")
	t.Setenv("PRELOOP_URL", "")
	writePreflightFixture(t, filepath.Join(home, ".cursor", "mcp.json"), `{"mcpServers":{}}`)
	oldURL, oldToken := FlagURL, FlagToken
	FlagURL, FlagToken = "", ""
	t.Cleanup(func() { FlagURL, FlagToken = oldURL, oldToken })
	flag := agentsDiscoverCmd.Flags().Lookup("no-onboard-prompt")
	oldValue := flag.Value.String()
	if err := flag.Value.Set("true"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = flag.Value.Set(oldValue) })
	discovery := captureCommandStdout(t, func() error {
		return runAgentsDiscover(agentsDiscoverCmd, nil)
	})
	status := captureCommandStdout(t, func() error {
		return runAgentsStatus(agentsStatusCmd, []string{"Cursor"})
	})
	for _, output := range []string{discovery, status} {
		for _, fragment := range []string{"Model routing: unsupported by current adapter", "native action gate: supported", "managed MCP: supported", "application behavior unverified"} {
			if !strings.Contains(output, fragment) {
				t.Fatalf("captured command output missing %q: %s", fragment, output)
			}
		}
		if strings.Contains(output, "MCP-governed") || strings.Contains(output, "Full (") {
			t.Fatalf("captured command uses overall governance label: %s", output)
		}
	}
}

func TestDirectGatewayProbeDisclosure(t *testing.T) {
	result := deferredLiveValidationResult{
		Agent:   AgentConfig{Name: "Claude Code"},
		Outcome: &managedLiveValidationOutcome{Attempted: true, Passed: true},
	}
	var output bytes.Buffer
	printDeferredLiveValidationLine(&output, result)
	printLiveValidationRoundTripResult(&output, result.Outcome, nil, "synthetic-model", 0)
	for _, fragment := range []string{"direct gateway route/accounting probe", "application behavior unverified"} {
		if strings.Count(output.String(), fragment) != 2 {
			t.Fatalf("both success surfaces need %q: %s", fragment, output.String())
		}
		if !strings.Contains(liveValidationSummaryReason(result), fragment) {
			t.Fatalf("summary missing %q: %s", fragment, liveValidationSummaryReason(result))
		}
	}
	for _, probeStatus := range []string{"passed", "pending", "not_run", "unsupported", "failed"} {
		label := gatewayProbeStatusLabel(probeStatus)
		if !strings.Contains(label, "direct gateway route/accounting probe") || !strings.Contains(label, "application behavior unverified") {
			t.Fatalf("onboarding probe status overstates evidence: %s", label)
		}
	}
}
