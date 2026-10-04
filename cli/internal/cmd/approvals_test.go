package cmd

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/testenv"
)

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

// approve and deny used to post {"reason": ...} with no "approved", which the
// server refused with 422 and whose reason it dropped. They now post the
// reason as "comment", which is what the backend stores.
func TestApprovalsApproveDenySendCommentNotReason(t *testing.T) {
	testenv.SetTempHome(t)
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	for _, tc := range []struct {
		name string
		run  func(*cobra.Command, []string) error
		path string
	}{
		{"approve", runApprovalsApprove, "/api/v1/approval-requests/req-1/approve"},
		{"deny", runApprovalsDeny, "/api/v1/approval-requests/req-1/decline"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var gotPath string
			var gotBody map[string]any
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				gotPath = r.URL.Path
				_ = json.NewDecoder(r.Body).Decode(&gotBody)
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write([]byte(`{"id":"req-1","tool_name":"deploy"}`))
			}))
			defer server.Close()
			FlagToken, FlagURL = "test-token", server.URL
			t.Cleanup(func() { FlagToken, FlagURL = "", "" })

			cmd := &cobra.Command{}
			cmd.Flags().String("reason", "", "")
			_ = cmd.Flags().Set("reason", "checked by hand")
			if err := tc.run(cmd, []string{"req-1"}); err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if gotPath != tc.path {
				t.Fatalf("path = %q, want %q", gotPath, tc.path)
			}
			if gotBody["comment"] != "checked by hand" {
				t.Fatalf("body = %v, want comment", gotBody)
			}
			if _, ok := gotBody["reason"]; ok {
				t.Fatalf("body still carries reason: %v", gotBody)
			}
		})
	}
}
