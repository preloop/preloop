package cmd

// Offboard write-back of subscription OAuth credentials.
//
// Claude Code and Codex subscription OAuth bundles use single-use rotating
// refresh tokens. While an agent is onboarded its model traffic flows through
// the Preloop gateway, which refreshes the imported bundle — rotating the
// refresh token and invalidating the copy left in the agent's local
// credential store. Restoring the pre-onboarding config therefore cannot
// restore a working login: the Preloop account holds the only live lineage.
//
// At offboard time the CLI exports that live bundle over the authenticated
// API (POST /api/v1/ai-models/{id}/credentials/export) and writes it back to
// the agent's local credential store, so offboarding never costs the operator
// their subscription login. This runs BEFORE offboard cleanup can delete the
// Preloop-held model credential.

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
)

const (
	anthropicClaudeCodeOAuthCredentialType = "oauth_anthropic_claude_code"
	openaiCodexOAuthCredentialType         = "oauth_openai_codex"
)

type exportedModelCredential struct {
	CredentialType string `json:"credential_type"`
	Access         string `json:"access"`
	Refresh        string `json:"refresh"`
	Expires        int64  `json:"expires"`
	AccountID      string `json:"account_id"`
}

// restoreSubscriptionLoginOnOffboard exports the live subscription OAuth
// bundle for the agent's bound models and writes it back to the agent's
// local credential store. Returns true when a bundle was written back.
// Failures are reported on writer but never abort the offboard.
func restoreSubscriptionLoginOnOffboard(
	client *api.Client,
	agent AgentConfig,
	detail *managedAgentDetailResponse,
	writer io.Writer,
) bool {
	if client == nil || !client.IsAuthenticated() || detail == nil {
		return false
	}
	wantType := ""
	switch {
	case isClaudeCodeAgent(agent):
		wantType = anthropicClaudeCodeOAuthCredentialType
	case isCodexCLIAgent(agent):
		wantType = openaiCodexOAuthCredentialType
	default:
		return false
	}
	seen := map[string]struct{}{}
	for _, binding := range detail.Agent.ConfiguredModels {
		modelID := strings.TrimSpace(binding.AIModelID)
		if modelID == "" {
			continue
		}
		if _, dup := seen[modelID]; dup {
			continue
		}
		seen[modelID] = struct{}{}

		var bundle exportedModelCredential
		err := client.Post(
			fmt.Sprintf("/api/v1/ai-models/%s/credentials/export", modelID),
			nil,
			&bundle,
		)
		if err != nil {
			// Not exportable (API-key credential, removed model, or a
			// provider refresh failure) — try the next bound model.
			continue
		}
		if bundle.CredentialType != wantType || strings.TrimSpace(bundle.Access) == "" {
			continue
		}

		var path string
		switch wantType {
		case anthropicClaudeCodeOAuthCredentialType:
			path, err = writeClaudeSubscriptionCredential(bundle)
		case openaiCodexOAuthCredentialType:
			path, err = writeCodexSubscriptionCredential(bundle)
		}
		if err != nil {
			fmt.Fprintf( //nolint:errcheck
				writer,
				"  Warning: could not write the restored subscription login locally: %v\n",
				err,
			)
			return false
		}
		fmt.Fprintf( //nolint:errcheck
			writer,
			"  Restored subscription login: live token written back to %s (subscription tokens rotate, and the Preloop account held the active copy while onboarded).\n",
			path,
		)
		return true
	}
	return false
}

// writeClaudeSubscriptionCredential merges the exported bundle into
// ~/.claude/.credentials.json (creating it when absent), preserving unrelated
// fields such as scopes and subscriptionType. On macOS it additionally
// best-effort updates the Claude Code keychain entry when one exists, since
// the CLI prefers the keychain there.
func writeClaudeSubscriptionCredential(bundle exportedModelCredential) (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("failed to resolve home directory: %w", err)
	}
	path := filepath.Join(home, ".claude", ".credentials.json")

	document := map[string]interface{}{}
	if data, readErr := os.ReadFile(path); readErr == nil {
		_ = json.Unmarshal(data, &document) //nolint:errcheck // corrupt file → rebuild
		if document == nil {
			document = map[string]interface{}{}
		}
	}
	container, ok := asObjectMap(document["claudeAiOauth"])
	if !ok {
		container = map[string]interface{}{}
	}
	container["accessToken"] = strings.TrimSpace(bundle.Access)
	if refresh := strings.TrimSpace(bundle.Refresh); refresh != "" {
		container["refreshToken"] = refresh
	}
	if bundle.Expires > 0 {
		container["expiresAt"] = bundle.Expires
	}
	document["claudeAiOauth"] = container

	data, err := json.MarshalIndent(document, "", "  ")
	if err != nil {
		return "", fmt.Errorf("failed to encode credential file: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return "", fmt.Errorf("failed to create %s: %w", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return "", fmt.Errorf("failed to write %s: %w", path, err)
	}

	if runtime.GOOS == "darwin" {
		updateClaudeKeychainCredentialBlob(string(data))
	}
	return path, nil
}

// updateClaudeKeychainCredentialBlob replaces the Claude Code keychain blob
// when one already exists. Best-effort: a missing entry or a locked keychain
// must not fail the offboard (the file write above already succeeded).
func updateClaudeKeychainCredentialBlob(blob string) {
	if readClaudeKeychainCredentialBlob() == "" {
		return
	}
	account := strings.TrimSpace(os.Getenv("USER"))
	if account == "" {
		account = "Claude Code"
	}
	_ = exec.Command( //nolint:errcheck
		"security",
		"add-generic-password",
		"-U",
		"-s", "Claude Code-credentials",
		"-a", account,
		"-w", blob,
	).Run()
}

// writeCodexSubscriptionCredential merges the exported bundle into Codex's
// auth.json (creating it when absent), preserving unrelated fields such as
// id_token — Codex re-derives a fresh id_token on its next refresh using the
// restored refresh token.
func writeCodexSubscriptionCredential(bundle exportedModelCredential) (string, error) {
	path := resolveCodexAuthPath()

	document := map[string]interface{}{}
	if data, readErr := os.ReadFile(path); readErr == nil {
		_ = json.Unmarshal(data, &document) //nolint:errcheck // corrupt file → rebuild
		if document == nil {
			document = map[string]interface{}{}
		}
	}
	tokens, ok := asObjectMap(document["tokens"])
	if !ok {
		tokens = map[string]interface{}{}
	}
	tokens["access_token"] = strings.TrimSpace(bundle.Access)
	if refresh := strings.TrimSpace(bundle.Refresh); refresh != "" {
		tokens["refresh_token"] = refresh
	}
	if accountID := strings.TrimSpace(bundle.AccountID); accountID != "" {
		tokens["account_id"] = accountID
	}
	document["tokens"] = tokens
	document["last_refresh"] = time.Now().UTC().Format(time.RFC3339)

	data, err := json.MarshalIndent(document, "", "  ")
	if err != nil {
		return "", fmt.Errorf("failed to encode auth file: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return "", fmt.Errorf("failed to create %s: %w", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return "", fmt.Errorf("failed to write %s: %w", path, err)
	}
	return path, nil
}
