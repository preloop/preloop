package cmd

import (
	"strings"
	"testing"
)

// custodyAndPlanText cuts the custody and plan blocks out of an onboarding
// transcript: from the custody header to the line before the confirmation.
func custodyAndPlanText(t *testing.T, output string) string {
	t.Helper()
	start := strings.Index(output, "  Credential custody (metadata only):")
	end := strings.Index(output, "Apply managed Preloop onboarding for")
	if start < 0 || end <= start {
		t.Fatalf("custody and plan blocks not found:\n%s", output)
	}
	return output[start:end]
}

// protects=the full custody and plan wording a person can still read with
// --explain; fails_when=any byte of that text differs from what the CLI
// printed before the redesign (recorded from main at 7fb7bafd);
// no_claim=does not cover other agent kinds, or text outside those two
// blocks; a fake API and a synthetic login stand in for the account and the
// Claude subscription.
func TestExplainKeepsCustodyAndPlanTextByteIdentical(t *testing.T) {
	f := newClaudeSubscriptionFixture(t)
	setStdinTerminal(t, true)
	output, err := runOnboardFixture(t, f, managedEnrollmentOptions{Explain: true}, "\n\nn\n")
	if err != nil {
		t.Fatalf("declined onboarding should not fail: %v\n%s", err, output)
	}
	assertRecordedGolden(t, "explain_claude_subscription.golden", f.normalize(custodyAndPlanText(t, output)))
}
