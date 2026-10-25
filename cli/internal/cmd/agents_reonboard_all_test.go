package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

// Regression: re-running onboarding only touched agents that were never
// onboarded or whose MCP config had drifted, so most onboarded agents kept
// stale hooks/plugins and were missing from the summary.

func TestSelectOnboardingRunCandidatesKeepsOnboardedAgents(t *testing.T) {
	testenv.SetTempHome(t)
	openclaw := AgentConfig{Name: "OpenClaw", ConfigPath: "/tmp/openclaw.json"}
	lists := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/agents" {
			lists++
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(managedAgentListResponse{Items: []managedAgentSummary{{
			ID:                "agent-1",
			DisplayName:       "OpenClaw",
			SessionSourceType: "openclaw",
			SessionSourceID:   runtimePrincipalIDForAgent(openclaw),
			LifecycleState:    "active",
			OnboardingState:   "fully_onboarded",
		}}})
	}))
	defer server.Close()
	client := api.NewClientWithToken(server.URL, "tok")

	discovered := []AgentConfig{
		openclaw,
		{Name: "Codex CLI", ConfigPath: "/tmp/codex.toml"},
		{Name: "Cursor", ConfigPath: "/tmp/cursor.json"},
	}
	candidates := selectOnboardingRunCandidates(client, discovered)
	if lists != 1 {
		t.Fatalf("managed agents must be listed once per batch, got %d list calls", lists)
	}
	if len(candidates) != 3 {
		t.Fatalf("every discovered agent must be a candidate, got %#v", candidates)
	}
	if !candidates[0].IsOnboarded {
		t.Fatalf("OpenClaw should be onboarded, got %#v", candidates[0])
	}
	if candidates[1].IsOnboarded || candidates[2].IsOnboarded {
		t.Fatalf("Codex and Cursor should be fresh onboardings, got %#v", candidates[1:])
	}
}

func TestSelectOnboardingRunCandidatesMarksOutdatedAgents(t *testing.T) {
	testenv.SetTempHome(t)
	openclaw := AgentConfig{Name: "OpenClaw", ConfigPath: "/tmp/openclaw.json"}
	items := []managedAgentSummary{{
		ID:                "agent-1",
		SessionSourceType: "openclaw",
		SessionSourceID:   runtimePrincipalIDForAgent(openclaw),
		LifecycleState:    "active",
		OnboardingState:   "fully_onboarded",
	}}
	current := selectOnboardingRunCandidatesWith(items, []AgentConfig{openclaw}, func(AgentConfig) []string { return nil })
	if !current[0].IsOnboarded || current[0].ReonboardRecommended {
		t.Fatalf("OpenClaw should be onboarded and current, got %#v", current[0])
	}
	stale := func(AgentConfig) []string { return []string{"Agent Control plugin or config is missing or outdated."} }
	outdated := selectOnboardingRunCandidatesWith(items, []AgentConfig{openclaw}, stale)
	if !outdated[0].ReonboardRecommended || len(outdated[0].DriftReasons) != 1 {
		t.Fatalf("outdated plugin must mark the agent outdated, got %#v", outdated[0])
	}
}

func TestSelectOnboardingRunCandidatesDoesNotAskRefreshReasonsForNewAgents(t *testing.T) {
	testenv.SetTempHome(t)
	called := false
	candidates := selectOnboardingRunCandidatesWith(nil, []AgentConfig{{Name: "Cursor", ConfigPath: "/tmp/mcp.json"}}, func(AgentConfig) []string {
		called = true
		return nil
	})
	if len(candidates) != 1 || candidates[0].IsOnboarded || called {
		t.Fatalf("new agent must stay a plain candidate, got %#v (refresh called %v)", candidates, called)
	}
}

func TestDiscoverWithNoLocalAgentsStillListsServerOnlyDeviceAgents(t *testing.T) {
	host, fallback := enrollmentHostnameLabel()
	if fallback {
		t.Skip("no hostname on this machine")
	}
	setupDiscoveryHome(t) // isolated home and probes; restores agentSpecs on cleanup
	agentSpecs = nil      // nothing is discovered locally
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(managedAgentListResponse{Items: []managedAgentSummary{{
			ID: "gone", DisplayName: "Hermes", SessionSourceType: "hermes", SessionSourceID: "x",
			EnrollmentHostname: host, LifecycleState: "active",
		}}})
	}))
	defer server.Close()
	useDiscoveryServer(t, server.URL)
	cmd := newDiscoverTestCmd(t, "--no-onboard-prompt")
	out := captureStdout(t, func() error { return runAgentsDiscover(cmd, nil) })
	if !strings.Contains(out, "No AI agents found on this machine.") ||
		!strings.Contains(out, "Hermes") || !strings.Contains(out, agentOnboardingStatusNotFound) {
		t.Fatalf("discover with no local agents must list the server-only agent, got %q", out)
	}
}

func TestPromptToOnboardCandidatesAsksPerOnboardedAgent(t *testing.T) {
	testenv.SetTempHome(t)
	// Current agent: accept + name. Outdated agent: decline.
	input := strings.NewReader("y\n\nn\n")
	output := &bytes.Buffer{}
	current := AgentConfig{Name: "Cursor", ConfigPath: "/tmp/cursor.json", IsOnboarded: true}
	outdated := AgentConfig{
		Name:                 "Gemini CLI",
		ConfigPath:           "/tmp/gemini.json",
		IsOnboarded:          true,
		ReonboardRecommended: true,
		DriftReasons:         []string{"Agent Control plugin or config is missing or outdated."},
	}
	var enrolled []string
	outcomes, err := promptToOnboardCandidates(input, output, []AgentConfig{current, outdated}, false, false, func(a AgentConfig, _ bool) error {
		enrolled = append(enrolled, a.Name)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	rendered := output.String()
	for _, want := range []string{
		"Cursor (Cursor) is onboarded and current. Re-onboard to refresh its MCP config, hooks and plugins, then re-test?",
		"Gemini CLI is onboarded but outdated:",
		"  - Agent Control plugin or config is missing or outdated.",
		"Update Gemini CLI (Gemini CLI): MCP config, hooks and agent-control plugin, then re-test?",
	} {
		if !strings.Contains(rendered, want) {
			t.Fatalf("missing %q in %q", want, rendered)
		}
	}
	if len(enrolled) != 1 || enrolled[0] != "Cursor" {
		t.Fatalf("only the accepted agent re-onboards, got %v", enrolled)
	}
	if len(outcomes) != 2 || outcomes[1].Status != agentOnboardingStatusUnchanged ||
		!strings.Contains(outcomes[1].Reason, "outdated") {
		t.Fatalf("declined outdated agent must be reported unchanged/outdated, got %#v", outcomes)
	}
}

func TestPromptToOnboardCandidatesYesReonboardsEveryAgent(t *testing.T) {
	testenv.SetTempHome(t)
	output := &bytes.Buffer{}
	agents := []AgentConfig{
		{Name: "Cursor", ConfigPath: "/tmp/cursor.json", IsOnboarded: true},
		{Name: "Gemini CLI", ConfigPath: "/tmp/gemini.json", IsOnboarded: true, ReonboardRecommended: true, DriftReasons: []string{"stale"}},
		{Name: "Codex CLI", ConfigPath: "/tmp/codex.toml"},
	}
	count := 0
	outcomes, err := promptToOnboardCandidates(strings.NewReader(""), output, agents, true, false, func(AgentConfig, bool) error {
		count++
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if count != 3 || len(outcomes) != 3 {
		t.Fatalf("--yes must re-onboard all agents, enrolled %d outcomes %#v", count, outcomes)
	}
	rendered := output.String()
	if !strings.Contains(rendered, "Refreshing Cursor (already onboarded).") ||
		!strings.Contains(rendered, "Updating outdated Gemini CLI: stale") {
		t.Fatalf("unexpected --yes output %q", rendered)
	}
}

func TestOnboardingSummaryListsEveryAgent(t *testing.T) {
	discovered := []AgentConfig{
		{Name: "Cursor", ConfigPath: "/tmp/cursor.json"},
		{Name: "Codex CLI", ConfigPath: "/tmp/codex.toml"},
	}
	outcomes := outcomesFromBestEffort(
		[]AgentConfig{discovered[0]},
		[]agentOnboardingFailure{{Agent: discovered[1], Err: errors.New("boom\nmore")}},
	)
	all := completeDiscoveryOutcomes(discovered, outcomes)
	codex := AgentConfig{Name: "Codex CLI", ConfigPath: "/tmp/codex.toml"}
	items := []managedAgentSummary{
		{ID: "local", DisplayName: "Codex CLI", SessionSourceType: "codex", SessionSourceID: runtimePrincipalIDForAgent(codex), EnrollmentHostname: "example-host", LifecycleState: "active"},
		{ID: "gone", DisplayName: "Hermes", SessionSourceType: "hermes", SessionSourceID: "x", EnrollmentHostname: "EXAMPLE-HOST", LifecycleState: "active"},
		{ID: "retired", DisplayName: "Old", SessionSourceType: "hermes", SessionSourceID: "y", EnrollmentHostname: "example-host", LifecycleState: "decommissioned"},
		{ID: "elsewhere", DisplayName: "Other", SessionSourceType: "hermes", SessionSourceID: "z", EnrollmentHostname: "other-host", LifecycleState: "active"},
	}
	all = appendServerOnlyDeviceAgentsFrom(items, "example-host", discovered, all)
	if len(all) != 3 {
		t.Fatalf("expected 2 local + 1 server-only row, got %#v", all)
	}
	if all[0].Status != agentOnboardingStatusOnboarded || all[1].Status != agentOnboardingStatusFailed || all[1].Reason != "boom" {
		t.Fatalf("unexpected local rows %#v", all[:2])
	}
	if all[2].Status != agentOnboardingStatusNotFound || resolveAgentDisplayName(all[2].Agent) != "Hermes" {
		t.Fatalf("unexpected server-only row %#v", all[2])
	}
	var buf bytes.Buffer
	printAgentOnboardingSummary(&buf, all)
	for _, name := range []string{"Cursor", "Codex CLI", "Hermes"} {
		if !strings.Contains(buf.String(), name) {
			t.Fatalf("summary is missing %s: %q", name, buf.String())
		}
	}
	if got := appendServerOnlyDeviceAgentsFrom(items, "", discovered, nil); len(got) != 0 {
		t.Fatalf("unknown host must add nothing, got %#v", got)
	}
}
