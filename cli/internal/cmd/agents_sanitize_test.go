package cmd

import (
	"strings"
	"testing"
)

// The snapshot sent to the server and kept in local state must hold no
// secret, whatever the key is called. Env-style names used to pass through.
func TestSanitizeConfigSnapshotRedactsEnvStyleSecrets(t *testing.T) {
	doc := map[string]interface{}{
		"theme": "dark",
		"model": "sonnet",
		"env": map[string]interface{}{
			"ANTHROPIC_API_KEY":             "sk-ant-api03-synthetic-upstream",
			"ANTHROPIC_AUTH_TOKEN":          "synthetic-auth",
			"CLAUDE_CODE_OAUTH_TOKEN":       "sk-ant-oat01-synthetic",
			"AWS_SECRET_ACCESS_KEY":         "synthetic-aws-secret",
			"AWS_SESSION_TOKEN":             "synthetic-aws-session",
			"AWS_BEARER_TOKEN_BEDROCK":      "synthetic-bedrock",
			"ANTHROPIC_BASE_URL":            "https://preloop.example.com/anthropic",
			"ANTHROPIC_CUSTOM_MODEL_OPTION": "anthropic/claude-sonnet-4-5",
			"MAX_THINKING_TOKENS":           "8000",
			"UNRELATED":                     syntheticAgentKey,
		},
		"experimental_bearer_token": "synthetic-codex",
		"mcpServers": map[string]interface{}{
			"example": map[string]interface{}{
				"args":    []interface{}{"--header", "Authorization: Bearer synthetic-inline-token-000000"},
				"headers": map[string]interface{}{"X-Api-Key": "synthetic-header"},
			},
		},
	}
	sanitizeConfigSnapshot(doc)
	text := compactJSON(doc)
	for _, secret := range []string{
		"sk-ant-api03", "synthetic-auth", "sk-ant-oat01", "synthetic-aws", "synthetic-bedrock",
		"synthetic-codex", "synthetic-inline-token", "synthetic-header", syntheticAgentKey,
	} {
		if strings.Contains(text, secret) {
			t.Fatalf("snapshot still holds %q: %s", secret, text)
		}
	}
	for _, kept := range []string{
		`"ANTHROPIC_BASE_URL":"https://preloop.example.com/anthropic"`,
		`"ANTHROPIC_CUSTOM_MODEL_OPTION":"anthropic/claude-sonnet-4-5"`,
		`"MAX_THINKING_TOKENS":"8000"`,
		`"theme":"dark"`,
	} {
		if !strings.Contains(text, kept) {
			t.Fatalf("snapshot lost a non-secret %s: %s", kept, text)
		}
	}
}
