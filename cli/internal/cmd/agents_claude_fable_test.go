package cmd

// Regression tests for Fable-defaulted Claude Code onboarding (tester #4,
// 2026-07-20): Max accounts commonly store "claude-fable-5" or the 1M-context
// variant "claude-fable-5[1m]" as the model selection. The bracketed form was
// discarded outright (no model pin at all), the fable family was unknown to
// every selector table, and a stale explicit settings.model outranked the env
// pin — so Claude Code showed "API billing" and silently switched to its API
// default model instead of the managed alias.

import (
	"strings"
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

func TestApplyClaudeManagedGatewayTreatsFableAsFamily(t *testing.T) {
	// Fable is a first-class Claude family: a fable-pinned Max account keeps
	// the selector form in settings.model (like opus/sonnet/haiku) and gets
	// its alias mapped through ANTHROPIC_DEFAULT_FABLE_MODEL, NOT collapsed
	// through the non-family path that flattens every selector onto one
	// alias and destroys /model switching.
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
		nil,
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}

	if plan.ManagedDocument["model"] != "fable" {
		t.Errorf(
			"fable aliases keep the selector form in settings.model, got %#v",
			plan.ManagedDocument["model"],
		)
	}
	env := plan.ManagedDocument["env"].(map[string]interface{})
	if env["ANTHROPIC_MODEL"] != "fable" {
		t.Errorf("unexpected ANTHROPIC_MODEL: %#v", env["ANTHROPIC_MODEL"])
	}
	if env["ANTHROPIC_DEFAULT_FABLE_MODEL"] != "anthropic/claude-fable-5" {
		t.Errorf(
			"unexpected ANTHROPIC_DEFAULT_FABLE_MODEL: %#v",
			env["ANTHROPIC_DEFAULT_FABLE_MODEL"],
		)
	}
	if env["ANTHROPIC_CUSTOM_MODEL_OPTION"] != "anthropic/claude-fable-5" {
		t.Errorf(
			"unexpected ANTHROPIC_CUSTOM_MODEL_OPTION: %#v",
			env["ANTHROPIC_CUSTOM_MODEL_OPTION"],
		)
	}
	// The subagent override must stay unset: with family keys covered,
	// subagents resolve through the same selector chain as stock Claude Code.
	if _, ok := env[claudeSubagentModelEnvKey]; ok {
		t.Errorf("CLAUDE_CODE_SUBAGENT_MODEL must not be pinned for family models")
	}
}

func TestApplyClaudeManagedGatewayWritesSiblingFamilyEnv(t *testing.T) {
	// Sibling family aliases imported at onboard time must each land on
	// their own ANTHROPIC_DEFAULT_<FAMILY>_MODEL key so /model switching,
	// background (haiku fast-path) requests, and subagents on another
	// family resolve at the gateway instead of 404ing.
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{
			"model": "claude-fable-5[1m]",
		},
	}
	plan, err := applyClaudeManagedGateway(
		plan,
		"https://preloop.example",
		"claude-durable-token",
		"anthropic/claude-fable-5",
		[]string{
			"anthropic/claude-opus-4-6",
			"anthropic/claude-sonnet-4-5",
			"anthropic/claude-haiku-4-5",
		},
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}
	env := plan.ManagedDocument["env"].(map[string]interface{})
	for key, want := range map[string]string{
		"ANTHROPIC_DEFAULT_FABLE_MODEL":  "anthropic/claude-fable-5",
		"ANTHROPIC_DEFAULT_OPUS_MODEL":   "anthropic/claude-opus-4-6",
		"ANTHROPIC_DEFAULT_SONNET_MODEL": "anthropic/claude-sonnet-4-5",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL":  "anthropic/claude-haiku-4-5",
	} {
		if env[key] != want {
			t.Errorf("env[%q] = %#v, want %q", key, env[key], want)
		}
	}
	// The pinned family's alias wins over any sibling alias for the same
	// family: the pinned alias is prepended to the coverage list.
	if env["ANTHROPIC_MODEL"] != "fable" {
		t.Errorf("unexpected ANTHROPIC_MODEL: %#v", env["ANTHROPIC_MODEL"])
	}
}

func TestApplyClaudeManagedGatewaySiblingEnvClearedOnReonboard(t *testing.T) {
	// Re-onboarding with a smaller family set must clear the keys of
	// families that no longer resolve — a /model entry that 404s is worse
	// than no entry.
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{
			"env": map[string]interface{}{
				"ANTHROPIC_DEFAULT_OPUS_MODEL":      "anthropic/claude-opus-4-6",
				"ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": "Opus (Preloop)",
			},
		},
	}
	plan, err := applyClaudeManagedGateway(
		plan,
		"https://preloop.example",
		"claude-durable-token",
		"anthropic/claude-fable-5",
		nil,
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}
	env := plan.ManagedDocument["env"].(map[string]interface{})
	if _, ok := env["ANTHROPIC_DEFAULT_OPUS_MODEL"]; ok {
		t.Errorf("stale opus key must be cleared when the family is no longer covered")
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
		nil,
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

func TestClaudeShellBedrockOverrideNotes(t *testing.T) {
	// applyClaudeManagedGateway neutralizes Bedrock only inside settings.json.
	// A shell-exported CLAUDE_CODE_USE_BEDROCK survives onboarding in the
	// agent's process environment and can override the gateway config, so it
	// must produce an actionable note.

	cases := []struct {
		name    string
		flag    string
		awsKey  string
		want    bool
		wantAWS bool
	}{
		{"flag exported triggers a warning", "1", "", true, false},
		{"flag true triggers a warning", "true", "", true, false},
		{"flag 0 is silent", "0", "", false, false},
		{"flag unset is silent", "", "", false, false},
		{"aws keys are listed alongside the flag", "1", "AKIAIOSFODNN7EXAMPLE", true, true},
	}

	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			t.Setenv("CLAUDE_CODE_USE_BEDROCK", testCase.flag)
			t.Setenv("AWS_ACCESS_KEY_ID", testCase.awsKey)
			t.Setenv("AWS_BEARER_TOKEN_BEDROCK", "")
			t.Setenv("AWS_SECRET_ACCESS_KEY", "")
			t.Setenv("AWS_SESSION_TOKEN", "")
			t.Setenv("AWS_REGION", "")
			t.Setenv("AWS_DEFAULT_REGION", "")

			notes := claudeShellBedrockOverrideNotes()
			if !testCase.want {
				if len(notes) != 0 {
					t.Fatalf("expected no notes, got %#v", notes)
				}
				return
			}
			if len(notes) != 1 {
				t.Fatalf("expected exactly one note, got %#v", notes)
			}
			if !strings.Contains(notes[0], "CLAUDE_CODE_USE_BEDROCK") {
				t.Errorf("note must name the overriding variable: %q", notes[0])
			}
			hasAWSMention := strings.Contains(notes[0], "AWS_ACCESS_KEY_ID")
			if testCase.wantAWS && !hasAWSMention {
				t.Errorf("exported AWS key must be listed: %q", notes[0])
			}
			if !testCase.wantAWS && hasAWSMention {
				t.Errorf("no AWS key exported; note must not list one: %q", notes[0])
			}
		})
	}
}

func TestApplyClaudeManagedGatewayWarnsOnShellBedrockOverride(t *testing.T) {
	t.Setenv("CLAUDE_CODE_USE_BEDROCK", "1")
	t.Setenv("AWS_SECRET_ACCESS_KEY", "")

	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{
			"model": "claude-sonnet-4-5",
		},
	}
	plan, err := applyClaudeManagedGateway(
		plan,
		"https://preloop.example",
		"claude-durable-token",
		"anthropic/claude-sonnet-4-5",
		nil,
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}

	found := false
	for _, note := range plan.Notes {
		if strings.Contains(note, "CLAUDE_CODE_USE_BEDROCK") {
			found = true
		}
	}
	if !found {
		t.Errorf("expected a shell-override warning note, got %#v", plan.Notes)
	}
}
