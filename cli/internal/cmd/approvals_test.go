package cmd

import "testing"

// The approvals table previously rendered fields the backend response never
// contained (approvers/active/auto_approve), so every workflow displayed as
// inactive with no approvers. approverSummary reads the real
// approver_user_ids / approver_team_ids fields.
func TestApprovalWorkflowApproverSummary(t *testing.T) {
	cases := []struct {
		name     string
		workflow ApprovalWorkflow
		expected string
	}{
		{"no approvers", ApprovalWorkflow{}, "none"},
		{
			"single user",
			ApprovalWorkflow{ApproverUserIDs: []string{"u1"}},
			"1 user",
		},
		{
			"multiple users",
			ApprovalWorkflow{ApproverUserIDs: []string{"u1", "u2"}},
			"2 users",
		},
		{
			"users and teams",
			ApprovalWorkflow{
				ApproverUserIDs: []string{"u1"},
				ApproverTeamIDs: []string{"t1", "t2"},
			},
			"1 user, 2 teams",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := tc.workflow.approverSummary(); got != tc.expected {
				t.Fatalf("expected %q, got %q", tc.expected, got)
			}
		})
	}
}
