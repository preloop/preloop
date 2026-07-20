package cmd

// Regression tests for Fable-defaulted Claude Code onboarding (tester #4,
// 2026-07-20): Max accounts commonly store "claude-fable-5" or the 1M-context
// variant "claude-fable-5[1m]" as the model selection. The bracketed form was
// discarded outright (no model pin at all), the fable family was unknown to
// every selector table, and a stale explicit settings.model outranked the env
// pin — so Claude Code showed "API billing" and silently switched to its API
// default model instead of the managed alias.

import (
	"testing"
)

func TestStripClaudeContextWindowSuffix(t *testing.T) {
	cases := []struct {
		in       string
		want     string
		stripped bool
	}{
		{"claude-fable-5[1m]", "claude-fable-5", true},
		{"claude-fable-5", "claude-fable-5", false},
		{" claude-opus-4-8[200k] ", "claude-opus-4-8", true},
		{"[1m]", "[1m]", false},
		{"", "", false},
		{"claude-fable-5[1m", "claude-fable-5[1m", false},
	}
	for _, testCase := range cases {
		got, stripped := stripClaudeContextWindowSuffix(testCase.in)
		if got != testCase.want || stripped != testCase.stripped {
			t.Errorf(
				"stripClaudeContextWindowSuffix(%q) = (%q, %v), want (%q, %v)",
				testCase.in, got, stripped, testCase.want, testCase.stripped,
			)
		}
	}
}

func TestFableIsAKnownClaudeSelection(t *testing.T) {
	if !isClaudeSelectionKey("fable") {
		t.Error("fable must be a known Claude selection key")
	}
	if alias := claudeSelectionFallbackModelAlias("fable"); alias != "anthropic/claude-fable-5" {
		t.Errorf("unexpected fable fallback alias: %q", alias)
	}
}

func TestApplyClaudeManagedGatewayPinsSettingsModelForNonFamilyAlias(t *testing.T) {
	// The managed document simulates a Max user whose explicit selection was
	// the 1M-context Fable variant; the stale selection must be replaced or
	// it outranks the env pin in API-key mode.
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{
			"model": "claude-fable-5[1m]",
			"mcpServers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"url": "https://preloop.example/mcp/v1",
				},
			},
		},
	}
	plan, err := applyClaudeManagedGateway(
		plan,
		"https://preloop.example",
		"claude-durable-token",
		"anthropic/claude-fable-5",
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}

	if plan.ManagedDocument["model"] != "anthropic/claude-fable-5" {
		t.Errorf(
			"stale settings.model must be replaced by the managed alias, got %#v",
			plan.ManagedDocument["model"],
		)
	}
	env := plan.ManagedDocument["env"].(map[string]interface{})
	if env["ANTHROPIC_MODEL"] != "anthropic/claude-fable-5" {
		t.Errorf("unexpected ANTHROPIC_MODEL: %#v", env["ANTHROPIC_MODEL"])
	}
	if env["ANTHROPIC_CUSTOM_MODEL_OPTION"] != "anthropic/claude-fable-5" {
		t.Errorf(
			"unexpected ANTHROPIC_CUSTOM_MODEL_OPTION: %#v",
			env["ANTHROPIC_CUSTOM_MODEL_OPTION"],
		)
	}
}

func TestApplyClaudeManagedGatewayKeepsFamilySelectionBehavior(t *testing.T) {
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{
			"model": "claude-fable-5[1m]",
			"mcpServers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"url": "https://preloop.example/mcp/v1",
				},
			},
		},
	}
	plan, err := applyClaudeManagedGateway(
		plan,
		"https://preloop.example",
		"claude-durable-token",
		"anthropic/claude-sonnet-4-5",
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}
	if plan.ManagedDocument["model"] != "sonnet" {
		t.Errorf(
			"family aliases keep the selector form in settings.model, got %#v",
			plan.ManagedDocument["model"],
		)
	}
}
