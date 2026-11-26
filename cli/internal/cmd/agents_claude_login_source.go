package cmd

import (
	"fmt"
	"os"
	"strings"
)

// claudeOAuthSourceEnv marks a subscription login imported from the
// CLAUDE_CODE_OAUTH_TOKEN environment variable (a long-lived token from
// `claude setup-token`). It has no refresh token, so Preloop never rotates it
// and offboarding has nothing to write back.
const claudeOAuthSourceEnv = "env"

const claudeOAuthTokenEnvName = "CLAUDE_CODE_OAUTH_TOKEN"

// claudeEnvOAuthToken returns the subscription token Claude Code would take
// from its environment: the settings env block first, then the process.
func claudeEnvOAuthToken(document map[string]interface{}) string {
	if token := resolveConfigSecret(lookupValue(document, "env", claudeOAuthTokenEnvName)); token != "" {
		return strings.TrimSpace(token)
	}
	return strings.TrimSpace(os.Getenv(claudeOAuthTokenEnvName))
}

// claudeLoginSourceDescription names a local login store for a person.
func claudeLoginSourceDescription(source string) string {
	switch source {
	case claudeOAuthSourceEnv:
		return "the environment variable " + claudeOAuthTokenEnvName
	case claudeOAuthSourceFile:
		return "the credentials file " + claudeCredentialFilePath()
	case claudeOAuthSourceKeychain:
		return `the macOS Keychain item "Claude Code-credentials"`
	}
	return "this machine's Claude Code login (store not recorded)"
}

// loadedClaudeLoginSource returns the store the imported login was read
// from: the one recorded at enrollment, otherwise the one import would read
// now.
func loadedClaudeLoginSource(agent AgentConfig) string {
	if state, err := loadLocalEnrollmentState(agent); err == nil && state.ClaudeOAuthCredentialSource != "" {
		return state.ClaudeOAuthCredentialSource
	}
	document, _, _ := loadJSONDocumentIfExists(agent.ConfigPath)
	if claudeEnvOAuthToken(document) != "" {
		return claudeOAuthSourceEnv
	}
	if credential, _ := resolveClaudeOAuthCredential(); credential != nil {
		return credential.Source
	}
	return ""
}

// claudeLoginIsEnvSourced reports whether this enrollment imported its
// subscription login from the environment token.
func claudeLoginIsEnvSourced(agent AgentConfig) bool {
	state, err := loadLocalEnrollmentState(agent)
	return err == nil && state.ClaudeOAuthCredentialSource == claudeOAuthSourceEnv
}

// rejectedSubscriptionLoginAdvice explains a rejected imported login: where
// it was read from, why it is probably stale, and how to import the live one.
func rejectedSubscriptionLoginAdvice(agent AgentConfig, source string) []string {
	if isCodexCLIAgent(agent) {
		return []string{
			"The provider rejected the ChatGPT login Preloop imported from this machine's Codex auth store.",
			"Sign in again with `codex login`, then run: " + subscriptionReconnectCommand(agent),
		}
	}
	lines := []string{
		"Anthropic rejected the Claude subscription login Preloop imported.",
		"It was read from " + claudeLoginSourceDescription(source) + ".",
	}
	switch source {
	case claudeOAuthSourceEnv:
		lines = append(lines,
			"That token looks stale: it was revoked, has passed its one year life, or belongs to another account.",
			"Create a new one with `claude setup-token`, put it in "+claudeOAuthTokenEnvName+", then run: "+subscriptionReconnectCommand(agent),
		)
	default:
		lines = append(lines,
			"That copy looks stale: a subscription refresh token works once, so a login copied or mounted from another machine stops working as soon as the original is refreshed there.",
			"Import the live one: sign in where this agent runs (`claude auth login --claudeai`), then run: "+subscriptionReconnectCommand(agent),
		)
		if detectContainerRuntime() != "" {
			lines = append(lines,
				"In a container, either sign in inside the container or pass a token from `claude setup-token` as "+claudeOAuthTokenEnvName+"; do not share one credentials file between the host and the container.",
			)
		}
	}
	lines = append(lines, fmt.Sprintf("Guide: %s#how-the-claude-login-reaches-the-container", manualSetupDocsURL))
	return lines
}

// subscriptionUnverifiedHeadline replaces "Onboarded" when a subscription
// login backs the route and no real request was made with it: a login that
// was never used cannot be reported as working. It returns "" when the
// normal headline applies.
func subscriptionUnverifiedHeadline(
	agent AgentConfig,
	upstream *managedGatewayUpstream,
	probeRequested bool,
	probeDeferred bool,
) string {
	if probeRequested || probeDeferred || upstream == nil {
		return ""
	}
	if !isOAuthCredentialType(upstream.CredentialType) {
		return ""
	}
	name := resolveAgentDisplayName(agent)
	return fmt.Sprintf(
		"Configured %s; the subscription login was NOT verified (gateway validation skipped). Verify it with one real request: preloop agents validate %s --live",
		name, shellQuoteAgentName(name),
	)
}
