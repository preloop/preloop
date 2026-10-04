package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/spf13/cobra"
)

var ciIDPattern = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

// CI responses are projected through an explicit metadata vocabulary. New
// server fields never implicitly reach stdout, including on issuance.
var ciSafeFields = map[string]bool{
	"id": true, "principal_id": true, "key_id": true, "name": true,
	"is_active": true, "credential_version": true, "grant": true,
	"version": true, "project_id": true, "flow_id": true, "actions": true,
	"repository_identifier": true, "repository_slug": true, "tracker_type": true,
	"keys": true, "expires_at": true, "created_at": true, "last_used_at": true,
	"identity": true, "available": true, "can_view": true, "can_manage": true,
	"supported_actions": true, "binding": true, "tracker_id": true,
	"runner_pool": true, "url": true, "enabled": true, "event_types": true,
	"max_retries": true, "retry_backoff_seconds": true, "timeout_seconds": true,
}

func ciSafeMetadata(value interface{}) interface{} {
	switch typed := value.(type) {
	case map[string]interface{}:
		out := map[string]interface{}{}
		for name, item := range typed {
			if ciSafeFields[name] {
				out[name] = ciSafeMetadata(item)
			}
		}
		return out
	case []interface{}:
		out := make([]interface{}, len(typed))
		for i, item := range typed {
			out[i] = ciSafeMetadata(item)
		}
		return out
	default:
		return value
	}
}

func newCICmd() *cobra.Command {
	group := &cobra.Command{
		Use: "ci", Short: "Human setup for restricted CI identities",
		Long: "Use your saved human login or PRELOOP_TOKEN. Issued secrets go only to a new 0600 file; safe metadata goes to stdout. CI credentials cannot administer themselves.",
	}
	for _, operation := range []string{"capabilities", "list", "show", "preview", "create", "update", "issue", "rotate", "revoke", "subscribe"} {
		group.AddCommand(newCICommand(operation))
	}
	return group
}

func newCICommand(operation string) *cobra.Command {
	var inputPath, secretPath string
	argCount := 0
	if operation == "show" || operation == "update" || operation == "issue" || operation == "subscribe" {
		argCount = 1
	} else if operation == "rotate" || operation == "revoke" {
		argCount = 2
	}
	use := operation
	if argCount > 0 {
		use += " PRINCIPAL_ID"
	}
	if argCount == 2 {
		use += " KEY_ID"
	}
	secretName := ""
	if operation == "create" || operation == "issue" || operation == "rotate" {
		secretName = "token"
	} else if operation == "subscribe" {
		secretName = "secret"
	}
	needsBody := operation == "preview" || operation == "create" || operation == "update" || operation == "subscribe"
	cmd := &cobra.Command{
		Use: use, Short: "Restricted CI " + operation, Args: cobra.ExactArgs(argCount),
		RunE: func(cmd *cobra.Command, args []string) error {
			// The global plaintext-token flag must not leak credentials into
			// process lists or shell history for these setup operations.
			if FlagToken != "" || cmd.Flags().Changed("token") {
				return fmt.Errorf("use your saved login or PRELOOP_TOKEN, not --token")
			}
			for _, id := range args {
				if !ciIDPattern.MatchString(id) {
					return fmt.Errorf("principal and key identifiers must be UUIDs")
				}
			}
			var payload map[string]interface{}
			if inputPath != "" {
				data, err := os.ReadFile(inputPath)
				if err != nil {
					return fmt.Errorf("cannot read CI request file")
				}
				if json.Unmarshal(data, &payload) != nil || payload == nil {
					return fmt.Errorf("CI request file must contain one JSON object")
				}
			} else if needsBody {
				return fmt.Errorf("--input is required (JSON request file; never a token)")
			} else if operation == "issue" || operation == "rotate" {
				payload = map[string]interface{}{}
			}
			var destination *os.File
			if secretName != "" {
				if secretPath == "" || secretPath == "-" {
					return fmt.Errorf("--secret-file must name a new private file")
				}
				var err error
				destination, err = os.OpenFile(secretPath, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
				if err != nil {
					return fmt.Errorf("cannot reserve secret file; use a new path in a private directory")
				}
				defer destination.Close()
			}
			client, err := api.NewClient("", FlagURL)
			if err != nil {
				if destination != nil {
					_ = os.Remove(secretPath)
				}
				return fmt.Errorf("cannot load human authentication")
			}
			method, suffix := ciRequestPath(operation, args)
			var result interface{}
			if err = client.CIAdminRequest(method, suffix, payload, &result); err != nil {
				if destination != nil {
					_ = os.Remove(secretPath)
				}
				return err
			}
			if destination != nil {
				body, ok := result.(map[string]interface{})
				secret, valid := body[secretName].(string)
				if !ok || !valid || strings.TrimSpace(secret) == "" {
					return fmt.Errorf("secret response missing; inspect identity metadata and revoke or replace the new credential")
				}
				if _, err = io.WriteString(destination, secret+"\n"); err != nil {
					return fmt.Errorf("secret file write failed after issuance; inspect metadata and revoke or replace the new credential")
				}
				if err = destination.Sync(); err != nil {
					return fmt.Errorf("secret file sync failed after issuance; inspect metadata and revoke or replace the new credential")
				}
				if err = destination.Close(); err != nil {
					return fmt.Errorf("secret file close failed after issuance; inspect metadata and revoke or replace the new credential")
				}
			}
			return json.NewEncoder(cmd.OutOrStdout()).Encode(ciSafeMetadata(result))
		},
	}
	if needsBody || operation == "issue" || operation == "rotate" {
		cmd.Flags().StringVar(&inputPath, "input", "", "JSON request file matching the human API schema")
	}
	if secretName != "" {
		cmd.Flags().StringVar(&secretPath, "secret-file", "", "new exclusive 0600 file for the issued token or signing secret")
	}
	return cmd
}

func ciRequestPath(operation string, args []string) (string, string) {
	suffix := ""
	if len(args) > 0 {
		suffix = "/" + args[0]
	}
	switch operation {
	case "capabilities":
		return http.MethodGet, "/capabilities"
	case "list", "show":
		return http.MethodGet, suffix
	case "preview":
		return http.MethodPost, "/preview"
	case "create":
		return http.MethodPost, ""
	case "update":
		return http.MethodPatch, suffix
	case "issue":
		return http.MethodPost, suffix + "/keys"
	case "rotate":
		return http.MethodPost, suffix + "/keys/" + args[1] + "/rotate"
	case "revoke":
		return http.MethodDelete, suffix + "/keys/" + args[1]
	case "subscribe":
		return http.MethodPost, suffix + "/subscriptions"
	default:
		panic("unknown CI administration operation")
	}
}
