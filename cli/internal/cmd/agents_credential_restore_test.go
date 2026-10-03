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
	"fmt"
	"runtime"

	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func isolateOffboardKeychains(t *testing.T) {
	t.Helper()
	oldClaude, oldCodex := readClaudeOffboardKeychain, readCodexOffboardKeychain
	oldFileWrite := writeOffboardCredentialStoreFile
	readClaudeOffboardKeychain = func() (string, error) { return "", nil }
	readCodexOffboardKeychain = func() (string, error) { return "", nil }
	t.Cleanup(func() {
		readClaudeOffboardKeychain, readCodexOffboardKeychain = oldClaude, oldCodex
		writeOffboardCredentialStoreFile = oldFileWrite
	})
}

func exportServerForTest(
	t *testing.T,
	statusCode int,
	bundle exportedModelCredential,
	metadataTypes ...string,
) *httptest.Server {
	t.Helper()
	metadataType := bundle.CredentialType
	if metadataType == "" {
		metadataType = anthropicClaudeCodeOAuthCredentialType
	}
	if len(metadataTypes) > 0 {
		metadataType = metadataTypes[0]
	}
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models" {
			_ = json.NewEncoder(w).Encode([]aiModelResponse{{ID: "model-1", CredentialType: metadataType}})
			return
		}
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
	isolateOffboardKeychains(t)
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

	restored, restoreErr := restoreSubscriptionLoginOnOffboard(
		client, agent, detailWithModelForTest("model-1"), &output,
	)
	if restoreErr != nil || restored != subscriptionRestoreSucceeded {
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
	isolateOffboardKeychains(t)
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

	restored, restoreErr := restoreSubscriptionLoginOnOffboard(
		client, agent, detailWithModelForTest("model-1"), &output,
	)
	if restoreErr != nil || restored != subscriptionRestoreSucceeded {
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

func TestRestoreSubscriptionLoginRejectsFailedRequiredExport(t *testing.T) {
	isolateOffboardKeychains(t)
	home := testenv.SetHome(t, t.TempDir())

	server := exportServerForTest(t, http.StatusBadRequest, exportedModelCredential{})
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "test-token")
	agent := AgentConfig{Name: "Claude Code"}
	var output bytes.Buffer

	restored, restoreErr := restoreSubscriptionLoginOnOffboard(
		client, agent, detailWithModelForTest("model-1"), &output,
	)
	if restoreErr == nil || restored != subscriptionRestoreFailed {
		t.Fatal("expected required export failure")
	}
	if _, err := os.Stat(filepath.Join(home, ".claude", ".credentials.json")); !os.IsNotExist(err) {
		t.Errorf("credential file should not have been created, stat err: %v", err)
	}
}

func TestRestoreSubscriptionLoginRejectsMismatchedCredentialType(t *testing.T) {
	isolateOffboardKeychains(t)
	home := testenv.SetHome(t, t.TempDir())

	// A Codex bundle must never be written into Claude Code's store.
	server := exportServerForTest(t, http.StatusOK, exportedModelCredential{
		CredentialType: openaiCodexOAuthCredentialType,
		Access:         "codex-access",
	}, anthropicClaudeCodeOAuthCredentialType)
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "test-token")
	agent := AgentConfig{Name: "Claude Code"}
	var output bytes.Buffer

	restored, restoreErr := restoreSubscriptionLoginOnOffboard(
		client, agent, detailWithModelForTest("model-1"), &output,
	)
	if restoreErr == nil || restored != subscriptionRestoreFailed {
		t.Fatal("expected mismatched credential type to be rejected")
	}
	if _, err := os.Stat(filepath.Join(home, ".claude", ".credentials.json")); !os.IsNotExist(err) {
		t.Errorf("credential file should not have been created, stat err: %v", err)
	}
}

func TestRestoreSubscriptionLoginNeedsDetailAndAuth(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	var output bytes.Buffer

	if status, err := restoreSubscriptionLoginOnOffboard(nil, AgentConfig{Name: "Claude Code"}, detailWithModelForTest("m"), &output); status != subscriptionRestoreFailed || err == nil {
		t.Error("nil client must not restore")
	}
	client := api.NewClientWithToken("http://127.0.0.1:0", "token")
	if status, err := restoreSubscriptionLoginOnOffboard(client, AgentConfig{Name: "Claude Code"}, nil, &output); status != subscriptionRestoreFailed || err == nil {
		t.Error("nil detail must not restore")
	}
	if status, err := restoreSubscriptionLoginOnOffboard(client, AgentConfig{Name: "OpenCode"}, detailWithModelForTest("m"), &output); status != subscriptionRestoreNotApplicable || err != nil {
		t.Error("non-subscription agents must not restore")
	}
}

func TestClaudeOffboardKeychainPreservesMetadataAndRequiresWrite(t *testing.T) {
	isolateOffboardKeychains(t)
	home := testenv.SetTempHome(t)
	filePath := filepath.Join(home, ".claude", ".credentials.json")
	mustWriteRecoveryFile(t, filePath, `{"unrelated_file":"keep"}`)
	readClaudeOffboardKeychain = func() (string, error) {
		return `{"claudeAiOauth":{"subscriptionType":"max","scopes":["user:inference"],"refreshToken":"stale"},"other":"keep"}`, nil
	}
	oldWrite := writeClaudeOffboardKeychain
	t.Cleanup(func() { writeClaudeOffboardKeychain = oldWrite })
	bundle := exportedModelCredential{Access: "synthetic-access", Refresh: "synthetic-refresh", Expires: 1900000000000}
	writeClaudeOffboardKeychain = func(string) error { return fmt.Errorf("synthetic write failure") }
	if _, err := writeClaudeSubscriptionCredential(bundle); err == nil {
		t.Fatal("active store failure must fail recovery")
	}
	data, _ := os.ReadFile(filePath)
	if string(data) != `{"unrelated_file":"keep"}` {
		t.Error("fallback file changed after active keychain failure")
	}
	var written string
	writeClaudeOffboardKeychain = func(blob string) error { written = blob; return nil }
	path, err := writeClaudeSubscriptionCredential(bundle)
	if err != nil || path != "the macOS Keychain" {
		t.Fatalf("active keychain not restored: %s %v", path, err)
	}
	for _, value := range []string{"synthetic-refresh", "subscriptionType", "max", "user:inference", `"other": "keep"`} {
		if !strings.Contains(written, value) {
			t.Errorf("keychain field lost: %s", value)
		}
	}
}

func TestOffboardCredentialFilesPreserveSymlinkMetadataAndPermissions(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Unix permissions and symlinks")
	}
	for _, kind := range []string{"claude", "codex"} {
		t.Run(kind, func(t *testing.T) {
			isolateOffboardKeychains(t)
			home := testenv.SetTempHome(t)
			t.Setenv("CODEX_HOME", filepath.Join(home, ".codex"))
			target := filepath.Join(home, "active-auth.json")
			path := filepath.Join(home, ".claude", ".credentials.json")
			existing := `{"claudeAiOauth":{"subscriptionType":"max"},"other":"preserve"}`
			if kind == "codex" {
				path = filepath.Join(home, ".codex", "auth.json")
				existing = `{"tokens":{"id_token":"preserve"},"auth_mode":"chatgpt","other":"preserve"}`
			}
			mustWriteRecoveryFile(t, target, existing)
			if err := os.Chmod(target, 0400); err != nil {
				t.Fatal(err)
			}
			if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(target, path); err != nil {
				t.Fatal(err)
			}
			bundle := exportedModelCredential{Access: "synthetic-access", Refresh: "synthetic-refresh", Expires: 1900000000000, AccountID: "synthetic-account"}
			var err error
			if kind == "claude" {
				_, err = writeClaudeSubscriptionCredential(bundle)
			} else {
				_, err = writeCodexSubscriptionCredential(bundle)
			}
			if err != nil {
				t.Fatal(err)
			}
			info, err := os.Lstat(path)
			if err != nil || info.Mode()&os.ModeSymlink == 0 {
				t.Error("credential symlink replaced")
			}
			info, err = os.Stat(target)
			if err != nil || info.Mode().Perm() != 0400 {
				t.Errorf("restrictive permissions lost: %v", err)
			}
			data, _ := os.ReadFile(target)
			for _, value := range []string{"synthetic-access", "synthetic-refresh", "preserve"} {
				if !strings.Contains(string(data), value) {
					t.Errorf("credential field lost: %s", value)
				}
			}
		})
	}
}

func TestOffboardCredentialAtomicRenameFailurePreservesDestination(t *testing.T) {
	path := filepath.Join(t.TempDir(), "auth.json")
	if err := os.Mkdir(path, 0700); err != nil {
		t.Fatal(err)
	}
	if err := writeOffboardCredentialFile(path, []byte(`{"synthetic":"credential"}`)); err == nil {
		t.Fatal("expected rename failure")
	}
	if info, err := os.Stat(path); err != nil || !info.IsDir() {
		t.Fatal("failed rename removed existing destination")
	}
	entries, err := os.ReadDir(filepath.Dir(path))
	if err != nil || len(entries) != 1 {
		t.Fatalf("temporary credential leaked: %v %v", entries, err)
	}
}

func TestClaudeOffboardKeychainUpdatesExistingAccount(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("synthetic security shell fixture")
	}
	dir := t.TempDir()
	logPath := filepath.Join(dir, "security-call")
	t.Setenv("PRELOOP_TEST_SECURITY_LOG", logPath)
	t.Setenv("USER", "synthetic-other-user")
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	script := `#!/bin/sh
case "$1" in
 find-generic-password)
  printf '"acct"<blob>="synthetic-active-account"\n' >&2
  ;;
 add-generic-password)
  printf '%s\n' "$@" > "$PRELOOP_TEST_SECURITY_LOG"
  ;;
 *) exit 1 ;;
esac
`
	if err := os.WriteFile(filepath.Join(dir, "security"), []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	if err := defaultWriteClaudeOffboardKeychain(`{"synthetic":"bundle"}`); err != nil {
		t.Fatal(err)
	}
	args, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(args), "-a\nsynthetic-active-account\n") || strings.Contains(string(args), "synthetic-other-user") {
		t.Error("write did not target the existing active account")
	}
}
