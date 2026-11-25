package cmd

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
)

// preflightClaudeOffboardStore checks the selected destination before export
// can rotate a server grant. The source marker is also written by older builds
// that support explicit file/Keychain selection; read it without guessing from
// the mere presence of a credentials file beside a locked Keychain.
func preflightClaudeOffboardStore(client *api.Client, agent AgentConfig, detail *managedAgentDetailResponse) error {
	if !isClaudeCodeAgent(agent) || detail == nil || len(detail.Agent.ConfiguredModels) == 0 {
		return nil
	}
	var models []aiModelResponse
	if client == nil || client.Get(api.RESTPrefix+"/ai-models", &models) != nil {
		return errors.New("offboard stopped before export: cannot verify subscription bindings; server credentials and enrollment retained")
	}
	bound := map[string]bool{}
	for _, binding := range detail.Agent.ConfiguredModels {
		bound[binding.AIModelID] = true
	}
	needsRestore := false
	for _, model := range models {
		if bound[model.ID] && model.CredentialType == anthropicClaudeCodeOAuthCredentialType {
			needsRestore = true
		}
	}
	if !needsRestore {
		return nil
	}
	var marker struct {
		Source string `json:"claude_oauth_credential_source"`
	}
	if path, err := localEnrollmentStatePath(agent.Name, agent.ConfigPath); err == nil {
		if data, err := os.ReadFile(path); err == nil {
			_ = json.Unmarshal(data, &marker)
		}
	}
	if marker.Source == "file" {
		if err := preflightClaudeCredentialFile(); err == nil {
			return nil
		}
	} else if marker.Source == "" || marker.Source == "keychain" {
		blob, err := readClaudeOffboardKeychain()
		if err == nil && strings.TrimSpace(blob) != "" {
			return nil
		}
		if err == nil && marker.Source == "" {
			// No Keychain entry; the existing writer uses the file.
			if err := preflightClaudeCredentialFile(); err == nil {
				return nil
			}
		}
	}
	return errors.New("offboard stopped before export: the active Claude login store is unavailable or unknown. No server credentials were revoked; enrollment and gateway configuration are retained. Unlock Keychain, or run `preloop agents sync-credentials \"Claude Code\"` with a CLI supporting explicit file selection, then retry offboard")
}

// preflightClaudeCredentialFile checks the destination without touching a login.
// A recorded file store may need to be recreated from the server's live grant.
func preflightClaudeCredentialFile() error {
	path := claudeCredentialFilePath()
	data, err := readOffboardCredentialFile(path)
	if err != nil {
		return err
	}
	if len(data) > 0 {
		var document map[string]interface{}
		if err := json.Unmarshal(data, &document); err != nil || document == nil {
			return errors.New("invalid existing Claude credential document")
		}
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	probe, err := os.CreateTemp(filepath.Dir(path), ".preloop-write-check-*")
	if err != nil {
		return err
	}
	defer os.Remove(probe.Name()) //nolint:errcheck
	return probe.Close()
}
