package cmd

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/spf13/cobra"
)

func init() {
	flowCmd.AddCommand(newFlowWebhookSecretCmd())
}

func newFlowWebhookSecretCmd() *cobra.Command {
	group := &cobra.Command{
		Use:   "webhook-secret",
		Short: "Manage a flow's webhook ingress secret",
	}
	var asJSON bool
	rotate := &cobra.Command{
		Use:   "rotate <flow-id-or-name>",
		Short: "Replace the webhook secret and show the new URL once",
		Long: `Replace a flow's webhook ingress secret. The old URL stops working
immediately. Save the new URL and update every webhook sender. Flow reads
only return secret status and a hint; they cannot recover the secret.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			client, err := api.NewClient(FlagToken, FlagURL)
			if err != nil {
				return err
			}
			flowID, err := resolveFlowID(client, args[0])
			if err != nil {
				return err
			}
			var response struct {
				Secret    string `json:"webhook_secret"`
				Hint      string `json:"webhook_secret_hint"`
				RotatedAt string `json:"webhook_secret_rotated_at"`
				Set       bool   `json:"webhook_secret_set"`
			}
			if err := client.Post(flowsPath+"/"+flowID+"/webhook-secret/rotate", nil, &response); err != nil {
				if api.IsStatus(err, http.StatusNotFound) || api.IsStatus(err, http.StatusMethodNotAllowed) {
					return fmt.Errorf("this server does not support webhook secret rotation, or the flow no longer exists; check the flow and upgrade the server if needed")
				}
				return fmt.Errorf("failed to rotate webhook secret: %w", err)
			}
			if response.Secret == "" {
				return fmt.Errorf("rotation returned no webhook secret")
			}
			if asJSON {
				return json.NewEncoder(cmd.OutOrStdout()).Encode(response)
			}
			cmd.PrintErrln("New webhook URL (shown once). Save it and update every webhook sender:")
			cmd.Println(strings.TrimRight(client.BaseURL(), "/") + api.RESTPrefix + "/webhooks/flows/" + flowID + "/" + response.Secret)
			return nil
		},
	}
	rotate.Flags().BoolVar(&asJSON, "json", false, "print the rotation response as JSON")
	group.AddCommand(rotate)
	return group
}
