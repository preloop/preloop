package cmd

// Tests for the Claude Code subscription-OAuth onboarding lifecycle.
//
// Claude Code's Anthropic subscription OAuth bundle uses a single-use rotating
// refresh token. Once onboarding imports the bundle into the Preloop account,
// the account copy owns the live token lineage (the gateway refreshes it,
// rotating the refresh token and invalidating the copy left in
// ~/.claude/.credentials.json). These tests pin the behaviors that keep
// repeated onboard/offboard cycles from destroying either copy:
//
//  1. Offboarding restores the agent config file only — it must never touch
//     the rotating local credential file.
//  2. Re-onboarding with a STALE local bundle must not overwrite the
//     account's live, gateway-refreshed credential (which would brick it
//     with invalid_grant / "Refresh token not found or invalid").
//  3. Re-onboarding when NO local credential resolves must reuse the
//     credential already stored in the account instead of degrading a
//     recoverable enrollment to MCP-only.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func claudeOAuthUpstreamForTest(payload map[string]interface{}) *managedGatewayUpstream {
	upstream := &managedGatewayUpstream{
		SourceAgent:       "claude_code",
		SourceProviderID:  "anthropic",
		ProviderName:      "anthropic",
		ModelIdentifier:   "claude-haiku-4-5",
		ManagedModelAlias: "anthropic/claude-haiku-4-5",
	}
	if payload != nil {
		upstream.CredentialType = "oauth_anthropic_claude_code"
		upstream.CredentialPayload = payload
	}
	return upstream
}

func claudeGatewayAIModelForTest(hasAPIKey bool) aiModelResponse {
	return aiModelResponse{
		ID:              "model-1",
		Name:            "Claude Code anthropic/claude-haiku-4-5",
		ProviderName:    "anthropic",
		ModelIdentifier: "claude-haiku-4-5",
		CredentialType:  "oauth_anthropic_claude_code",
		HasAPIKey:       hasAPIKey,
		MetaData: map[string]interface{}{
			"gateway": map[string]interface{}{
				"enabled":     true,
				"model_alias": "anthropic/claude-haiku-4-5",
			},
		},
	}
}

// newClaudeGatewayModelSyncServer serves the two endpoints
// syncManagedGatewayAIModel touches and records every PUT update body.
func newClaudeGatewayModelSyncServer(
	t *testing.T,
	models []aiModelResponse,
	updates *[]map[string]interface{},
) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
			_ = json.NewEncoder(w).Encode(models)
		case r.Method == http.MethodPut && strings.HasPrefix(r.URL.Path, "/api/v1/ai-models/"):
			update := map[string]interface{}{}
			if err := json.NewDecoder(r.Body).Decode(&update); err != nil {
				t.Fatalf("failed to decode ai-model update: %v", err)
			}
			*updates = append(*updates, update)
			updated := models[0]
			_ = json.NewEncoder(w).Encode(updated)
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/ai-models":
			t.Errorf("unexpected AI model create during credential reuse: %s", r.URL.Path)
			_ = json.NewEncoder(w).Encode(models[0])
		default:
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
}

func TestSyncManagedGatewayAIModelKeepsServerOAuthWhenLocalBundleExpired(t *testing.T) {
	updates := []map[string]interface{}{}
	server := newClaudeGatewayModelSyncServer(
		t,
		[]aiModelResponse{claudeGatewayAIModelForTest(true)},
		&updates,
	)
	defer server.Close()
	client := api.NewClientWithToken(server.URL, "tok")

	staleExpiry := time.Now().UTC().Add(-24 * time.Hour).UnixMilli()
	upstream := claudeOAuthUpstreamForTest(map[string]interface{}{
		"access":  "sk-ant-oat01-stale",
		"refresh": "sk-ant-ort01-stale",
		"expires": staleExpiry,
	})

	model, _, err := syncManagedGatewayAIModel(
		client,
		&managedAgentSummary{ID: "agent-1"},
		AgentConfig{Name: "Claude Code"},
		upstream,
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if model == nil || model.ID != "model-1" {
		t.Fatalf("expected reused model, got %#v", model)
	}
	for _, update := range updates {
		if _, ok := update["credential_payload"]; ok {
			t.Fatalf(
				"expired local OAuth bundle must not overwrite the account credential; got update %#v",
				update,
			)
		}
	}
}

func TestSyncManagedGatewayAIModelReseedsServerOAuthWhenLocalBundleFresh(t *testing.T) {
	updates := []map[string]interface{}{}
	server := newClaudeGatewayModelSyncServer(
		t,
		[]aiModelResponse{claudeGatewayAIModelForTest(true)},
		&updates,
	)
	defer server.Close()
	client := api.NewClientWithToken(server.URL, "tok")

	freshExpiry := time.Now().UTC().Add(4 * time.Hour).UnixMilli()
	upstream := claudeOAuthUpstreamForTest(map[string]interface{}{
		"access":  "sk-ant-oat01-fresh",
		"refresh": "sk-ant-ort01-fresh",
		"expires": freshExpiry,
	})

	model, _, err := syncManagedGatewayAIModel(
		client,
		&managedAgentSummary{ID: "agent-1"},
		AgentConfig{Name: "Claude Code"},
		upstream,
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if model == nil {
		t.Fatalf("expected reused model, got nil")
	}
	reseeded := false
	for _, update := range updates {
		if payload, ok := update["credential_payload"].(map[string]interface{}); ok {
			reseeded = true
			if payload["access"] != "sk-ant-oat01-fresh" {
				t.Fatalf("expected fresh access token in re-seed, got %#v", payload)
			}
		}
	}
	if !reseeded {
		t.Fatalf("expected a fresh local OAuth bundle to re-seed the account credential; updates: %#v", updates)
	}
}

func TestSyncManagedGatewayAIModelReusesServerCredentialWhenLocalMissing(t *testing.T) {
	updates := []map[string]interface{}{}
	server := newClaudeGatewayModelSyncServer(
		t,
		[]aiModelResponse{claudeGatewayAIModelForTest(true)},
		&updates,
	)
	defer server.Close()
	client := api.NewClientWithToken(server.URL, "tok")

	// No local credential at all: Claude Code is logged out (its rotated
	// bundle was invalidated) but the account already holds the live copy.
	upstream := claudeOAuthUpstreamForTest(nil)
	if upstream.CanRouteThroughGateway() {
		t.Fatalf("test upstream must not be locally routable")
	}

	model, notes, err := syncManagedGatewayAIModel(
		client,
		&managedAgentSummary{ID: "agent-1"},
		AgentConfig{Name: "Claude Code"},
		upstream,
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if model == nil || model.ID != "model-1" {
		t.Fatalf("expected server credential reuse to return the stored model, got %#v", model)
	}
	foundNote := false
	for _, note := range notes {
		if strings.Contains(note, "already stored in your Preloop account") {
			foundNote = true
		}
	}
	if !foundNote {
		t.Fatalf("expected server-credential reuse note, got %#v", notes)
	}
	for _, update := range updates {
		if _, ok := update["credential_payload"]; ok {
			t.Fatalf("credential reuse must not rewrite the stored credential; got %#v", update)
		}
	}
}

func TestSyncManagedGatewayAIModelNoServerReuseWithoutStoredCredential(t *testing.T) {
	updates := []map[string]interface{}{}
	server := newClaudeGatewayModelSyncServer(
		t,
		[]aiModelResponse{claudeGatewayAIModelForTest(false)},
		&updates,
	)
	defer server.Close()
	client := api.NewClientWithToken(server.URL, "tok")

	upstream := claudeOAuthUpstreamForTest(nil)
	model, _, err := syncManagedGatewayAIModel(
		client,
		&managedAgentSummary{ID: "agent-1"},
		AgentConfig{Name: "Claude Code"},
		upstream,
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if model != nil {
		t.Fatalf("expected no reuse when the stored model has no credential, got %#v", model)
	}
}

func TestSyncManagedGatewayAIModelServerReuseScopedToClaudeCode(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("no API traffic expected for a non-Claude agent without local credentials, got %s %s", r.Method, r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()
	client := api.NewClientWithToken(server.URL, "tok")

	upstream := claudeOAuthUpstreamForTest(nil)
	model, _, err := syncManagedGatewayAIModel(
		client,
		&managedAgentSummary{ID: "agent-1"},
		AgentConfig{Name: "OpenClaw"},
		upstream,
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if model != nil {
		t.Fatalf("expected no server-credential reuse outside Claude Code, got %#v", model)
	}
}

func TestOauthCredentialPayloadExpired(t *testing.T) {
	now := time.Now().UTC().UnixMilli()
	if oauthCredentialPayloadExpired(map[string]interface{}{"expires": now + 60_000}) {
		t.Fatalf("future expiry must not be treated as expired")
	}
	if !oauthCredentialPayloadExpired(map[string]interface{}{"expires": now - 60_000}) {
		t.Fatalf("past expiry must be treated as expired")
	}
	if oauthCredentialPayloadExpired(map[string]interface{}{}) {
		t.Fatalf("missing expiry must not be treated as expired")
	}
	if oauthCredentialPayloadExpired(map[string]interface{}{"expires": "not-a-number"}) {
		t.Fatalf("unparseable expiry must not be treated as expired")
	}
}

func writeClaudeCredentialsFileForTest(t *testing.T, home, access string, expiresAtMS int64) string {
	t.Helper()
	claudeDir := filepath.Join(home, ".claude")
	if err := os.MkdirAll(claudeDir, 0o755); err != nil {
		t.Fatalf("failed to create .claude dir: %v", err)
	}
	path := filepath.Join(claudeDir, ".credentials.json")
	blob := fmt.Sprintf(
		`{"claudeAiOauth":{"accessToken":%q,"refreshToken":"sk-ant-ort01-test","expiresAt":%d,"scopes":["user:inference"],"subscriptionType":"max"}}`,
		access,
		expiresAtMS,
	)
	if err := os.WriteFile(path, []byte(blob), 0o600); err != nil {
		t.Fatalf("failed to write credentials file: %v", err)
	}
	return path
}

func TestOnboardOffboardCyclesPreserveRotatingClaudeCredentialFile(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	// Keep removeClaudeCodeManagedMCPServer from invoking a real `claude`
	// binary found on the developer's PATH.
	t.Setenv("PATH", "")

	claudeDir := filepath.Join(home, ".claude")
	if err := os.MkdirAll(claudeDir, 0o755); err != nil {
		t.Fatalf("failed to create .claude dir: %v", err)
	}
	settingsPath := filepath.Join(claudeDir, "settings.json")
	original := []byte("{\n  \"model\": \"haiku\"\n}\n")
	if err := os.WriteFile(settingsPath, original, 0o644); err != nil {
		t.Fatalf("failed to seed settings.json: %v", err)
	}
	credentialsPath := writeClaudeCredentialsFileForTest(
		t,
		home,
		"sk-ant-oat01-cycle-0",
		time.Now().UTC().Add(8*time.Hour).UnixMilli(),
	)

	agent := AgentConfig{Name: "Claude Code", ConfigPath: settingsPath}

	for cycle, rotatedAccess := range []string{"sk-ant-oat01-cycle-1", "sk-ant-oat01-cycle-2"} {
		// --- onboard: back up the clean config, then rewrite it managed ---
		originalBytes, configExisted, err := readExistingAgentConfig(settingsPath)
		if err != nil {
			t.Fatalf("cycle %d: failed to read config: %v", cycle, err)
		}
		if !bytes.Equal(originalBytes, original) {
			t.Fatalf(
				"cycle %d: onboarding must start from the restored clean config; got %s",
				cycle,
				originalBytes,
			)
		}
		backupState, err := createLocalEnrollmentBackup(
			agent,
			configExisted,
			originalBytes,
			managedMCPEnrollmentPlan{},
		)
		if err != nil {
			t.Fatalf("cycle %d: failed to create backup: %v", cycle, err)
		}
		backupBytes, err := os.ReadFile(backupState.BackupPath)
		if err != nil {
			t.Fatalf("cycle %d: failed to read backup: %v", cycle, err)
		}
		if !bytes.Equal(backupBytes, original) {
			t.Fatalf(
				"cycle %d: backup must capture the clean pre-onboarding config, got %s",
				cycle,
				backupBytes,
			)
		}
		managed := []byte(`{"env":{"ANTHROPIC_BASE_URL":"https://preloop.example/anthropic","ANTHROPIC_API_KEY":"pl-durable-token"},"model":"haiku"}`)
		if err := os.WriteFile(settingsPath, managed, 0o644); err != nil {
			t.Fatalf("cycle %d: failed to write managed config: %v", cycle, err)
		}
		if err := saveLocalEnrollmentState(backupState); err != nil {
			t.Fatalf("cycle %d: failed to save enrollment state: %v", cycle, err)
		}

		// --- Claude Code rotates its subscription credential mid-enrollment ---
		writeClaudeCredentialsFileForTest(
			t,
			home,
			rotatedAccess,
			time.Now().UTC().Add(8*time.Hour).UnixMilli(),
		)

		// --- offboard: restore the backup ---
		state, err := loadLocalEnrollmentState(agent)
		if err != nil {
			t.Fatalf("cycle %d: failed to load enrollment state: %v", cycle, err)
		}
		if _, err := restoreAgentFromBackup(agent, state); err != nil {
			t.Fatalf("cycle %d: failed to restore from backup: %v", cycle, err)
		}
		if err := removeLocalEnrollmentState(agent); err != nil {
			t.Fatalf("cycle %d: failed to remove enrollment state: %v", cycle, err)
		}

		restored, err := os.ReadFile(settingsPath)
		if err != nil {
			t.Fatalf("cycle %d: failed to read restored settings: %v", cycle, err)
		}
		if !bytes.Equal(restored, original) {
			t.Fatalf("cycle %d: expected clean restored settings, got %s", cycle, restored)
		}
		credentials, err := os.ReadFile(credentialsPath)
		if err != nil {
			t.Fatalf("cycle %d: credential file must survive offboarding: %v", cycle, err)
		}
		if !strings.Contains(string(credentials), rotatedAccess) {
			t.Fatalf(
				"cycle %d: offboarding clobbered the rotated credential file; got %s",
				cycle,
				credentials,
			)
		}
	}
}

func TestPrintClaudeCodeOAuthOffboardNote(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	// Expired local bundle: the gateway-held copy superseded it, so the user
	// gets a recovery hint.
	writeClaudeCredentialsFileForTest(
		t,
		home,
		"sk-ant-oat01-expired",
		time.Now().UTC().Add(-2*time.Hour).UnixMilli(),
	)
	var out bytes.Buffer
	printClaudeCodeOAuthOffboardNote(&out)
	if !strings.Contains(out.String(), "re-authenticate") {
		t.Fatalf("expected a re-authentication note for an expired local bundle, got %q", out.String())
	}

	// Fresh local bundle: no note.
	writeClaudeCredentialsFileForTest(
		t,
		home,
		"sk-ant-oat01-fresh",
		time.Now().UTC().Add(6*time.Hour).UnixMilli(),
	)
	out.Reset()
	printClaudeCodeOAuthOffboardNote(&out)
	if out.String() != "" {
		t.Fatalf("expected no note for a fresh local bundle, got %q", out.String())
	}
}
