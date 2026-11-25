package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
)

func TestExpiredClaudeSubscriptionStopsBeforeEnrollment(t *testing.T) {
	home := setPreflightTestEnv(t)
	path := filepath.Join(home, ".claude", "settings.json")
	writePreflightFixture(t, path, `{}`)
	writePreflightFixture(t, filepath.Join(home, ".claude", ".credentials.json"), `{"claudeAiOauth":{"accessToken":"sk-ant-oat01-synthetic","refreshToken":"synthetic","expiresAt":1700000000000}}`)
	state, detail := detectClaudeCodeAuthState()
	if state == agentAuthStateReady || !strings.Contains(detail, "claude auth login --claudeai") {
		t.Fatalf("expired login was blessed: %s, %s", state, detail)
	}
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls++; w.WriteHeader(500) }))
	defer server.Close()
	client, err := api.NewClient("synthetic", server.URL)
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("PRELOOP_URL", server.URL)
	err = executeManagedEnrollment(AgentConfig{Name: "Claude Code", ConfigPath: path}, managedEnrollmentOptions{Client: client, AutoApprove: true})
	if err == nil || !strings.Contains(err.Error(), "claude auth login --claudeai") || calls != 0 {
		t.Fatalf("expected local rejection before requests; calls=%d err=%v", calls, err)
	}
	data, err := os.ReadFile(path)
	if err != nil || string(data) != `{}` {
		t.Fatal("configuration changed before preflight")
	}
}

func TestClaudeSubscriptionPreflightAcceptsFreshLogin(t *testing.T) {
	home := setPreflightTestEnv(t)
	path := filepath.Join(home, ".claude", "settings.json")
	writePreflightFixture(t, path, `{}`)
	data, _ := json.Marshal(map[string]interface{}{"claudeAiOauth": map[string]interface{}{"accessToken": "sk-ant-oat01-synthetic", "refreshToken": "synthetic", "expiresAt": time.Now().Add(time.Hour).UnixMilli()}})
	writePreflightFixture(t, filepath.Join(home, ".claude", ".credentials.json"), string(data))
	if err := preflightClaudeSubscriptionLogin(AgentConfig{Name: "Claude Code", ConfigPath: path}, managedEnrollmentOptions{}); err != nil {
		t.Fatal(err)
	}
}

func TestUnpinnedClaudeGatewayCanRunValidation(t *testing.T) {
	setPreflightTestEnv(t)
	agent := AgentConfig{Name: "Claude Code"}
	document := map[string]interface{}{"env": map[string]interface{}{
		"ANTHROPIC_BASE_URL": "https://example.com/anthropic", "ANTHROPIC_API_KEY": "synthetic",
	}}
	result := managedMCPAdapterForAgent(agent).ValidateManagedConfig(document, "https://example.com")
	if reason := missingManagedGatewayValidationPrerequisite(result); reason != "" {
		t.Fatal(reason)
	}
}

func TestClaudeOffboardPreflightHonorsRecordedFileWithLockedKeychain(t *testing.T) {
	home := setPreflightTestEnv(t)
	isolateOffboardKeychains(t)
	readClaudeOffboardKeychain = func() (string, error) { t.Fatal("recorded file login must not read Keychain"); return "", nil }
	agent := AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}
	path, err := localEnrollmentStatePath(agent.Name, agent.ConfigPath)
	if err != nil {
		t.Fatal(err)
	}
	writePreflightFixture(t, path, `{"claude_oauth_credential_source":"file"}`)
	writePreflightFixture(t, filepath.Join(home, ".claude", ".credentials.json"), `{"claudeAiOauth":{"accessToken":"sk-ant-oat01-synthetic"}}`)
	server := exportServerForTest(t, http.StatusOK, exportedModelCredential{CredentialType: anthropicClaudeCodeOAuthCredentialType})
	defer server.Close()
	client, err := api.NewClient("synthetic", server.URL)
	if err != nil {
		t.Fatal(err)
	}
	if err := preflightClaudeOffboardStore(client, agent, detailWithModelForTest("model-1")); err != nil {
		t.Fatal(err)
	}
}

func TestClaudeOffboardPreflightRefusesAmbiguousStoreBeforeExport(t *testing.T) {
	setPreflightTestEnv(t)
	isolateOffboardKeychains(t)
	readClaudeOffboardKeychain = func() (string, error) { return "", os.ErrPermission }
	writes := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writes++
		}
		_ = json.NewEncoder(w).Encode([]aiModelResponse{{ID: "model-1", CredentialType: anthropicClaudeCodeOAuthCredentialType}})
	}))
	defer server.Close()
	client, err := api.NewClient("synthetic", server.URL)
	if err != nil {
		t.Fatal(err)
	}
	err = preflightClaudeOffboardStore(client, AgentConfig{Name: "Claude Code"}, detailWithModelForTest("model-1"))
	if err == nil || !strings.Contains(err.Error(), "No server credentials were revoked") || writes != 0 {
		t.Fatalf("writes=%d err=%v", writes, err)
	}
}

func TestClaudeOffboardIdenticalFileIsNotRewritten(t *testing.T) {
	home := setPreflightTestEnv(t)
	isolateOffboardKeychains(t)
	path := filepath.Join(home, ".claude", ".credentials.json")
	original := `{"claudeAiOauth":{"accessToken":"synthetic-access","refreshToken":"synthetic-refresh","expiresAt":4102444800000}}`
	writePreflightFixture(t, path, original)
	writeOffboardCredentialStoreFile = func(string, []byte) error { t.Fatal("identical grant must not be rewritten"); return nil }
	_, err := writeClaudeSubscriptionCredential(exportedModelCredential{Access: "synthetic-access", Refresh: "synthetic-refresh", Expires: 4102444800000})
	if err != nil {
		t.Fatal(err)
	}
}

func TestFailedSubscriptionProbeReportsRepairWithoutSuccess(t *testing.T) {
	result := deferredLiveValidationResult{Agent: AgentConfig{Name: "Claude Code"}, Outcome: &managedLiveValidationOutcome{Attempted: true, ValidationResult: map[string]interface{}{"live_validation_status": "credential_repair_required"}}, Err: errors.New("Model credentials could not be refreshed: invalid_grant")}
	var output bytes.Buffer
	printDeferredLiveValidationLine(&output, result)
	if !strings.Contains(output.String(), "invalid_grant") || !strings.Contains(output.String(), `preloop agents reconnect "Claude Code" --from-local`) || strings.Contains(output.String(), "Onboarded") || strings.Contains(output.String(), "ANTHROPIC_API_KEY") {
		t.Fatal(output.String())
	}
	if !liveValidationOutcomeKeepsGatewayConfig(result.Outcome) || liveValidationOutcomeGatewayVerified(result.Outcome) {
		t.Fatal("repair must preserve the route without marking it verified")
	}
	outcomes := []agentOnboardingOutcome{{Agent: result.Agent, Status: agentOnboardingStatusOnboarded, Reason: "onboarding completed"}}
	applyLiveValidationOutcomesToSummary(outcomes, []deferredLiveValidationResult{result})
	if outcomes[0].Status == agentOnboardingStatusOnboarded || strings.Contains(outcomes[0].Reason, "onboarding completed") {
		t.Fatalf("misleading summary: %#v", outcomes[0])
	}
}

func TestSkippedGatewayProbeDoesNotBlessEnrollment(t *testing.T) {
	result := deferredLiveValidationResult{Agent: AgentConfig{Name: "Claude Code"}, Outcome: &managedLiveValidationOutcome{ValidationResult: map[string]interface{}{"live_validation_skip_reason": "gateway token missing"}}}
	outcomes := []agentOnboardingOutcome{{Agent: result.Agent, Status: agentOnboardingStatusOnboarded}}
	applyLiveValidationOutcomesToSummary(outcomes, []deferredLiveValidationResult{result})
	if outcomes[0].Status == agentOnboardingStatusOnboarded || !strings.Contains(outcomes[0].Reason, "gateway token missing") || deferredLiveValidationPersistStatus(result.Outcome) == "validated" {
		t.Fatalf("skipped probe was blessed: %#v", outcomes)
	}
}

func TestReonboardFailedAccountClaudeSelectionRetainsLocalRepair(t *testing.T) {
	home := setPreflightTestEnv(t)
	writePreflightFixture(t, filepath.Join(home, ".claude", ".credentials.json"), `{"claudeAiOauth":{"accessToken":"sk-ant-oat01-synthetic","refreshToken":"synthetic-fresh","expiresAt":4102444800000}}`)
	for _, status := range []string{"active", "error", "revoked"} {
		for _, noImport := range []bool{false, true} {
			selected := &aiModelResponse{ProviderName: "anthropic", ModelIdentifier: "claude-haiku-5-5", CredentialType: anthropicClaudeCodeOAuthCredentialType, CredentialsStatus: status, HasAPIKey: true}
			result := enrollmentSelectedUpstream(AgentConfig{Name: "Claude Code"}, nil, "anthropic/claude-haiku-5-5", selected, managedEnrollmentOptions{NoCredentialImport: noImport})
			want := status != "active" && !noImport
			if (len(result.CredentialPayload) > 0) != want {
				t.Fatalf("status=%s noImport=%v did not preserve repair/healthy custody", status, noImport)
			}
			if want && validateCredentialCustody([]credentialCustody{gatewayCredentialCustody(result)}, managedEnrollmentOptions{AutoApprove: true}) == nil {
				t.Fatal("repair bypassed subscription-transfer consent")
			}
		}
	}
}

func TestClaudeOffboardFilePreflightCanRecreateMissingLogin(t *testing.T) {
	home := setPreflightTestEnv(t)
	if err := preflightClaudeCredentialFile(); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(home, ".claude", ".credentials.json")
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("preflight wrote the credential file")
	}
	writePreflightFixture(t, path, "malformed")
	if err := preflightClaudeCredentialFile(); err == nil {
		t.Fatal("malformed existing credential document must stop before export")
	}
}

func TestExpiredClaudeCacheDoesNotBlockUnrelatedCredentialSelection(t *testing.T) {
	home := setPreflightTestEnv(t)
	path := filepath.Join(home, ".claude", "settings.json")
	writePreflightFixture(t, path, `{}`)
	writePreflightFixture(t, filepath.Join(home, ".claude", ".credentials.json"), `{"claudeAiOauth":{"accessToken":"sk-ant-oat01-synthetic","refreshToken":"synthetic","expiresAt":1700000000000}}`)
	agent := AgentConfig{Name: "Claude Code", ConfigPath: path}
	if err := preflightClaudeSubscriptionLogin(agent, managedEnrollmentOptions{NoCredentialImport: true}); err != nil {
		t.Fatalf("no-import selection was blocked by an unused cache: %v", err)
	}
	writePreflightFixture(t, filepath.Join(home, ".claude.json"), `{"primaryApiKey":"synthetic-api-key"}`)
	if err := preflightClaudeSubscriptionLogin(agent, managedEnrollmentOptions{}); err != nil {
		t.Fatalf("configured API key was blocked by an unused cache: %v", err)
	}
	if state, detail := detectClaudeCodeAuthState(); state != agentAuthStateReady {
		t.Fatalf("configured API key should determine readiness: %s %s", state, detail)
	}
}
