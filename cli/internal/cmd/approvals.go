package cmd

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"

	"github.com/preloop/preloop/cli/internal/api"
)

const (
	approvalPoliciesPath = "/api/v1/approval-workflows"
	approvalRequestsPath = "/api/v1/approval-requests"
)

// ApprovalWorkflow represents an approval workflow.
type ApprovalWorkflow struct {
	ID                   string   `json:"id" yaml:"id"`
	Name                 string   `json:"name" yaml:"name"`
	Description          string   `json:"description,omitempty" yaml:"description,omitempty"`
	ApprovalType         string   `json:"approval_type" yaml:"approval_type"`
	ApprovalMode         string   `json:"approval_mode" yaml:"approval_mode"`
	IsDefault            bool     `json:"is_default" yaml:"is_default"`
	TimeoutSeconds       int      `json:"timeout_seconds" yaml:"timeout_seconds"`
	AsyncApprovalEnabled bool     `json:"async_approval_enabled" yaml:"async_approval_enabled"`
	ApprovalsRequired    int      `json:"approvals_required" yaml:"approvals_required"`
	ApproverUserIDs      []string `json:"approver_user_ids,omitempty" yaml:"approver_user_ids,omitempty"`
	ApproverTeamIDs      []string `json:"approver_team_ids,omitempty" yaml:"approver_team_ids,omitempty"`
}

// approverSummary renders a compact human-readable count of the workflow's
// approvers, e.g. "2 users", "1 user, 1 team", or "none".
func (p ApprovalWorkflow) approverSummary() string {
	parts := make([]string, 0, 2)
	if n := len(p.ApproverUserIDs); n == 1 {
		parts = append(parts, "1 user")
	} else if n > 1 {
		parts = append(parts, fmt.Sprintf("%d users", n))
	}
	if n := len(p.ApproverTeamIDs); n == 1 {
		parts = append(parts, "1 team")
	} else if n > 1 {
		parts = append(parts, fmt.Sprintf("%d teams", n))
	}
	if len(parts) == 0 {
		return "none"
	}
	return strings.Join(parts, ", ")
}

// ApprovalRequest represents an approval request.
type ApprovalRequest struct {
	ID          string    `json:"id" yaml:"id"`
	ToolName    string    `json:"tool_name" yaml:"tool_name"`
	ToolInput   string    `json:"tool_input,omitempty" yaml:"tool_input,omitempty"`
	Status      string    `json:"status" yaml:"status"`
	RequestedBy string    `json:"requested_by,omitempty" yaml:"requested_by,omitempty"`
	RequestedAt api.Time  `json:"requested_at" yaml:"requested_at"`
	ResolvedBy  string    `json:"resolved_by,omitempty" yaml:"resolved_by,omitempty"`
	ResolvedAt  api.Time  `json:"resolved_at,omitempty" yaml:"resolved_at,omitempty"`
	Reason      string    `json:"reason,omitempty" yaml:"reason,omitempty"`
	SessionID   string    `json:"session_id,omitempty" yaml:"session_id,omitempty"`
}

// approvalsCmd represents the approvals command group.
var approvalsCmd = &cobra.Command{
	Use:   "approvals",
	Short: "Manage approval requests",
	Long:  `Manage AI agent approval requests for tool executions.`,
}

// approvalsListCmd represents the approvals list command.
var approvalsListCmd = &cobra.Command{
	Use:   "list",
	Short: "List approval workflows",
	Long: `List all approval workflows configured for your organization.

Examples:
  preloop approvals list
  preloop approvals list --format json`,
	RunE: runApprovalsList,
}

// approvalsPendingCmd represents the approvals pending command.
var approvalsPendingCmd = &cobra.Command{
	Use:   "pending",
	Short: "List pending approval requests",
	Long: `List all pending approval requests that need action.

Examples:
  preloop approvals pending
  preloop approvals pending --limit 50`,
	RunE: runApprovalsPending,
}

// approvalsApproveCmd represents the approvals approve command.
var approvalsApproveCmd = &cobra.Command{
	Use:   "approve <request-id>",
	Short: "Approve a request",
	Long: `Approve an approval request, allowing the action to proceed.

Examples:
  preloop approvals approve abc123
  preloop approvals approve abc123 --reason "Looks good"`,
	Args: cobra.ExactArgs(1),
	RunE: runApprovalsApprove,
}

// approvalsDenyCmd represents the approvals deny command.
var approvalsDenyCmd = &cobra.Command{
	Use:   "deny <request-id>",
	Short: "Deny a request",
	Long: `Deny an approval request, blocking the action.

Examples:
  preloop approvals deny abc123
  preloop approvals deny abc123 --reason "Security concern"`,
	Args: cobra.ExactArgs(1),
	RunE: runApprovalsDeny,
}

func init() {
	// Add subcommands
	approvalsCmd.AddCommand(approvalsListCmd)
	approvalsCmd.AddCommand(approvalsPendingCmd)
	approvalsCmd.AddCommand(approvalsApproveCmd)
	approvalsCmd.AddCommand(approvalsDenyCmd)

	// Flags for list
	approvalsListCmd.Flags().StringP("format", "f", "table", "output format (table, json, yaml)")

	// Flags for pending
	approvalsPendingCmd.Flags().IntP("limit", "l", 20, "maximum number of results")
	approvalsPendingCmd.Flags().StringP("format", "f", "table", "output format (table, json, yaml)")

	// Flags for approve
	approvalsApproveCmd.Flags().StringP("reason", "r", "", "reason for approval")

	// Flags for deny
	approvalsDenyCmd.Flags().StringP("reason", "r", "", "reason for denial")
}

// runApprovalsList lists all approval workflows.
func runApprovalsList(cmd *cobra.Command, args []string) error {
	format, _ := cmd.Flags().GetString("format")

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}

	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	var policies []ApprovalWorkflow
	if err := client.Get(approvalPoliciesPath, &policies); err != nil {
		return fmt.Errorf("failed to list approval workflows: %w", err)
	}

	if len(policies) == 0 {
		fmt.Println("No approval workflows configured")
		return nil
	}

	switch strings.ToLower(format) {
	case "json":
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		return enc.Encode(policies)

	case "yaml":
		enc := yaml.NewEncoder(os.Stdout)
		return enc.Encode(policies)

	default: // table
		w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
		fmt.Fprintln(w, "NAME\tTYPE\tMODE\tDEFAULT\tAPPROVERS\tTIMEOUT") //nolint:errcheck
		for _, p := range policies {
			isDefault := "no"
			if p.IsDefault {
				isDefault = "yes"
			}
			timeout := "-"
			if p.TimeoutSeconds > 0 {
				timeout = fmt.Sprintf("%ds", p.TimeoutSeconds)
			}
			fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\t%s\n", //nolint:errcheck
				p.Name, p.ApprovalType, p.ApprovalMode, isDefault, p.approverSummary(), timeout)
		}
		return w.Flush()
	}
}

// runApprovalsPending lists pending approval requests.
func runApprovalsPending(cmd *cobra.Command, args []string) error {
	limit, _ := cmd.Flags().GetInt("limit")
	format, _ := cmd.Flags().GetString("format")

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}

	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	path := fmt.Sprintf("%s?status=pending&limit=%d", approvalRequestsPath, limit)

	var requests []ApprovalRequest
	if err := client.Get(path, &requests); err != nil {
		return fmt.Errorf("failed to list pending requests: %w", err)
	}

	if len(requests) == 0 {
		fmt.Println("No pending approval requests")
		return nil
	}

	switch strings.ToLower(format) {
	case "json":
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		return enc.Encode(requests)

	case "yaml":
		enc := yaml.NewEncoder(os.Stdout)
		return enc.Encode(requests)

	default: // table
		w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
		fmt.Fprintln(w, "ID\tTOOL\tREQUESTED BY\tAGE\tINPUT") //nolint:errcheck
		for _, r := range requests {
			age := formatDuration(time.Since(r.RequestedAt.Time))
			input := r.ToolInput
			if len(input) > 40 {
				input = input[:37] + "..."
			}
			// Truncate ID for display
			id := r.ID
			if len(id) > 12 {
				id = id[:12]
			}
			fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\n", //nolint:errcheck
				id, r.ToolName, r.RequestedBy, age, input)
		}
		return w.Flush()
	}
}

// runApprovalsApprove approves a request.
func runApprovalsApprove(cmd *cobra.Command, args []string) error {
	requestID := args[0]
	reason, _ := cmd.Flags().GetString("reason")

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}

	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	path := fmt.Sprintf("%s/%s/approve", approvalRequestsPath, requestID)

	request := map[string]string{}
	if reason != "" {
		request["reason"] = reason
	}

	var result ApprovalRequest
	if err := client.Post(path, request, &result); err != nil {
		return fmt.Errorf("failed to approve request: %w", err)
	}

	fmt.Printf("✓ Request '%s' approved\n", requestID)
	fmt.Printf("  Tool: %s\n", result.ToolName)
	if reason != "" {
		fmt.Printf("  Reason: %s\n", reason)
	}

	return nil
}

// runApprovalsDeny denies a request.
func runApprovalsDeny(cmd *cobra.Command, args []string) error {
	requestID := args[0]
	reason, _ := cmd.Flags().GetString("reason")

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}

	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	path := fmt.Sprintf("%s/%s/decline", approvalRequestsPath, requestID)

	request := map[string]string{}
	if reason != "" {
		request["reason"] = reason
	}

	var result ApprovalRequest
	if err := client.Post(path, request, &result); err != nil {
		return fmt.Errorf("failed to deny request: %w", err)
	}

	fmt.Printf("✗ Request '%s' denied\n", requestID)
	fmt.Printf("  Tool: %s\n", result.ToolName)
	if reason != "" {
		fmt.Printf("  Reason: %s\n", reason)
	}

	return nil
}

// formatDuration formats a duration in a human-readable way.
func formatDuration(d time.Duration) string {
	if d < time.Minute {
		return fmt.Sprintf("%ds", int(d.Seconds()))
	}
	if d < time.Hour {
		return fmt.Sprintf("%dm", int(d.Minutes()))
	}
	if d < 24*time.Hour {
		return fmt.Sprintf("%dh", int(d.Hours()))
	}
	days := int(d.Hours() / 24)
	return fmt.Sprintf("%dd", days)
}
