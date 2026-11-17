package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/spf13/cobra"
)

func newModelsRoutingCmd() *cobra.Command {
	var only, ignore []string
	var deny, zdr, fallbacks, clear bool
	command := &cobra.Command{
		Use:   "routing <model-id>",
		Short: "View or change an OpenRouter model's provider routing",
		Long: `View or change the provider routing saved on an OpenRouter model.
Saved routing wins over client request preferences. OpenRouter account settings also apply.
Omitted flags preserve existing settings. --clear removes the saved routing object.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			changes := map[string]interface{}{}
			if cmd.Flags().Changed("only") {
				changes["only"] = only
			}
			if cmd.Flags().Changed("ignore") {
				changes["ignore"] = ignore
			}
			if cmd.Flags().Changed("deny-data-collection") {
				changes["data_collection"] = "allow"
				if deny {
					changes["data_collection"] = "deny"
				}
			}
			if cmd.Flags().Changed("zdr") {
				changes["zdr"] = zdr
			}
			if cmd.Flags().Changed("allow-fallbacks") {
				changes["allow_fallbacks"] = fallbacks
			}
			if clear && len(changes) > 0 {
				return fmt.Errorf("--clear cannot be combined with routing flags")
			}
			client, err := api.NewClient(FlagToken, FlagURL)
			if err != nil {
				return err
			}
			if !client.IsAuthenticated() {
				return fmt.Errorf("not authenticated - run 'preloop login' first")
			}
			return executeModelsRouting(client, os.Stdout, args[0], changes, clear)
		},
	}
	command.Flags().StringSliceVar(&only, "only", nil, "Only these provider slugs (comma-separated)")
	command.Flags().StringSliceVar(&ignore, "ignore", nil, "Never these provider slugs (comma-separated)")
	command.Flags().BoolVar(&deny, "deny-data-collection", false, "Deny providers that collect data")
	command.Flags().BoolVar(&zdr, "zdr", false, "Require OpenRouter zero data retention routing")
	command.Flags().BoolVar(&fallbacks, "allow-fallbacks", true, "Allow fallback hosts within the routing restrictions")
	command.Flags().BoolVar(&clear, "clear", false, "Remove saved provider routing")
	return command
}

func init() { modelsCmd.AddCommand(newModelsRoutingCmd()) }

// executeModelsRouting preserves unrelated model parameters and advanced routing fields.
func executeModelsRouting(client *api.Client, out io.Writer, modelID string, changes map[string]interface{}, clear bool) error {
	path := api.RESTPrefix + "/ai-models/" + url.PathEscape(strings.TrimSpace(modelID))
	var model struct {
		Provider   string                 `json:"provider_name"`
		Endpoint   string                 `json:"api_endpoint"`
		Parameters map[string]interface{} `json:"model_parameters"`
	}
	if err := client.Get(path, &model); err != nil {
		return fmt.Errorf("read model: %w", err)
	}
	endpoint, _ := url.Parse(model.Endpoint)
	host := ""
	if endpoint != nil {
		host = strings.ToLower(endpoint.Hostname())
	}
	if !strings.EqualFold(model.Provider, "openrouter") && host != "openrouter.ai" && !strings.HasSuffix(host, ".openrouter.ai") {
		return fmt.Errorf("provider routing requires an OpenRouter model")
	}
	if model.Parameters == nil {
		model.Parameters = map[string]interface{}{}
	}
	routing, _ := model.Parameters["provider"].(map[string]interface{})
	if routing == nil {
		routing = map[string]interface{}{}
	}
	if len(changes) == 0 && !clear {
		return json.NewEncoder(out).Encode(routing)
	}
	for key, value := range changes {
		if values, ok := value.([]string); ok && len(values) == 0 {
			delete(routing, key)
			continue
		}
		routing[key] = value
	}
	model.Parameters["provider"] = routing
	if clear {
		delete(model.Parameters, "provider")
	}
	var result struct {
		Name string `json:"name"`
	}
	if err := client.Put(path, map[string]interface{}{"model_parameters": model.Parameters}, &result); err != nil {
		return fmt.Errorf("update routing: %w", err)
	}
	_, err := fmt.Fprintf(out, "%s: provider routing saved\n", result.Name)
	return err
}
