package cmd

import (
	"fmt"
	"net/url"
	"os/exec"
	"runtime"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

var agentsReconnectCmd = &cobra.Command{
	Use:   "reconnect <agent>",
	Short: "Reconnect a Claude or Codex subscription without re-onboarding",
	Long: `Sign in to Claude Code or Codex CLI and replace only the subscription
credential used by this enrollment. Models sharing a credential are repaired
with one update. Agent identity, policies, gateway configuration, and backups
are preserved.

Use --from-local after completing claude auth login or codex login yourself.
An expired or incomplete local login is refused. This command only updates
OAuth models owned by the existing enrollment in the selected Preloop account.

Examples:
  preloop agents reconnect "Claude Code"
  preloop agents reconnect "Codex CLI"
  preloop agents reconnect "Claude Code" --from-local`,
	Args: cobra.ExactArgs(1),
	RunE: runAgentsReconnect,
}

// These seams keep command tests away from browsers and real credential stores.
var runReconnectSubscriptionLogin = func(cmd *cobra.Command, args []string) error {
	login := exec.Command(args[0], args[1:]...)
	login.Stdin = cmd.InOrStdin()
	login.Stdout = cmd.OutOrStdout()
	login.Stderr = cmd.ErrOrStderr()
	return login.Run()
}

var readReconnectSubscriptionCredential = func(agent AgentConfig) map[string]interface{} {
	if isClaudeCodeAgent(agent) {
		credential, _ := resolveClaudeOAuthCredential()
		if runtime.GOOS == "darwin" {
			native, _ := resolveClaudeKeychainOAuthCredential()
			credential = selectClaudeReconnectCredential(credential, native, true)
		}
		if credential != nil {
			return credential.Payload()
		}
	} else if isCodexCLIAgent(agent) {
		credential, _ := resolveCodexOAuthCredential()
		if credential != nil {
			return credential.Payload()
		}
	}
	return nil
}

func selectClaudeReconnectCredential(file, keychain *claudeOAuthCredential, darwin bool) *claudeOAuthCredential {
	// Claude's native macOS login writes the Keychain. An old credential file
	// must not hide the login the operator just completed there.
	if darwin && keychain != nil {
		return keychain
	}
	return file
}

func init() {
	agentsCmd.AddCommand(agentsReconnectCmd)
	agentsReconnectCmd.Flags().Bool("from-local", false, "Use a fresh local subscription login without opening sign-in")
}

func runAgentsReconnect(cmd *cobra.Command, args []string) error {
	canonical, err := resolveAgentTypeName(args[0])
	if err != nil {
		return err
	}
	agent := AgentConfig{Name: canonical}
	wantType := ""
	loginArgs := []string{}
	switch {
	case isClaudeCodeAgent(agent):
		wantType = anthropicClaudeCodeOAuthCredentialType
		loginArgs = []string{"claude", "auth", "login", "--claudeai"}
	case isCodexCLIAgent(agent):
		wantType = openaiCodexOAuthCredentialType
		loginArgs = []string{"codex", "login"}
	default:
		return fmt.Errorf("reconnect supports Claude Code and Codex CLI subscription logins")
	}
	discovered, err := discoverAgents(cmd.OutOrStdout(), false)
	if err != nil {
		return err
	}
	agent, err = findDiscoveredAgent(discovered, canonical)
	if err != nil {
		return err
	}
	if _, err := loadLocalEnrollmentState(agent); err != nil {
		return fmt.Errorf("%s is not enrolled on this machine", canonical)
	}
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}
	if !client.IsAuthenticated() {
		return fmt.Errorf("run preloop login before reconnecting a subscription")
	}
	managed, err := getManagedAgentForDiscovered(client, agent)
	if err != nil {
		return err
	}
	// Check the destination before opening a browser or touching a local login.
	groups, err := listReconnectCredentialGroups(client, managed.ID, wantType)
	if err != nil {
		return err
	}
	fromLocal, _ := cmd.Flags().GetBool("from-local")
	if !fromLocal {
		if err := runReconnectSubscriptionLogin(cmd, loginArgs); err != nil {
			return fmt.Errorf("subscription sign-in failed: %w", err)
		}
	}
	payload := readReconnectSubscriptionCredential(agent)
	if err := validateReconnectCredential(payload); err != nil {
		return fmt.Errorf("%w; run %s and retry with --from-local", err, strings.Join(loginArgs, " "))
	}
	count, err := reconnectCredentialGroups(client, groups, wantType, payload)
	if err != nil {
		return err
	}
	fmt.Fprintf(cmd.OutOrStdout(), "Reconnected %s subscription: updated %d credential(s). Enrollment preserved.\n", canonical, count) //nolint:errcheck
	return nil
}

func validateReconnectCredential(payload map[string]interface{}) error {
	if lookupString(payload, "access") == "" || lookupString(payload, "refresh") == "" {
		return fmt.Errorf("no complete local subscription login found")
	}
	if coerceEpochMillis(payload["expires"]) <= time.Now().UTC().Add(time.Minute).UnixMilli() {
		return fmt.Errorf("local subscription login is expired or about to expire")
	}
	return nil
}

func listReconnectCredentialGroups(client *api.Client, agentID, credentialType string) ([][]aiModelResponse, error) {
	if strings.TrimSpace(agentID) == "" {
		return nil, fmt.Errorf("managed agent has no id")
	}
	var models []aiModelResponse
	if err := client.Get("/api/v1/ai-models", &models); err != nil {
		return nil, fmt.Errorf("list subscription models: %w", err)
	}
	groups := reconnectCredentialGroupsForEnrollment(models, agentID, credentialType)
	if len(groups) == 0 {
		return nil, fmt.Errorf("this enrollment has no matching subscription credential in the selected account")
	}
	return groups, nil
}

func reconnectCredentialGroupsForEnrollment(models []aiModelResponse, agentID, credentialType string) [][]aiModelResponse {
	groups := [][]aiModelResponse{}
	index := map[string]int{}
	if strings.TrimSpace(agentID) == "" {
		return groups
	}
	for _, model := range models {
		if model.CredentialType != credentialType || model.MetaData == nil || model.MetaData["managed_agent_id"] != agentID {
			continue
		}
		secretID := strings.TrimSpace(model.CredentialsSecretID)
		if secretID == "" || strings.TrimSpace(model.ID) == "" {
			continue
		}
		if position, ok := index[secretID]; ok {
			groups[position] = append(groups[position], model)
		} else {
			index[secretID] = len(groups)
			groups = append(groups, []aiModelResponse{model})
		}
	}
	return groups
}

func reconnectCredentialGroups(client *api.Client, groups [][]aiModelResponse, credentialType string, payload map[string]interface{}) (int, error) {
	if err := validateReconnectCredential(payload); err != nil {
		return 0, err
	}
	updated := 0
	secretID := ""
	for _, group := range groups {
		if len(group) == 0 {
			continue
		}
		if secretID == "" {
			var response aiModelResponse
			path := "/api/v1/ai-models/" + url.PathEscape(group[0].ID)
			if err := client.Put(path, map[string]interface{}{
				"credential_type": credentialType, "credential_payload": payload,
			}, &response); err != nil {
				return updated, fmt.Errorf("subscription reconnect failed: %w", err)
			}
			secretID = strings.TrimSpace(response.CredentialsSecretID)
			if secretID == "" {
				return updated, fmt.Errorf("subscription reconnect returned no credential reference")
			}
			updated++
			continue
		}
		// Never upload the same rotating bundle to multiple secrets. Repair
		// legacy split model families by attaching them to the single owner.
		for _, model := range group {
			var response aiModelResponse
			path := "/api/v1/ai-models/" + url.PathEscape(model.ID)
			if err := client.Put(path, map[string]interface{}{
				"credentials_secret_id": secretID,
			}, &response); err != nil {
				return updated, fmt.Errorf("subscription credential updated, but model %s could not be attached: %w", model.ID, err)
			}
		}
	}
	return updated, nil
}
