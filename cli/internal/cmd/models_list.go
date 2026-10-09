package cmd

import (
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/spf13/cobra"
)

type hostedModelInventory struct {
	Models []struct {
		ID                string `json:"id"`
		Name              string `json:"name"`
		Alias             string `json:"alias"`
		OwnAliasShadowing bool   `json:"own_alias_shadowing"`
	} `json:"models"`
	Allowance struct {
		Kind      string   `json:"kind"`
		Included  *float64 `json:"included_usd"`
		Spent     *float64 `json:"spent_usd"`
		Held      *float64 `json:"held_usd"`
		Remaining *float64 `json:"remaining_usd"`
		Reset     *string  `json:"reset_at"`
	} `json:"allowance"`
}

var modelsListCmd = &cobra.Command{
	Use: "list", Short: "List your models and entitled Preloop-hosted models", Args: cobra.NoArgs,
	RunE: func(cmd *cobra.Command, args []string) error {
		client, err := api.NewClient(FlagToken, FlagURL)
		if err != nil {
			return err
		}
		return executeModelsList(client, os.Stdout)
	},
}
var hostedAliasCheckCmd = &cobra.Command{
	Use: "check-hosted-alias ALIAS", Short: "Warn about account alias collisions before configuring a system hosted model (admin)", Args: cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		client, err := api.NewClient(FlagToken, FlagURL)
		if err != nil {
			return err
		}
		return executeHostedAliasCheck(client, os.Stdout, args[0])
	},
}

func init() { modelsCmd.AddCommand(modelsListCmd, hostedAliasCheckCmd) }
func moneyText(amount *float64) string {
	if amount == nil {
		return "not verified"
	}
	return fmt.Sprintf("$%.4f", *amount)
}
func executeModelsList(client *api.Client, w io.Writer) error {
	var own []struct {
		Name string `json:"name"`
		ID   string `json:"id"`
	}
	if err := client.Get("/api/v1/ai-models", &own); err != nil {
		return err
	}
	var features struct {
		Features map[string]any `json:"features"`
	}
	if err := client.Get("/api/v1/features", &features); err != nil {
		return err
	}
	if features.Features["hosted_models"] == true {
		var hosted hostedModelInventory
		if err := client.Get("/api/v1/account/hosted-models", &hosted); err != nil {
			return err
		}
		fmt.Fprintln(w, "Built-in (Preloop hosted) — operated by Preloop, metered against allowance:")
		for _, model := range hosted.Models {
			fmt.Fprintf(w, "  %s [hosted] %s (%s)\n", model.Name, model.Alias, model.ID)
			if model.OwnAliasShadowing {
				fmt.Fprintln(w, "    Warning: your own model uses this alias and takes precedence.")
			}
		}
		fmt.Fprintf(w, "  Included: %s; spent: %s; held (open reservations): %s; remaining: %s\n", moneyText(hosted.Allowance.Included), moneyText(hosted.Allowance.Spent), moneyText(hosted.Allowance.Held), moneyText(hosted.Allowance.Remaining))
		if hosted.Allowance.Kind == "one_time" {
			fmt.Fprintln(w, "  One-time credit does not reset.")
		} else if hosted.Allowance.Reset != nil {
			fmt.Fprintln(w, "  Resets:", *hosted.Allowance.Reset)
		} else {
			fmt.Fprintln(w, "  Monthly reset date is not yet verified.")
		}
	}
	fmt.Fprintln(w, "Your models — billed to your provider key:")
	for _, model := range own {
		fmt.Fprintf(w, "  %s [your key] (%s)\n", model.Name, model.ID)
	}
	return nil
}
func executeHostedAliasCheck(client *api.Client, w io.Writer, alias string) error {
	var result struct {
		Warning      *string `json:"warning"`
		ModelCount   int     `json:"model_count"`
		AccountCount int     `json:"account_count"`
	}
	if err := client.Get("/api/v1/admin/hosted-models/alias-check?alias="+url.QueryEscape(alias), &result); err != nil {
		if api.IsStatus(err, http.StatusNotFound) {
			return fmt.Errorf("hosted alias checking is unavailable on this server")
		}
		return err
	}
	if result.Warning != nil {
		fmt.Fprintf(w, "Warning: %s (%d models in %d accounts)\n", *result.Warning, result.ModelCount, result.AccountCount)
	} else {
		fmt.Fprintln(w, "No account-owned model currently uses this alias.")
	}
	return nil
}
