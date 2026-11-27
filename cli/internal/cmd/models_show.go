package cmd

import (
	"fmt"
	"io"
	"net/url"
	"os"
	"sort"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/spf13/cobra"
)

type executorEligibility struct {
	Eligible bool   `json:"eligible"`
	Reason   string `json:"reason"`
	Message  string `json:"message"`
}

type modelEligibilityRead struct {
	ID                  string                         `json:"id"`
	Name                string                         `json:"name"`
	ExecutorEligibility map[string]executorEligibility `json:"executor_eligibility"`
}

var modelsShowCmd = &cobra.Command{
	Use:   "show MODEL_ID",
	Short: "Show a model's executor eligibility for your identity",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		client, err := api.NewClient(FlagToken, FlagURL)
		if err != nil {
			return err
		}
		return executeModelsShow(client, os.Stdout, args[0])
	},
}

func init() { modelsCmd.AddCommand(modelsShowCmd) }

func executeModelsShow(client *api.Client, w io.Writer, id string) error {
	var model modelEligibilityRead
	if err := client.Get(api.RESTPrefix+"/ai-models/"+url.PathEscape(id), &model); err != nil {
		return err
	}
	fmt.Fprintf(w, "%s (%s)\n", model.Name, model.ID)
	printExecutorEligibility(w, model.ExecutorEligibility)
	return nil
}

func printExecutorEligibility(w io.Writer, entries map[string]executorEligibility) {
	if len(entries) == 0 {
		fmt.Fprintln(w, "    Executor eligibility: not reported by this server.")
		return
	}
	keys := make([]string, 0, len(entries))
	for key := range entries {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		entry := entries[key]
		state := "eligible"
		if !entry.Eligible {
			state = "not eligible"
		}
		fmt.Fprintf(w, "    %s: %s", key, state)
		if entry.Reason != "" {
			fmt.Fprintf(w, " (%s)", entry.Reason)
		}
		fmt.Fprintln(w)
		if entry.Message != "" {
			fmt.Fprintf(w, "      %s\n", entry.Message)
		}
	}
}
