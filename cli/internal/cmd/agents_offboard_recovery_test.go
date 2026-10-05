package cmd

import (
	"encoding/json"
	"errors"
	"fmt"
	"github.com/preloop/preloop/cli/internal/api"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// The gateway fixture rotates a synthetic lineage after enrollment.
type offboardRecoveryFixture struct {
	// mu guards fields the httptest handler writes while the test reads them.
	// Hijacking the export connection does not synchronize those accesses.
	// Every access to the fields below must hold mu while a request may
	// still be in flight.
	mu sync.Mutex

	agent                                      AgentConfig
	home, config, exportBody                   string
	exportStatus, archiveStatus, cleanupStatus int
	archives, deletes, exports                 int
	events                                     []string
	dropExport                                 bool
	modelDeleted                               bool
	checkInitialConfig                         bool
	credentialType, storePath                  string
}

func newOffboardRecoveryFixture(t *testing.T, kinds ...string) *offboardRecoveryFixture {
	t.Helper()
	isolateOffboardKeychains(t)
	home := testenv.SetTempHome(t)
	t.Setenv("CODEX_HOME", filepath.Join(home, ".codex"))
	f := &offboardRecoveryFixture{home: home, exportStatus: 200, archiveStatus: 200, cleanupStatus: 200, checkInitialConfig: true,
		exportBody: `{"credential_type":"oauth_openai_codex","access":"synthetic-live-access","refresh":"synthetic-rotated-refresh","expires":1900000000000,"account_id":"synthetic-account"}`}
	f.agent = normalizeDiscoveredAgent(AgentConfig{Name: "Codex CLI", ConfigPath: filepath.Join(home, "config.toml")})
	f.config = "model = \"managed-model\"\n"
	f.credentialType = openaiCodexOAuthCredentialType
	f.storePath = filepath.Join(home, ".codex", "auth.json")
	if len(kinds) > 0 && kinds[0] == "claude" {
		f.agent = normalizeDiscoveredAgent(AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")})
		f.config = `{"env":{"ANTHROPIC_API_KEY":"synthetic-gateway"},"hooks":{"PreToolUse":[{"hooks":[{"type":"command","command":"preloop approval-hook"}]}]}}`
		f.credentialType = anthropicClaudeCodeOAuthCredentialType
		f.storePath = filepath.Join(home, ".claude", ".credentials.json")
		f.exportBody = `{"credential_type":"oauth_anthropic_claude_code","access":"synthetic-live-access","refresh":"synthetic-rotated-refresh","expires":1900000000000}`
		mustWriteRecoveryFile(t, filepath.Join(home, ".claude.json"), `{"customApiKeyResponses":{"approved":["synthetic-key-suffix"]},"other":"preserve"}`)
	}
	mustWriteRecoveryFile(t, f.agent.ConfigPath, f.config)
	backup := filepath.Join(home, "backup.toml")
	mustWriteRecoveryFile(t, backup, "model = \"original-model\"\n")
	mustWriteRecoveryFile(t, filepath.Join(home, ".codex", "auth.json"), `{"tokens":{"refresh_token":"synthetic-stale-refresh","id_token":"preserve"},"auth_mode":"chatgpt"}`)
	state := &localEnrollmentState{AgentName: f.agent.Name, ConfigPath: f.agent.ConfigPath, BackupPath: backup, ConfigExisted: true, RuntimePrincipalID: runtimePrincipalIDForAgent(f.agent)}
	if err := saveLocalEnrollmentState(state); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		f.events = append(f.events, r.Method+" "+r.URL.Path)
		detail := *detailWithModelForTest("model-1")
		detail.Agent.SessionSourceID = runtimePrincipalIDForAgent(f.agent)
		detail.Agent.SessionSourceType = "codex"
		if isClaudeCodeAgent(f.agent) {
			detail.Agent.SessionSourceType = "claude_code"
		}
		detail.Agent.LatestModelAlias = "managed-model"
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/credentials/export"):
			f.exports++
			if f.dropExport {
				conn, _, err := w.(http.Hijacker).Hijack()
				if err != nil {
					t.Error(err)
					return
				}
				_ = conn.Close()
				return
			}
			data, _ := os.ReadFile(f.agent.ConfigPath)
			if f.checkInitialConfig && string(data) != f.config {
				t.Error("config restored before export")
			}
			w.WriteHeader(f.exportStatus)
			_, _ = fmt.Fprint(w, f.exportBody)
		case r.Method == http.MethodPatch:
			f.archives++
			data, _ := os.ReadFile(f.storePath)
			if !strings.Contains(string(data), "synthetic-rotated-refresh") {
				t.Error("archived before active login restored")
			}
			w.WriteHeader(f.archiveStatus)
			_ = json.NewEncoder(w).Encode(detail.Agent)
		case r.Method == http.MethodDelete:
			f.deletes++
			f.modelDeleted = true
			_ = json.NewEncoder(w).Encode(map[string]any{})
		case r.URL.Path == "/api/v1/agents":
			_ = json.NewEncoder(w).Encode(managedAgentListResponse{Items: []managedAgentSummary{detail.Agent}})
		case r.URL.Path == "/api/v1/agents/agent-1":
			_ = json.NewEncoder(w).Encode(detail)
		case r.URL.Path == "/api/v1/ai-models":
			models := []aiModelResponse{}
			if !f.modelDeleted {
				models = append(models, aiModelResponse{ID: "model-1", CredentialType: f.credentialType, MetaData: map[string]any{"gateway": map[string]any{"model_alias": "managed-model"}}})
			}
			_ = json.NewEncoder(w).Encode(models)
		case r.URL.Path == "/api/v1/mcp-servers":
			w.WriteHeader(f.cleanupStatus)
			_ = json.NewEncoder(w).Encode([]any{})
		case r.URL.Path == "/api/v1/flows":
			_ = json.NewEncoder(w).Encode([]any{})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	oldURL, oldToken := FlagURL, FlagToken
	FlagURL, FlagToken = server.URL, "synthetic-session"
	t.Cleanup(func() { FlagURL, FlagToken = oldURL, oldToken })
	return f
}

func mustWriteRecoveryFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
}

func TestExecuteOffboardRequiredRecoveryFailsBeforeMutation(t *testing.T) {
	f := newOffboardRecoveryFixture(t)
	f.exportStatus = 403
	var operationErr error
	out := captureStdout(t, func() error {
		operationErr = executeOffboard(f.agent, true, offboardCleanupYes, offboardCleanupYes)
		return nil
	})
	if operationErr == nil {
		t.Error("required recovery failure must abort offboard")
	}
	if f.archives != 0 || f.deletes != 0 {
		t.Errorf("destructive calls after recovery failure: archives=%d deletes=%d", f.archives, f.deletes)
	}
	data, _ := os.ReadFile(f.agent.ConfigPath)
	if string(data) != f.config {
		t.Error("config changed after recovery failure")
	}
	if _, err := loadLocalEnrollmentState(f.agent); err != nil {
		t.Errorf("retry state lost: %v", err)
	}
	if strings.Contains(out, "✓ Offboarded") {
		t.Errorf("premature success: %s", out)
	}
}

func runRecoveryOffboard(t *testing.T, f *offboardRecoveryFixture) (string, error) {
	t.Helper()
	var operationErr error
	out := captureStdout(t, func() error {
		operationErr = executeOffboard(f.agent, true, offboardCleanupYes, offboardCleanupYes)
		return nil
	})
	return out, operationErr
}

func assertRecoveryUntouched(t *testing.T, f *offboardRecoveryFixture, out string, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("required recovery failure did not abort")
	}
	f.mu.Lock()
	archives, deletes, modelDeleted := f.archives, f.deletes, f.modelDeleted
	events := append([]string(nil), f.events...)
	f.mu.Unlock()
	if archives != 0 || deletes != 0 || modelDeleted {
		t.Errorf("remote mutation: %v", events)
	}
	for _, event := range events {
		if strings.Contains(event, "mcp-servers") || strings.Contains(event, "flows") {
			t.Errorf("cleanup ran: %s", event)
		}
	}
	data, _ := os.ReadFile(f.agent.ConfigPath)
	if string(data) != f.config {
		t.Error("managed config changed")
	}
	if _, err := loadLocalEnrollmentState(f.agent); err != nil {
		t.Errorf("retry state lost: %v", err)
	}
	if strings.Contains(out, "✓ Offboarded") {
		t.Error("premature success")
	}
	for _, token := range []string{"synthetic-live-access", "synthetic-rotated-refresh", "synthetic-stale-refresh"} {
		if strings.Contains(out+err.Error(), token) {
			t.Errorf("credential leaked in message")
		}
	}
}

func TestExecuteOffboardRecoveryFaultsAndRetry(t *testing.T) {
	cases := []struct {
		name  string
		setup func(*testing.T, *offboardRecoveryFixture)
	}{
		{"forbidden", func(t *testing.T, f *offboardRecoveryFixture) { f.exportStatus = 403 }},
		{"not_found", func(t *testing.T, f *offboardRecoveryFixture) { f.exportStatus = 404 }},
		{"server_failure", func(t *testing.T, f *offboardRecoveryFixture) { f.exportStatus = 503 }},
		{"network", func(t *testing.T, f *offboardRecoveryFixture) { f.dropExport = true }},
		{"empty", func(t *testing.T, f *offboardRecoveryFixture) { f.exportBody = `{}` }},
		{"malformed", func(t *testing.T, f *offboardRecoveryFixture) { f.exportBody = `{"access":"synthetic-live-access",` }},
		{"missing_refresh", func(t *testing.T, f *offboardRecoveryFixture) {
			f.exportBody = `{"credential_type":"oauth_openai_codex","access":"synthetic-live-access","expires":1900000000000,"account_id":"synthetic-account"}`
		}},
		{"missing_expires", func(t *testing.T, f *offboardRecoveryFixture) {
			f.exportBody = `{"credential_type":"oauth_openai_codex","access":"synthetic-live-access","refresh":"synthetic-rotated-refresh","account_id":"synthetic-account"}`
		}},
		{"file_read", func(t *testing.T, f *offboardRecoveryFixture) {
			path := filepath.Join(f.home, ".codex", "auth.json")
			if err := os.Remove(path); err != nil {
				t.Fatal(err)
			}
			if err := os.Mkdir(path, 0700); err != nil {
				t.Fatal(err)
			}
		}},
		{"file_write", func(t *testing.T, f *offboardRecoveryFixture) {
			writeOffboardCredentialStoreFile = func(string, []byte) error { return errors.New("synthetic atomic store write failure") }
		}},
		{"active_keychain_write", func(t *testing.T, f *offboardRecoveryFixture) {
			readCodexOffboardKeychain = func() (string, error) { return `{"tokens":{"id_token":"preserve"}}`, nil }
			previous := writeCodexKeychainBlobForSync
			writeCodexKeychainBlobForSync = func(string) error { return errors.New("synthetic keychain locked") }
			t.Cleanup(func() { writeCodexKeychainBlobForSync = previous })
		}},
		{"active_keychain_read", func(t *testing.T, f *offboardRecoveryFixture) {
			readCodexOffboardKeychain = func() (string, error) { return "", errors.New("synthetic keychain locked") }
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newOffboardRecoveryFixture(t)
			tc.setup(t, f)
			out, err := runRecoveryOffboard(t, f)
			assertRecoveryUntouched(t, f, out, err)
			// Clear only the injected fault: the remote live lineage and local
			// enrollment survived the failed operation. The network case's
			// handler can still be reading these fields when the client returns.
			f.mu.Lock()
			f.exportStatus = 200
			f.dropExport = false
			f.exportBody = `{"credential_type":"oauth_openai_codex","access":"synthetic-live-access","refresh":"synthetic-rotated-refresh","expires":1900000000000,"account_id":"synthetic-account"}`
			f.mu.Unlock()
			readCodexOffboardKeychain = func() (string, error) { return "", nil }
			writeOffboardCredentialStoreFile = writeOffboardCredentialFile
			authPath := filepath.Join(f.home, ".codex", "auth.json")
			if info, err := os.Stat(authPath); err == nil && info.IsDir() {
				if err := os.Remove(authPath); err != nil {
					t.Fatal(err)
				}
			}
			out, err = runRecoveryOffboard(t, f)
			if err != nil {
				t.Fatalf("retry: %v", err)
			}
			if f.archives != 1 || f.deletes != 1 || !strings.Contains(out, "✓ Offboarded") {
				t.Errorf("retry incomplete: archives=%d deletes=%d output=%s", f.archives, f.deletes, out)
			}
			if _, err := loadLocalEnrollmentState(f.agent); err == nil {
				t.Error("successful retry kept enrollment state")
			}
			data, _ := os.ReadFile(authPath)
			if !strings.Contains(string(data), "synthetic-rotated-refresh") {
				t.Error("latest lineage not restored")
			}
		})
	}
}

func TestExecuteOffboardPartialCleanupRetainsCheckpoint(t *testing.T) {
	f := newOffboardRecoveryFixture(t)
	f.cleanupStatus = 503
	out, err := runRecoveryOffboard(t, f)
	if err == nil || !strings.Contains(err.Error(), "partially completed") {
		t.Fatalf("expected partial failure: %v", err)
	}
	if strings.Contains(out, "✓ Offboarded") {
		t.Error("premature success")
	}
	state, err := loadLocalEnrollmentState(f.agent)
	if err != nil || !state.OffboardArchived {
		t.Fatalf("retry checkpoint missing: %v", err)
	}
	if f.archives != 1 || f.exports != 1 || f.deletes != 0 {
		t.Errorf("unexpected progress: %v", f.events)
	}
	f.cleanupStatus = 200
	// A model already removed during a partial cleanup must not require export
	// or re-archival on retry.
	f.modelDeleted = true
	out, err = runRecoveryOffboard(t, f)
	if err != nil {
		t.Fatal(err)
	}
	if f.archives != 1 || f.exports != 1 || !strings.Contains(out, "✓ Offboarded") {
		t.Errorf("retry repeated completed steps: %v", f.events)
	}
}

func TestRestoreSubscriptionBindingsCompatibilityAndMultiplicity(t *testing.T) {
	for _, tc := range []struct {
		name, agentName string
		models          []aiModelResponse
		bindings        []managedAgentModelBindingSummary
		failed          bool
	}{
		{name: "api_key", agentName: "Claude Code", models: []aiModelResponse{{ID: "m", CredentialType: "api_key"}}, bindings: []managedAgentModelBindingSummary{{AIModelID: "m"}}},
		{name: "unrelated", agentName: "Claude Code", models: []aiModelResponse{{ID: "m", CredentialType: openaiCodexOAuthCredentialType}}, bindings: []managedAgentModelBindingSummary{{AIModelID: "m"}}},
		{name: "no_bindings", agentName: "Claude Code"},
		{name: "ordinary_agent", agentName: "OpenCode", bindings: []managedAgentModelBindingSummary{{AIModelID: "m"}}},
		{name: "missing_model", agentName: "Claude Code", bindings: []managedAgentModelBindingSummary{{AIModelID: "missing"}}, failed: true},
		{name: "multiple", agentName: "Claude Code", models: []aiModelResponse{{ID: "m", CredentialType: anthropicClaudeCodeOAuthCredentialType}, {ID: "n", CredentialType: anthropicClaudeCodeOAuthCredentialType}}, bindings: []managedAgentModelBindingSummary{{AIModelID: "m"}, {AIModelID: "n"}}, failed: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			isolateOffboardKeychains(t)
			testenv.SetTempHome(t)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet || r.URL.Path != "/api/v1/ai-models" {
					t.Errorf("unexpected export or mutation: %s %s", r.Method, r.URL.Path)
					http.NotFound(w, r)
					return
				}
				_ = json.NewEncoder(w).Encode(tc.models)
			}))
			defer server.Close()
			detail := &managedAgentDetailResponse{Agent: managedAgentSummary{ConfiguredModels: tc.bindings}}
			var output strings.Builder
			status, err := restoreSubscriptionLoginOnOffboard(api.NewClientWithToken(server.URL, "synthetic"), AgentConfig{Name: tc.agentName}, detail, &output)
			if tc.failed {
				if err == nil || status != subscriptionRestoreFailed {
					t.Errorf("expected required failure: %s %v", status, err)
				}
			} else if err != nil || status != subscriptionRestoreNotApplicable {
				t.Errorf("expected compatibility: %s %v", status, err)
			}
		})
	}
}

func TestExecuteOffboardUnmatchedSubscriptionAgentKeepsLocalPath(t *testing.T) {
	isolateOffboardKeychains(t)
	home := testenv.SetTempHome(t)
	stubClaudeBinary(t)
	configPath := filepath.Join(home, ".claude", "settings.json")
	backupPath := filepath.Join(home, "backup.json")
	mustWriteRecoveryFile(t, configPath, `{"env":{"ANTHROPIC_API_KEY":"synthetic-gateway"}}`)
	backup := `{"model":"original"}`
	mustWriteRecoveryFile(t, backupPath, backup)
	agent := normalizeDiscoveredAgent(AgentConfig{Name: "Claude Code", ConfigPath: configPath})
	if err := saveLocalEnrollmentState(&localEnrollmentState{
		AgentName:          agent.Name,
		ConfigPath:         configPath,
		BackupPath:         backupPath,
		ConfigExisted:      true,
		RuntimePrincipalID: runtimePrincipalIDForAgent(agent),
	}); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/agents" {
			t.Errorf("unmatched install must not touch remote credentials: %s %s", r.Method, r.URL.Path)
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(managedAgentListResponse{Items: nil})
	}))
	defer server.Close()
	oldURL, oldToken := FlagURL, FlagToken
	FlagURL, FlagToken = server.URL, "synthetic-session"
	t.Cleanup(func() { FlagURL, FlagToken = oldURL, oldToken })

	out := captureStdout(t, func() error {
		return executeOffboard(agent, true, offboardCleanupNo, offboardCleanupNo)
	})
	if !strings.Contains(out, "Could not match a managed record for this install") {
		t.Fatalf("expected lookup warning, got:\n%s", out)
	}
	if !strings.Contains(out, "✓ Offboarded") {
		t.Fatalf("local-only offboard did not finish:\n%s", out)
	}
	data, err := os.ReadFile(configPath)
	if err != nil || string(data) != backup {
		t.Fatalf("backup not restored: %s %v", data, err)
	}
	if _, err := loadLocalEnrollmentState(agent); err == nil {
		t.Error("local enrollment state retained")
	}
}

func TestExecuteOffboardClaudeApprovalFailureContinues(t *testing.T) {
	f := newOffboardRecoveryFixture(t, "claude")
	stubClaudeBinary(t)
	backup := "{\"model\":\"original\"}\n"
	if err := os.WriteFile(filepath.Join(f.home, "backup.toml"), []byte(backup), 0600); err != nil {
		t.Fatal(err)
	}
	approvalPath := filepath.Join(f.home, ".claude.json")
	if err := os.WriteFile(approvalPath, []byte("{not json"), 0600); err != nil {
		t.Fatal(err)
	}
	out, err := runRecoveryOffboard(t, f)
	if err != nil {
		t.Fatalf("malformed user config must not block offboard: %v", err)
	}
	if !strings.Contains(out, "Warning: could not remove the gateway key approval") {
		t.Fatalf("expected approval warning, got:\n%s", out)
	}
	if !strings.Contains(out, "✓ Offboarded") {
		t.Fatalf("offboard did not finish:\n%s", out)
	}
	data, err := os.ReadFile(approvalPath)
	if err != nil || string(data) != "{not json" {
		t.Fatalf("malformed claude.json changed: %s %v", data, err)
	}
	restored, err := os.ReadFile(f.agent.ConfigPath)
	if err != nil || string(restored) != backup {
		t.Fatalf("config not restored after approval warning: %s %v", restored, err)
	}
	if _, err := loadLocalEnrollmentState(f.agent); err == nil {
		t.Error("successful offboard kept enrollment state")
	}
}

func stubClaudeBinary(t *testing.T) {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "claude"), []byte("#!/bin/sh\nexit 0\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
}

func TestExecuteOffboardClaudeActiveKeychainFailurePreservesHooksAndApproval(t *testing.T) {
	f := newOffboardRecoveryFixture(t, "claude")
	readClaudeOffboardKeychain = func() (string, error) { return `{"claudeAiOauth":{"refreshToken":"synthetic-stale-refresh"}}`, nil }
	previous := writeClaudeOffboardKeychain
	writeClaudeOffboardKeychain = func(string) error { return errors.New("synthetic keychain locked") }
	t.Cleanup(func() { writeClaudeOffboardKeychain = previous })
	approvalPath := filepath.Join(f.home, ".claude.json")
	original, _ := os.ReadFile(approvalPath)
	out, err := runRecoveryOffboard(t, f)
	assertRecoveryUntouched(t, f, out, err)
	data, _ := os.ReadFile(approvalPath)
	if string(data) != string(original) {
		t.Error("gateway approval removed before recovery")
	}
	if !strings.Contains(f.config, "PreToolUse") {
		t.Fatal("hook fixture missing")
	}
}

func TestExecuteOffboardArchiveFailureRetainsRetryState(t *testing.T) {
	f := newOffboardRecoveryFixture(t)
	f.archiveStatus = 503
	out, err := runRecoveryOffboard(t, f)
	if err == nil || !strings.Contains(err.Error(), "partially completed") {
		t.Fatalf("expected partial archive failure: %v", err)
	}
	if strings.Contains(out, "✓ Offboarded") {
		t.Error("premature success")
	}
	if f.deletes != 0 {
		t.Error("cleanup ran after archive failure")
	}
	state, err := loadLocalEnrollmentState(f.agent)
	if err != nil || state.OffboardArchived {
		t.Fatalf("incorrect retry state: %v", err)
	}
	f.archiveStatus = 200
	f.checkInitialConfig = false
	out, err = runRecoveryOffboard(t, f)
	if err != nil || f.archives != 2 || f.deletes != 1 || !strings.Contains(out, "✓ Offboarded") {
		t.Fatalf("retry did not complete: %v %v", err, f.events)
	}
}
