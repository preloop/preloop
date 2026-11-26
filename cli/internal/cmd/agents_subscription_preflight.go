package cmd

import (
	"errors"
	"os"
	"path/filepath"
	"time"
)

const claudeSubscriptionLoginRepair = "Claude subscription login is expired or incomplete. Run `claude auth login --claudeai`, then rerun onboarding."

// preflightClaudeSubscriptionLogin rejects an unusable local import before
// enrollment mutations. It never refreshes the single-use local grant. An
// already managed route may retain a newer server login than the local copy.
func preflightClaudeSubscriptionLogin(agent AgentConfig, opts managedEnrollmentOptions, selected *aiModelResponse) error {
	if !isClaudeCodeAgent(agent) || opts.NoCredentialImport {
		return nil
	}
	if selected != nil && !oauthCredentialStatusIsError(selected) &&
		(selected.HasAPIKey || aiModelUsesAmbientProviderCredentials(selected)) {
		return nil
	}
	document, _, err := loadJSONDocumentIfExists(agent.ConfigPath)
	if err != nil {
		return err
	}
	// An explicitly selected API key is independent of a cached subscription.
	for _, key := range []string{"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"} {
		token := firstNonEmptyString(lookupString(document, "env", key), os.Getenv(key))
		if token != "" && !isClaudeCodeOAuthAccessToken(token) {
			return nil
		}
	}
	if token := resolveClaudePrimaryAPIKey(); token != "" && !isClaudeCodeOAuthAccessToken(token) {
		return nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	data, readErr := os.ReadFile(filepath.Join(home, ".claude", ".credentials.json"))
	var credential *claudeOAuthCredential
	if readErr == nil {
		credential = parseClaudeOAuthCredentialBlob(string(data), 0)
		if credential == nil {
			return errors.New(claudeSubscriptionLoginRepair)
		}
	} else {
		credential, _ = resolveClaudeKeychainOAuthCredential()
	}
	if credential == nil {
		return nil
	}
	expires := oauthPayloadEpochMillis(credential.ExpiresAtMS)
	if (expires > 0 && expires <= time.Now().UnixMilli()) || (credential.RefreshToken != "" && expires == 0) {
		return errors.New(claudeSubscriptionLoginRepair)
	}
	return nil
}
