package cmd

// `preloop models flow-use <model-id> [on|off]` shows or sets the owner
// opt-in that lets the subscription owner's own hosted flows use a
// subscription-OAuth model (Codex ChatGPT sign-in, Claude subscription).
// Only the subscription's owner can change it; the server refuses others.

import (
	"fmt"
	"io"
	"net/url"
	"os"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/spf13/cobra"
)

var modelsFlowUseCmd = &cobra.Command{
	Use:   "flow-use <model-id> [on|off]",
	Short: "Let flows you own use your subscription model",
	Long: `Show or set whether hosted runs of flows you own may use this subscription model.

Subscription models (Codex with ChatGPT sign-in, Claude subscriptions) serve
only the agent you onboarded them with. "on" also lets flows you created run
on the subscription from hosted executions, through the gateway; they use the
subscription's quota. Other users' flows and other agents are still refused.
Only the subscription's owner can change this. Without on|off the current
state is printed.

Examples:
  preloop models flow-use 3f0c1d9e-0000-0000-0000-000000000000
  preloop models flow-use 3f0c1d9e-0000-0000-0000-000000000000 on`,
	Args: cobra.RangeArgs(1, 2),
	RunE: func(cmd *cobra.Command, args []string) error {
		client, err := api.NewClient(FlagToken, FlagURL)
		if err != nil {
			return fmt.Errorf("failed to create API client: %w", err)
		}
		if !client.IsAuthenticated() {
			return fmt.Errorf("not authenticated - run 'preloop login' first")
		}
		mode := ""
		if len(args) == 2 {
			mode = args[1]
		}
		return executeModelsFlowUse(client, os.Stdout, args[0], mode)
	},
}

func init() {
	modelsCmd.AddCommand(modelsFlowUseCmd)
}

// modelsOwnerFlowUse mirrors the backend AIModelOwnerFlowUseRead.
type modelsOwnerFlowUse struct {
	Applicable bool `json:"applicable"`
	Enabled    bool `json:"enabled"`
	CanManage  bool `json:"can_manage"`
	Usage      *struct {
		RequestCount   int     `json:"request_count"`
		ExecutionCount int     `json:"execution_count"`
		TotalTokens    int     `json:"total_tokens"`
		APIEquivalent  float64 `json:"api_equivalent_cost"`
	} `json:"usage"`
}

// executeModelsFlowUse reads or updates the opt-in and prints the result.
func executeModelsFlowUse(client *api.Client, out io.Writer, modelID, mode string) error {
	path := api.RESTPrefix + "/ai-models/" + url.PathEscape(strings.TrimSpace(modelID)) + "/owner-flow-use"
	var result modelsOwnerFlowUse
	switch strings.ToLower(strings.TrimSpace(mode)) {
	case "":
		if err := client.Get(path, &result); err != nil {
			return fmt.Errorf("read flow use: %w", err)
		}
	case "on", "true":
		if err := client.Put(path, map[string]bool{"enabled": true}, &result); err != nil {
			return fmt.Errorf("update flow use: %w", err)
		}
	case "off", "false":
		if err := client.Put(path, map[string]bool{"enabled": false}, &result); err != nil {
			return fmt.Errorf("update flow use: %w", err)
		}
	default:
		return fmt.Errorf("mode must be on or off (got %q)", mode)
	}
	switch {
	case !result.Applicable:
		fmt.Fprintln(out, "Not a subscription model: flows can use it without this setting.") //nolint:errcheck
	case result.Enabled:
		fmt.Fprintln(out, "On: hosted runs of flows the subscription owner created may use this subscription.") //nolint:errcheck
	default:
		fmt.Fprintln(out, "Off: only the onboarded agent uses this subscription.") //nolint:errcheck
	}
	if result.Applicable && !result.CanManage {
		fmt.Fprintln(out, "Only the subscription's owner can change this.") //nolint:errcheck
	}
	if result.Usage != nil && result.Enabled {
		fmt.Fprintf( //nolint:errcheck
			out,
			"Last 30 days: %d request(s) across %d flow run(s), %d tokens, $%.2f est. at API prices (covered by the subscription).\n",
			result.Usage.RequestCount, result.Usage.ExecutionCount,
			result.Usage.TotalTokens, result.Usage.APIEquivalent,
		)
	}
	return nil
}
