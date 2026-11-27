package cmd

// The review step of onboarding: the point where a person accepts, changes
// or cancels the plan.

import (
	"bufio"
	"fmt"
	"io"
)

// legacyReviewer asks the confirmation onboarding has always asked, once per
// agent, reading the same reader as the prompts before it.
type legacyReviewer struct {
	input  *bufio.Reader
	output io.Writer
}

// confirmApply reports whether the plan in state should be applied. It does
// not ask when the caller already confirmed (discover's batch, --yes),
// except that moving a subscription login always needs its own answer after
// the custody disclosure.
func (r legacyReviewer) confirmApply(state *enrollmentPlanState) (bool, error) {
	opts := state.opts
	requiresCustodyConfirmation := false
	for _, item := range state.custody {
		if item.SubscriptionTransfer {
			requiresCustodyConfirmation = true
		}
	}
	if (opts.SkipConfirmation && !requiresCustodyConfirmation) || opts.AutoApprove {
		return true, nil
	}
	confirmed, err := confirmActionDefaultYes(
		r.input,
		r.output,
		fmt.Sprintf(
			"Apply managed Preloop onboarding for %s? (Y/n): ",
			resolveAgentDisplayName(state.agent),
		),
	)
	if err != nil {
		return false, fmt.Errorf("failed to read confirmation: %w", err)
	}
	return confirmed, nil
}
