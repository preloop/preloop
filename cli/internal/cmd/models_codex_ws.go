package cmd

// `preloop models codex-ws <model-id> on|off|inherit` sets the per-model
// override for the Codex upstream WebSocket transport (#1524). The server
// merges the flag into the model's meta_data, so other settings are kept.

import (
	"fmt"
	"io"
	"net/url"
	"os"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/spf13/cobra"
)

var modelsCodexWSCmd = &cobra.Command{
	Use:   "codex-ws <model-id> on|off|inherit",
	Short: "Set the Codex upstream WebSocket override for one model",
	Long: `Turn the Codex upstream WebSocket transport on or off for one openai-codex model.

"on" or "off" wins over the account override and the server-wide
CODEX_UPSTREAM_WEBSOCKET setting for this model only. "inherit" removes the
model override, so the account override or the global setting applies again.
Usage rows record which one applied in meta_data.ws_override_source.

Examples:
  preloop models codex-ws 3f0c1d9e-0000-0000-0000-000000000000 on
  preloop models codex-ws 3f0c1d9e-0000-0000-0000-000000000000 inherit`,
	Args: cobra.ExactArgs(2),
	RunE: func(cmd *cobra.Command, args []string) error {
		client, err := api.NewClient(FlagToken, FlagURL)
		if err != nil {
			return fmt.Errorf("failed to create API client: %w", err)
		}
		if !client.IsAuthenticated() {
			return fmt.Errorf("not authenticated - run 'preloop login' first")
		}
		return executeModelsCodexWS(client, os.Stdout, args[0], args[1])
	},
}

func init() {
	modelsCmd.AddCommand(modelsCodexWSCmd)
}

// executeModelsCodexWS validates the mode and sends the update.
func executeModelsCodexWS(client *api.Client, out io.Writer, modelID, mode string) error {
	var value interface{}
	switch strings.ToLower(strings.TrimSpace(mode)) {
	case "on", "true":
		value = true
	case "off", "false":
		value = false
	case "inherit", "default", "unset":
		value = nil
	default:
		return fmt.Errorf("mode must be on, off or inherit (got %q)", mode)
	}
	body := map[string]interface{}{"codex_upstream_websocket": value}
	var result struct {
		ID       string                 `json:"id"`
		Name     string                 `json:"name"`
		MetaData map[string]interface{} `json:"meta_data"`
	}
	path := api.RESTPrefix + "/ai-models/" + url.PathEscape(strings.TrimSpace(modelID))
	if err := client.Put(path, body, &result); err != nil {
		return fmt.Errorf("update model: %w", err)
	}
	stored, ok := result.MetaData["codex_upstream_websocket"].(bool)
	switch {
	case !ok:
		fmt.Fprintf(out, "%s: Codex upstream WebSocket inherits the account/global setting\n", result.Name)
	case stored:
		fmt.Fprintf(out, "%s: Codex upstream WebSocket on (model override)\n", result.Name)
	default:
		fmt.Fprintf(out, "%s: Codex upstream WebSocket off (model override)\n", result.Name)
	}
	return nil
}
