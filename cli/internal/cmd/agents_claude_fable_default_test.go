package cmd

import (
	"reflect"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
)

func TestClaudeCodeVersionHasBuiltinFable(t *testing.T) {
	cases := map[string]bool{
		"2.1.321":         true,
		"2.1.322":         true,
		"2.2.0":           true,
		"3.0.0":           true,
		"2.1.321-beta.1":  true,
		"2.1.319":         false,
		"2.1.320":         false,
		"2.0.999":         false,
		"":                false,
		"not-a-version":   false,
		"  2.1.321  ":     true,
		"1.9.400":         false,
		"2.1.321+build.7": true,
	}
	for version, want := range cases {
		if got := claudeCodeVersionHasBuiltinFable(version); got != want {
			t.Errorf("claudeCodeVersionHasBuiltinFable(%q) = %v, want %v", version, got, want)
		}
	}
}

func TestClaudeFableFollowsBuiltinDefaultMatrix(t *testing.T) {
	cases := []struct {
		name              string
		version           string
		subscriptionOAuth bool
		pin               bool
		want              bool
	}{
		{"new build, oauth, unpinned", "2.1.321", true, false, true},
		{"old build, oauth, unpinned", "2.1.319", true, false, false},
		{"unknown build, oauth", "", true, false, false},
		{"new build, api key", "2.1.321", false, false, false},
		{"new build, oauth, pinned", "2.1.321", true, true, false},
	}
	for _, tc := range cases {
		if got := claudeFableFollowsBuiltinDefault(tc.version, tc.subscriptionOAuth, tc.pin); got != tc.want {
			t.Errorf("%s: got %v, want %v", tc.name, got, tc.want)
		}
	}
}

func TestClaudeOnboardFableFollowsBuiltinDefaultProbesOnlyWhenEligible(t *testing.T) {
	probes := 0
	restore := claudeCodeInstalledVersion
	claudeCodeInstalledVersion = func() string { probes++; return "2.1.321" }
	defer func() { claudeCodeInstalledVersion = restore }()

	agent := AgentConfig{Name: "Claude Code"}
	oauth := &managedGatewayUpstream{CredentialType: anthropicClaudeCodeOAuthCredentialType}
	apiKey := &managedGatewayUpstream{CredentialType: "api_key"}

	if !claudeOnboardFableFollowsBuiltinDefault(agent, oauth, false) {
		t.Fatal("subscription OAuth on 2.1.321 must follow the built-in fable default")
	}
	if claudeOnboardFableFollowsBuiltinDefault(agent, apiKey, false) {
		t.Fatal("API-key accounts keep the Fable pair")
	}
	if claudeOnboardFableFollowsBuiltinDefault(agent, oauth, true) {
		t.Fatal("--pin-model-families keeps the Fable pair")
	}
	if claudeOnboardFableFollowsBuiltinDefault(AgentConfig{Name: "OpenCode"}, oauth, false) {
		t.Fatal("only Claude Code is affected")
	}
	if probes != 1 {
		t.Fatalf("claude --version must only be probed for eligible configs, got %d probes", probes)
	}
}

func fableGatewayTestPlan(builtin bool) managedMCPEnrollmentPlan {
	return managedMCPEnrollmentPlan{
		ClaudeFableBuiltinDefault: builtin,
		ManagedDocument: map[string]interface{}{
			"model": "fable",
			"env": map[string]interface{}{
				"ANTHROPIC_DEFAULT_FABLE_MODEL":      "anthropic/claude-fable-5-1",
				"ANTHROPIC_DEFAULT_FABLE_MODEL_NAME": "Fable (Preloop)",
			},
		},
	}
}

func TestApplyClaudeManagedGatewayDropsFablePairWhenBuiltinDefault(t *testing.T) {
	plan, err := applyClaudeManagedGateway(
		fableGatewayTestPlan(true),
		"https://preloop.example",
		"managed-token",
		"anthropic/claude-fable-5-1",
		[]string{"anthropic/claude-opus-5-5"},
		false,
	)
	if err != nil {
		t.Fatal(err)
	}
	env := plan.ManagedDocument["env"].(map[string]interface{})
	for _, key := range []string{
		"ANTHROPIC_DEFAULT_FABLE_MODEL",
		"ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
		"ANTHROPIC_CUSTOM_MODEL_OPTION",
		"ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
		"ANTHROPIC_MODEL",
		"ANTHROPIC_DEFAULT_OPUS_MODEL",
	} {
		if _, ok := env[key]; ok {
			t.Errorf("%s must not be written when Claude Code has a built-in fable default, got %#v", key, env[key])
		}
	}
	if plan.ManagedDocument["model"] != "fable" {
		t.Errorf("settings.model must keep the fable selector, got %#v", plan.ManagedDocument["model"])
	}
	if !strings.Contains(strings.Join(plan.Notes, "\n"), "built-in fable default") {
		t.Errorf("notes must explain the dropped Fable pin: %#v", plan.Notes)
	}
}

func TestApplyClaudeManagedGatewayKeepsFablePairOnOlderBuild(t *testing.T) {
	plan, err := applyClaudeManagedGateway(
		fableGatewayTestPlan(false),
		"https://preloop.example",
		"managed-token",
		"anthropic/claude-fable-5-2",
		nil,
		false,
	)
	if err != nil {
		t.Fatal(err)
	}
	env := plan.ManagedDocument["env"].(map[string]interface{})
	if env["ANTHROPIC_DEFAULT_FABLE_MODEL"] != "anthropic/claude-fable-5-2" {
		t.Errorf("older builds keep the Fable pair, got %#v", env["ANTHROPIC_DEFAULT_FABLE_MODEL"])
	}
	if env["ANTHROPIC_CUSTOM_MODEL_OPTION"] != "anthropic/claude-fable-5-2" {
		t.Errorf("custom option follows the Fable pin, got %#v", env["ANTHROPIC_CUSTOM_MODEL_OPTION"])
	}
}

func TestApplyClaudeManagedGatewayDropsSiblingFablePairWhenBuiltinDefault(t *testing.T) {
	plan := managedMCPEnrollmentPlan{
		ClaudeFableBuiltinDefault: true,
		ManagedDocument:           map[string]interface{}{},
	}
	plan, err := applyClaudeManagedGateway(
		plan, "https://preloop.example", "managed-token",
		"anthropic/claude-sonnet-5-5", []string{"anthropic/claude-fable-5-1"}, false,
	)
	if err != nil {
		t.Fatal(err)
	}
	env := plan.ManagedDocument["env"].(map[string]interface{})
	if _, ok := env["ANTHROPIC_DEFAULT_FABLE_MODEL"]; ok {
		t.Errorf("sibling Fable pair must be dropped, got %#v", env["ANTHROPIC_DEFAULT_FABLE_MODEL"])
	}
	if env["ANTHROPIC_CUSTOM_MODEL_OPTION"] != "anthropic/claude-sonnet-5-5" {
		t.Errorf("non-Fable custom option is unchanged, got %#v", env["ANTHROPIC_CUSTOM_MODEL_OPTION"])
	}
}

func TestClaudeAllowlistFamilyAdditions(t *testing.T) {
	allowed := []string{
		"anthropic/claude-fable-5-1",
		"anthropic/claude-opus-5-5",
	}
	cases := []struct {
		name       string
		allowed    []string
		candidates []string
		want       []string
	}{
		{"newer fable sibling is added", allowed, []string{"anthropic/claude-fable-5-2"}, []string{"anthropic/claude-fable-5-2"}},
		{"family not named stays denied", allowed, []string{"anthropic/claude-haiku-5-5"}, nil},
		{"unrestricted allowlist untouched", nil, []string{"anthropic/claude-fable-5-2"}, nil},
		{"already covered", allowed, []string{"anthropic/claude-opus-5-5"}, nil},
		{"older sibling is not added", allowed, []string{"anthropic/claude-fable-5"}, nil},
		{"non-family alias ignored", allowed, []string{"openai/gpt-5.4"}, nil},
		{"bare tail entry names family", []string{"claude-fable-5-1"}, []string{"anthropic/claude-fable-5-2"}, []string{"anthropic/claude-fable-5-2"}},
	}
	for _, tc := range cases {
		got := claudeAllowlistFamilyAdditions(tc.allowed, tc.candidates)
		if len(got) == 0 && len(tc.want) == 0 {
			continue
		}
		if !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%s: got %#v, want %#v", tc.name, got, tc.want)
		}
	}
}

func TestExtendAllowlistWithFamilySiblingsPutsNewerSibling(t *testing.T) {
	server := newGovernanceTestServer(t, []string{"anthropic/claude-fable-5-1", "anthropic/claude-opus-5-5"})
	client := api.NewClientWithToken(server.server.URL, "tok")

	added, allowed, err := extendAllowlistWithFamilySiblings(
		client, "agent-1", []string{"anthropic/claude-fable-5-2", "anthropic/claude-haiku-5-5"},
	)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(added, []string{"anthropic/claude-fable-5-2"}) {
		t.Fatalf("added = %#v", added)
	}
	wantAllowed := []string{"anthropic/claude-fable-5-1", "anthropic/claude-opus-5-5", "anthropic/claude-fable-5-2"}
	if !reflect.DeepEqual(allowed, wantAllowed) {
		t.Fatalf("allowed = %#v", allowed)
	}
	if server.putCount != 1 {
		t.Fatalf("expected one governance PUT, got %d", server.putCount)
	}
	if got := stringSlice(t, server.putBody["allowed_models"]); !reflect.DeepEqual(got, wantAllowed) {
		t.Fatalf("PUT allowed_models = %#v", got)
	}
	if _, ok := server.putBody["model_budgets"]; !ok {
		t.Fatal("the PUT must round-trip the rest of the governance config")
	}
}

func TestExtendAllowlistWithFamilySiblingsLeavesUnrelatedAllowlist(t *testing.T) {
	server := newGovernanceTestServer(t, []string{"anthropic/claude-opus-5-5"})
	client := api.NewClientWithToken(server.server.URL, "tok")
	added, _, err := extendAllowlistWithFamilySiblings(client, "agent-1", []string{"anthropic/claude-fable-5-2"})
	if err != nil {
		t.Fatal(err)
	}
	if len(added) != 0 || server.putCount != 0 {
		t.Fatalf("an allowlist that does not name Fable must not be widened (added=%#v, puts=%d)", added, server.putCount)
	}
}

func fableRefreshDoc() map[string]interface{} {
	return map[string]interface{}{
		"model": "fable",
		"env": map[string]interface{}{
			"ANTHROPIC_BASE_URL":                 "https://preloop.example/anthropic",
			"ANTHROPIC_API_KEY":                  "claude-durable-token",
			"ANTHROPIC_MODEL":                    "fable",
			"ANTHROPIC_CUSTOM_MODEL_OPTION":      "anthropic/claude-fable-5-1",
			"ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Preloop anthropic/claude-fable-5-1",
			"ANTHROPIC_DEFAULT_FABLE_MODEL":      "anthropic/claude-fable-5-1",
			"ANTHROPIC_DEFAULT_FABLE_MODEL_NAME": "Fable (Preloop)",
		},
	}
}

func fableRefreshModels() []aiModelResponse {
	return []aiModelResponse{
		refreshTestModel("m-fable-1", "anthropic", "claude-fable-5-1", "anthropic/claude-fable-5-1"),
		refreshTestModel("m-fable-2", "anthropic", "claude-fable-5-2", "anthropic/claude-fable-5-2"),
		refreshTestModel("m-opus", "anthropic", "claude-opus-5-5", "anthropic/claude-opus-5-5"),
	}
}

func TestRefreshClaudeKeepsAllowedFablePinAndWarnsAboutDeniedRelease(t *testing.T) {
	outcome, err := refreshClaudeManagedModelDocumentWithOptions(
		AgentConfig{Name: "Claude Code"},
		fableRefreshDoc(),
		fableRefreshModels(),
		nil,
		claudeRefreshOptions{Allowed: []string{"anthropic/claude-fable-5-1", "anthropic/claude-opus-5-5"}},
	)
	if err != nil {
		t.Fatal(err)
	}
	env := outcome.Doc["env"].(map[string]interface{})
	if env["ANTHROPIC_DEFAULT_FABLE_MODEL"] != "anthropic/claude-fable-5-1" {
		t.Fatalf("pin must stay on the allowed release, got %#v", env["ANTHROPIC_DEFAULT_FABLE_MODEL"])
	}
	warnings := strings.Join(outcome.Warnings, "\n")
	if !strings.Contains(warnings, "anthropic/claude-fable-5-2 is not in this agent's allowed models") {
		t.Fatalf("expected a newer-release warning, got %#v", outcome.Warnings)
	}
}

func TestRefreshClaudeMovesFablePinWhenAllowlistAdmitsNewRelease(t *testing.T) {
	outcome, err := refreshClaudeManagedModelDocumentWithOptions(
		AgentConfig{Name: "Claude Code"},
		fableRefreshDoc(),
		fableRefreshModels(),
		nil,
		claudeRefreshOptions{Allowed: []string{
			"anthropic/claude-fable-5-1", "anthropic/claude-opus-5-5", "anthropic/claude-fable-5-2",
		}},
	)
	if err != nil {
		t.Fatal(err)
	}
	env := outcome.Doc["env"].(map[string]interface{})
	if env["ANTHROPIC_DEFAULT_FABLE_MODEL"] != "anthropic/claude-fable-5-2" {
		t.Fatalf("pin must move to the admitted release, got %#v", env["ANTHROPIC_DEFAULT_FABLE_MODEL"])
	}
	if env["ANTHROPIC_CUSTOM_MODEL_OPTION"] != "anthropic/claude-fable-5-2" {
		t.Fatalf("custom option must follow the Fable pin, got %#v", env["ANTHROPIC_CUSTOM_MODEL_OPTION"])
	}
	if len(outcome.Warnings) != 0 {
		t.Fatalf("no warning expected, got %#v", outcome.Warnings)
	}
}

func TestRefreshClaudeWarnsWhenLiveListHasUnauthorizedFableRelease(t *testing.T) {
	models := fableRefreshModels()[:1]
	outcome, err := refreshClaudeManagedModelDocumentWithOptions(
		AgentConfig{Name: "Claude Code"},
		fableRefreshDoc(),
		models,
		nil,
		claudeRefreshOptions{Live: claudeLiveModelList{
			Attempted: true, Obtained: true, IDs: []string{"claude-fable-5-1", "claude-fable-5-2"},
		}},
	)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(strings.Join(outcome.Warnings, "\n"), "claude-fable-5-2 is available upstream but not authorized") {
		t.Fatalf("expected an upstream-release warning, got %#v", outcome.Warnings)
	}
}

func TestRefreshClaudeDropsFablePairWhenBuiltinDefault(t *testing.T) {
	outcome, err := refreshClaudeManagedModelDocumentWithOptions(
		AgentConfig{Name: "Claude Code"},
		fableRefreshDoc(),
		fableRefreshModels(),
		nil,
		claudeRefreshOptions{FableBuiltinDefault: true},
	)
	if err != nil {
		t.Fatal(err)
	}
	env := outcome.Doc["env"].(map[string]interface{})
	for _, key := range []string{"ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_CUSTOM_MODEL_OPTION", "ANTHROPIC_MODEL"} {
		if _, ok := env[key]; ok {
			t.Errorf("%s must be removed, got %#v", key, env[key])
		}
	}
	if outcome.Doc["model"] != "fable" {
		t.Errorf("settings.model keeps the fable selector, got %#v", outcome.Doc["model"])
	}
	if !strings.Contains(strings.Join(outcome.Notices, "\n"), "ANTHROPIC_DEFAULT_FABLE_MODEL") {
		t.Errorf("notice must name the removed Fable pin: %#v", outcome.Notices)
	}
}

func TestClaudeRefreshFamilyTargetAliasPrefersNewerLiveRelease(t *testing.T) {
	family, _ := claudeFamilyForSelector("fable")
	models := fableRefreshModels()[:1]
	live := claudeLiveModelList{Attempted: true, Obtained: true, IDs: []string{
		"claude-fable-5-1", "claude-fable-5-2", "claude-fable-5-2-20261101",
	}}
	if got := claudeRefreshFamilyTargetAlias(family, models, nil, live); got != "anthropic/claude-fable-5-2" {
		t.Fatalf("expected the newer live release, got %q", got)
	}
	if got := claudeRefreshFamilyTargetAlias(family, models, nil, claudeLiveModelList{}); got != "anthropic/claude-fable-5-1" {
		t.Fatalf("without a live list the catalog row wins, got %q", got)
	}
	older := claudeLiveModelList{Attempted: true, Obtained: true, IDs: []string{"claude-fable-5"}}
	if got := claudeRefreshFamilyTargetAlias(family, models, nil, older); got != "anthropic/claude-fable-5-1" {
		t.Fatalf("an older live id must not replace the catalog row, got %q", got)
	}
}
