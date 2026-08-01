package cmd

import (
	"bytes"
	"errors"
	"strings"
	"testing"
	"time"
)

func TestPrintOnboardingFollowUpCommands(t *testing.T) {
	var out bytes.Buffer
	printOnboardingFollowUpCommands(&out, "Hermes", "/tmp/backups/hermes.json")
	rendered := out.String()
	if !strings.Contains(rendered, "To change model/provider: preloop agents onboard Hermes --model <m>") {
		t.Fatalf("missing reconfigure line: %q", rendered)
	}
	if !strings.Contains(rendered, "To undo: preloop agents offboard Hermes (restores backup at /tmp/backups/hermes.json)") {
		t.Fatalf("missing undo line: %q", rendered)
	}
}

func TestPrintLiveValidationRoundTripResultSuccess(t *testing.T) {
	var out bytes.Buffer
	printLiveValidationRoundTripResult(
		&out,
		&managedLiveValidationOutcome{Passed: true, Attempted: true},
		nil,
		"openai/gpt-5.6-sol",
		1250*time.Millisecond,
	)
	rendered := out.String()
	if !strings.Contains(rendered, "✓ round-trip OK, model=openai/gpt-5.6-sol, latency=1.3s") &&
		!strings.Contains(rendered, "✓ round-trip OK, model=openai/gpt-5.6-sol, latency=1.2s") {
		t.Fatalf("unexpected success formatting: %q", rendered)
	}
}

func TestPrintLiveValidationRoundTripResultFailure(t *testing.T) {
	var out bytes.Buffer
	printLiveValidationRoundTripResult(
		&out,
		&managedLiveValidationOutcome{Passed: false, Attempted: true},
		errors.New("gateway returned 401"),
		"openai/gpt-5.6-sol",
		500*time.Millisecond,
	)
	rendered := out.String()
	if !strings.Contains(rendered, "✗ round-trip FAILED") {
		t.Fatalf("expected failure marker, got %q", rendered)
	}
	if !strings.Contains(rendered, "gateway returned 401") {
		t.Fatalf("expected error detail, got %q", rendered)
	}
	if !strings.Contains(rendered, "preloop agents validate") {
		t.Fatalf("expected actionable validate hint, got %q", rendered)
	}
}

func TestFormatDeferredLiveValidationRoundTrip(t *testing.T) {
	line := formatDeferredLiveValidationRoundTrip(deferredLiveValidationResult{
		Agent: AgentConfig{Name: "Hermes"},
		Outcome: &managedLiveValidationOutcome{
			Passed:    true,
			Attempted: true,
			ValidationResult: map[string]interface{}{
				"live_validation_model_alias": "openai/gpt-5.6-sol",
			},
		},
		Duration: 2 * time.Second,
	})
	if !strings.Contains(line, "round-trip OK") || !strings.Contains(line, "Hermes") {
		t.Fatalf("unexpected deferred success line: %q", line)
	}
}

func TestRunAgentsInstallRuntimeDryRunIncludesModel(t *testing.T) {
	cmd := agentsInstallRuntimeCmd
	t.Cleanup(func() {
		_ = cmd.Flags().Set("model", "")
		_ = cmd.Flags().Set("dry-run", "false")
		_ = cmd.Flags().Set("yes", "false")
	})
	if err := cmd.Flags().Set("dry-run", "true"); err != nil {
		t.Fatalf("set dry-run: %v", err)
	}
	if err := cmd.Flags().Set("yes", "true"); err != nil {
		t.Fatalf("set yes: %v", err)
	}
	if err := cmd.Flags().Set("model", "openai/gpt-5.6-sol"); err != nil {
		t.Fatalf("set model: %v", err)
	}
	// dry-run prints to stdout; ensure the flag path does not error.
	if err := runAgentsInstallRuntime(cmd, []string{"hermes"}); err != nil {
		t.Fatalf("dry run with --model failed: %v", err)
	}
}

func TestPrintLiveValidationRoundTripResultNilOutcomeAndErr(t *testing.T) {
	var out bytes.Buffer
	printLiveValidationRoundTripResult(&out, nil, nil, "openai/gpt-5.6-sol", 0)
	rendered := out.String()
	if strings.Contains(rendered, "FAILED") || strings.Contains(rendered, "OK") {
		t.Fatalf("nil/nil should finish the line quietly, got %q", rendered)
	}
}
