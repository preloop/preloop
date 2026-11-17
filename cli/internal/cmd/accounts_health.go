package cmd

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

// accountHealth is GET /api/v1/operator/accounts/{id}/health.
type accountHealth struct {
	AccountID        string `json:"account_id"`
	Since            string `json:"since"`
	Until            string `json:"until"`
	FailedExecutions []struct {
		Category         string  `json:"category"`
		Count            int     `json:"count"`
		FirstExecutionID *string `json:"first_execution_id"`
	} `json:"failed_executions"`
	HollowSuccesses struct {
		ZeroToolCalls struct {
			Count int `json:"count"`
		} `json:"zero_tool_calls"`
		ZeroModelRequests struct {
			Count int `json:"count"`
		} `json:"zero_model_requests"`
	} `json:"hollow_successes"`
	RouteProblems []struct {
		Method        string `json:"method"`
		RouteTemplate string `json:"route_template"`
		StatusClass   int    `json:"status_class"`
		Count         int    `json:"count"`
	} `json:"route_problems"`
	EmptyAfterCreate []struct {
		Method        string `json:"method"`
		RouteTemplate string `json:"route_template"`
		Count         int    `json:"count"`
	} `json:"empty_after_create"`
	GatewayUpstreamErrors []struct {
		Provider   string `json:"provider"`
		ErrorClass string `json:"error_class"`
		Count      int    `json:"count"`
	} `json:"gateway_upstream_errors"`
}

var (
	accountsHealthSince time.Duration
	accountsHealthJSON  bool
)

var accountsHealthCmd = &cobra.Command{
	Use:   "health <account-id>",
	Short: "Show one account's failures and anomalies (platform operators)",
	Long: `Show what went wrong for one account in a recent window: failed flow
executions by category, successful executions with zero tool calls or zero
model requests, API routes with repeated 4xx or 5xx, list responses that
came back empty right after a create, and gateway upstream errors per
provider.

Platform operators only (installation superusers). Returns counts, ids and
route templates, never customer content.`,
	Args: cobra.ExactArgs(1),
	RunE: runAccountsHealth,
}

func init() {
	accountsHealthCmd.Flags().DurationVar(&accountsHealthSince, "since", 24*time.Hour, "Window length ending now (at most 744h)")
	accountsHealthCmd.Flags().BoolVar(&accountsHealthJSON, "json", false, "Print the raw JSON response")
	accountsCmd.AddCommand(accountsHealthCmd)
}

func runAccountsHealth(cmd *cobra.Command, args []string) error {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}
	accountID := strings.TrimSpace(args[0])
	query := url.Values{}
	query.Set("since", time.Now().UTC().Add(-accountsHealthSince).Format(time.RFC3339))
	path := api.RESTPrefix + "/operator/accounts/" + url.PathEscape(accountID) + "/health?" + query.Encode()
	var raw json.RawMessage
	if err := client.Get(path, &raw); err != nil {
		if api.IsStatus(err, http.StatusForbidden) {
			return fmt.Errorf("account health is for platform operators only")
		}
		if api.IsStatus(err, http.StatusNotFound) {
			return fmt.Errorf("account %s not found", accountID)
		}
		return err
	}
	out := cmd.OutOrStdout()
	if accountsHealthJSON {
		_, err := fmt.Fprintln(out, string(raw))
		return err
	}
	var report accountHealth
	if err := json.Unmarshal(raw, &report); err != nil {
		return fmt.Errorf("unexpected account health response: %w", err)
	}
	return printAccountHealth(out, report)
}

func printAccountHealth(out interface{ Write([]byte) (int, error) }, r accountHealth) error {
	w := tabwriter.NewWriter(out, 0, 0, 2, ' ', 0)
	fmt.Fprintf(w, "Account %s, %s to %s\n\n", r.AccountID, r.Since, r.Until)
	fmt.Fprintln(w, "Failed executions\tCOUNT\tFIRST EXECUTION")
	for _, f := range r.FailedExecutions {
		first := ""
		if f.FirstExecutionID != nil {
			first = *f.FirstExecutionID
		}
		fmt.Fprintf(w, "  %s\t%d\t%s\n", f.Category, f.Count, first)
	}
	fmt.Fprintf(w, "\nSucceeded with zero tool calls\t%d\n", r.HollowSuccesses.ZeroToolCalls.Count)
	fmt.Fprintf(w, "Succeeded with zero model requests\t%d\n", r.HollowSuccesses.ZeroModelRequests.Count)
	fmt.Fprintln(w, "\nRoutes with repeated errors\tCLASS\tCOUNT")
	for _, p := range r.RouteProblems {
		fmt.Fprintf(w, "  %s %s\t%dxx\t%d\n", p.Method, p.RouteTemplate, p.StatusClass, p.Count)
	}
	fmt.Fprintln(w, "\nEmpty right after a create\t\tCOUNT")
	for _, e := range r.EmptyAfterCreate {
		fmt.Fprintf(w, "  %s %s\t\t%d\n", e.Method, e.RouteTemplate, e.Count)
	}
	fmt.Fprintln(w, "\nGateway upstream errors\tCLASS\tCOUNT")
	for _, g := range r.GatewayUpstreamErrors {
		fmt.Fprintf(w, "  %s\t%s\t%d\n", g.Provider, g.ErrorClass, g.Count)
	}
	return w.Flush()
}
