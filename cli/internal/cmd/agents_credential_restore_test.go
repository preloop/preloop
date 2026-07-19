package cmd

// Tests for the offboard subscription-credential write-back.
//
// Subscription OAuth refresh tokens rotate server-side while an agent is
// onboarded, so offboarding must restore the Preloop-held live bundle into
// the agent's local credential store — otherwise every offboard costs the
// operator their Claude/Codex login.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func exportServerForTest(
	t *testing.T,
	statusCode int,
	bundle exportedModelCredential,
) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost &&
			strings.HasSuffix(r.URL.Path, "/credentials/export") {
			w.WriteHeader(statusCode)
			if statusCode == http.StatusOK {
				_ = json.NewEncoder(w).Encode(bundle)
			} else {
				_ = json.NewEncoder(w).Encode(map[string]string{"detail": "nope"})
			}
			return
		}
		http.NotFound(w, r)
	}))
}

func detailWithModelForTest(modelID string) *managedAgentDetailResponse {
	return &managedAgentDetailResponse{
		Agent: managedAgentSummary{
			ID: "agent-1",
			ConfiguredModels: []managedAgentModelBindingSummary{
				{ID: "binding-1", AIModelID: modelID},
			},
		},
	}
}

func TestRestoreSubscriptionLoginWritesClaudeCredentialFile(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	credentialPath := filepath.Join(home, ".claude", ".credentials.json")
	if err := os.MkdirAll(filepath.Dir(credentialPath), 0o700); err != nil {
		t.Fatal(err)
	}
	// Simulate the stranded state: an existing file whose token was rotated
	// away, but which still carries fields the write-back must preserve.
	existing := `{"claudeAiOauth":{"accessToken":"","expiresAt":0,"scopes":["user:inference"],"subscriptionType":"max"}}`
	if err := os.WriteFile(credentialPath, []byte(existing), 0o600); err != nil {
		t.Fatal(err)
	}

	server := exportServerForTest(t, http.StatusOK, exportedModelCredential{
		CredentialType: anthropicClaudeCodeOAuthCredentialType,
		Access:         "live-access",
		Refresh:        "live-refresh",
		Expires:        1789000000000,
	})
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "test-token")
	agent := AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}
	var output bytes.Buffer

	restored := restoreSubscriptionLoginOnOffboard(
		client, agent, detailWithModelForTest("model-1"), &output,
	)
	if !restored {
		t.Fatalf("expected write-back to succeed, output: %s", output.String())
	}

	data, err := os.ReadFile(credentialPath)
	if err != nil {
		t.Fatal(err)
	}
	var document map[string]interface{}
	if err := json.Unmarshal(data, &document); err != nil {
		t.Fatal(err)
	}
	oauth, ok := document["claudeAiOauth"].(map[string]interface{})
	if !ok {
		t.Fatalf("claudeAiOauth missing: %s", data)
	}
	if oauth["accessToken"] != "live-access" {
		t.Errorf("accessToken not restored: %v", oauth["accessToken"])
	}
	if oauth["refreshToken"] != "live-refresh" {
		t.Errorf("refreshToken not restored: %v", oauth["refreshToken"])
	}
	if oauth["expiresAt"] != float64(1789000000000) {
		t.Errorf("expiresAt not restored: %v", oauth["expiresAt"])
	}
	// Unrelated fields must survive the merge.
	if oauth["subscriptionType"] != "max" {
		t.Errorf("subscriptionType lost in merge: %v", oauth["subscriptionType"])
	}
	if !strings.Contains(output.String(), "Restored subscription login") {
		t.Errorf("expected restored message, got: %s", output.String())
	}
}

func TestRestoreSubscriptionLoginWritesCodexAuthFile(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	authPath := filepath.Join(home, ".codex", "auth.json")
	if err := os.MkdirAll(filepath.Dir(authPath), 0o700); err != nil {
		t.Fatal(err)
	}
	existing := `{"OPENAI_API_KEY":null,"tokens":{"id_token":"keep-me","access_token":"stale","refresh_token":"stale"},"last_refresh":"2026-07-18T00:00:00Z"}`
	if err := os.WriteFile(authPath, []byte(existing), 0o600); err != nil {
		t.Fatal(err)
	}

	server := exportServerForTest(t, http.StatusOK, exportedModelCredential{
		CredentialType: openaiCodexOAuthCredentialType,
		Access:         "codex-access",
		Refresh:        "codex-refresh",
		AccountID:      "chatgpt-account",
	})
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "test-token")
	agent := AgentConfig{Name: "Codex CLI", ConfigPath: filepath.Join(home, ".codex", "config.toml")}
	var output bytes.Buffer

	restored := restoreSubscriptionLoginOnOffboard(
		client, agent, detailWithModelForTest("model-1"), &output,
	)
	if !restored {
		t.Fatalf("expected write-back to succeed, output: %s", output.String())
	}

	data, err := os.ReadFile(authPath)
	if err != nil {
		t.Fatal(err)
	}
	var document map[string]interface{}
	if err := json.Unmarshal(data, &document); err != nil {
		t.Fatal(err)
	}
	tokens, ok := document["tokens"].(map[string]interface{})
	if !ok {
		t.Fatalf("tokens missing: %s", data)
	}
	if tokens["access_token"] != "codex-access" {
		t.Errorf("access_token not restored: %v", tokens["access_token"])
	}
	if tokens["refresh_token"] != "codex-refresh" {
		t.Errorf("refresh_token not restored: %v", tokens["refresh_token"])
	}
	if tokens["account_id"] != "chatgpt-account" {
		t.Errorf("account_id not restored: %v", tokens["account_id"])
	}
	// Codex re-derives id_token on refresh, but an existing one must survive.
	if tokens["id_token"] != "keep-me" {
		t.Errorf("id_token lost in merge: %v", tokens["id_token"])
	}
	if document["last_refresh"] == "2026-07-18T00:00:00Z" {
		t.Errorf("last_refresh not updated")
	}
}

func TestRestoreSubscriptionLoginSkipsNonExportableCredentials(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())

	server := exportServerForTest(t, http.StatusBadRequest, exportedModelCredential{})
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "test-token")
	agent := AgentConfig{Name: "Claude Code"}
	var output bytes.Buffer

	restored := restoreSubscriptionLoginOnOffboard(
		client, agent, detailWithModelForTest("model-1"), &output,
	)
	if restored {
		t.Fatal("expected write-back to be skipped for non-exportable credentials")
	}
	if _, err := os.Stat(filepath.Join(home, ".claude", ".credentials.json")); !os.IsNotExist(err) {
		t.Errorf("credential file should not have been created, stat err: %v", err)
	}
}

func TestRestoreSubscriptionLoginRejectsMismatchedCredentialType(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())

	// A Codex bundle must never be written into Claude Code's store.
	server := exportServerForTest(t, http.StatusOK, exportedModelCredential{
		CredentialType: openaiCodexOAuthCredentialType,
		Access:         "codex-access",
	})
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "test-token")
	agent := AgentConfig{Name: "Claude Code"}
	var output bytes.Buffer

	restored := restoreSubscriptionLoginOnOffboard(
		client, agent, detailWithModelForTest("model-1"), &output,
	)
	if restored {
		t.Fatal("expected mismatched credential type to be rejected")
	}
	if _, err := os.Stat(filepath.Join(home, ".claude", ".credentials.json")); !os.IsNotExist(err) {
		t.Errorf("credential file should not have been created, stat err: %v", err)
	}
}

func TestRestoreSubscriptionLoginNeedsDetailAndAuth(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	var output bytes.Buffer

	if restoreSubscriptionLoginOnOffboard(nil, AgentConfig{Name: "Claude Code"}, detailWithModelForTest("m"), &output) {
		t.Error("nil client must not restore")
	}
	client := api.NewClientWithToken("http://127.0.0.1:0", "token")
	if restoreSubscriptionLoginOnOffboard(client, AgentConfig{Name: "Claude Code"}, nil, &output) {
		t.Error("nil detail must not restore")
	}
	if restoreSubscriptionLoginOnOffboard(client, AgentConfig{Name: "OpenCode"}, detailWithModelForTest("m"), &output) {
		t.Error("non-subscription agents must not restore")
	}
}
