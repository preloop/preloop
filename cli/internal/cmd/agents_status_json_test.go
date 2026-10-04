package cmd

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestSafeStatusJSONAllowlist(t *testing.T) {
	secret := "synthetic-secret-never-emit"
	token := "sk-" + secret
	applied := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
	agent := AgentConfig{
		Name: "Claude Code", DisplayName: secret, RuntimePrincipalID: secret,
		ConfigPath: "/home/" + secret + "/settings.json",
		MCPServers: map[string]MCPDef{secret: {
			Command: secret, Args: []string{secret},
			URL:       "https://user:" + token + "@example.com/?token=" + token,
			Env:       map[string]string{"ARBITRARY": token},
			Headers:   map[string]string{"Authorization": "Bearer " + token, "X-Api-Key": secret},
			Auth:      map[string]interface{}{"token": token},
			Transport: "http",
		}},
		IsOnboarded: true, OnboardingState: "fully_onboarded",
		AuthState: "ready", AuthDetail: secret, SupportLevel: "full",
		RuntimeState: "present", RuntimeDetail: secret,
		ConfigDrift: true, ReonboardRecommended: true, DriftReasons: []string{secret},
	}
	local := &localEnrollmentState{
		AgentName: "Claude Code", DisplayName: secret, RuntimePrincipalID: secret, IdentitySalt: secret,
		EnrollmentID: "11111111-1111-1111-1111-111111111111",
		ConfigPath:   "/home/" + secret, BackupPath: "/home/" + secret + "/backup",
		ConfigExisted: true, ManagedServerName: "preloop",
		ManagedServerURL:    "https://example.com/mcp?token=" + token,
		ManagedControlWSURL: "wss://example.com/?token=" + token,
		AppliedAt:           applied,
		DiscoveredConfig: map[string]interface{}{
			"mcp_servers": map[string]interface{}{"preloop": map[string]interface{}{
				"env": secret, "headers": secret, "auth": map[string]interface{}{"token": token},
			}},
		},
		ManagedConfig: map[string]interface{}{"token": token, "headers": secret},
	}
	remote := &managedAgentDetailResponse{
		Agent: managedAgentSummary{
			ID: "22222222-2222-2222-2222-222222222222", DisplayName: secret,
			SessionSourceID: secret, SessionReference: "/home/" + secret,
			EnrollmentHostname: secret, IdentityDerivation: secret,
			LifecycleState: "active", ActivityStatus: "idle",
			OnboardingState: "fully_onboarded", LatestModelAlias: "openai/gpt-5.4",
			ManagedMCPServers:      []string{"preloop", "Not A Server"},
			ModelGatewayConfigured: true, MCPProxyConfigured: true, TotalRequests: 4,
		},
		Credentials: []managedAgentCredentialSummary{{
			ID: "44444444-4444-4444-4444-444444444444", APIKeyID: secret, Name: secret,
			Status: "active", KeyPrefix: token, CreatedAt: "2026-01-02T03:04:05Z",
			RevokedReason: secret,
		}},
		Enrollments: []managedAgentEnrollmentSummary{{
			ID: "33333333-3333-3333-3333-333333333333", EnrollmentType: "cli_managed_config",
			AdapterKey: "claude_code", Status: "validated",
			TargetConfigPath: "/home/" + secret, RestoreAvailable: true,
			DiscoveredConfig: map[string]interface{}{"env": token, "headers": secret, "auth": token},
			ManagedConfig:    map[string]interface{}{"token": token},
			BackupMetadata:   map[string]interface{}{"config_path": "/home/" + secret},
			CreatedAt:        "2026-01-02T03:04:05Z",
			ValidationResult: map[string]interface{}{
				"validation_passed": true, "live_validation_status": "passed",
				"live_validation_model_alias": "openai/gpt-5.4",
				"control_plugin_verified":     true, "control_channel_configured": true,
				"live_validation_token": token, "live_validation_error": secret,
				"expected_preloop_url": "https://example.com/?token=" + token,
				"config_path":          "/home/" + secret,
				"headers":              map[string]interface{}{"Authorization": "Bearer " + token},
				"auth":                 map[string]interface{}{"token": token},
				"env":                  map[string]interface{}{"ARBITRARY": token},
				"discovered_config":    map[string]interface{}{"token": token},
				"managed_config":       map[string]interface{}{"token": token},
			},
		}},
	}
	models := []aiModelResponse{{
		ID: "55555555-5555-5555-5555-555555555555", Name: "Claude Sonnet",
		ProviderName: "Anthropic", ModelIdentifier: "claude-sonnet",
		APIEndpoint: "https://example.com/?token=" + token, MetaData: map[string]interface{}{"token": token},
		CredentialType: "oauth_anthropic_claude_code", CredentialsSecretID: secret,
		CredentialsStatus: "active", CredentialsLastError: "refresh failed token=" + token,
		HasAPIKey: true,
	}}
	desktop := map[string]interface{}{
		"installed": true, "display": ":99", "vnc_port": 5900,
		"auth": token, "browser": "/usr/bin/" + secret,
	}
	data, err := json.Marshal(safeStatusJSON(agent, local, remote, models, desktop))
	if err != nil {
		t.Fatal(err)
	}
	assertStatusJSONOmitsSecrets(t, data, secret, token)
	var payload map[string]interface{}
	if err := json.Unmarshal(data, &payload); err != nil {
		t.Fatal(err)
	}
	agentRow, _ := payload["agent"].(map[string]interface{})
	if agentRow["name"] != "Claude Code" || agentRow["app_id"] != "claude-code" ||
		agentRow["mcp_server_count"] != float64(1) || agentRow["auth_state"] != "ready" ||
		agentRow["runtime_state"] != "present" || agentRow["onboarding_state"] != "fully_onboarded" ||
		agentRow["support_level"] != "full" || agentRow["is_onboarded"] != true {
		t.Fatalf("agent allowlist lost status fields: %s", data)
	}
	localRow, _ := payload["local_state"].(map[string]interface{})
	if localRow["agent_name"] != "Claude Code" || localRow["config_existed"] != true ||
		localRow["managed_server_name"] != "preloop" ||
		localRow["enrollment_id"] != "11111111-1111-1111-1111-111111111111" {
		t.Fatalf("local status lost: %s", data)
	}
	remoteRow, _ := payload["remote_state"].(map[string]interface{})
	remoteAgent, _ := remoteRow["agent"].(map[string]interface{})
	if remoteAgent["id"] != "22222222-2222-2222-2222-222222222222" ||
		remoteAgent["lifecycle_state"] != "active" || remoteAgent["model_gateway_configured"] != true {
		t.Fatalf("remote status lost: %s", data)
	}
	enrollments, _ := remoteRow["enrollments"].([]interface{})
	validation, _ := enrollments[0].(map[string]interface{})["validation_result"].(map[string]interface{})
	if validation["validation_passed"] != true || validation["live_validation_status"] != "passed" ||
		validation["live_validation_model_alias"] != "openai/gpt-5.4" ||
		validation["control_plugin_verified"] != true || validation["control_channel_configured"] != true {
		t.Fatalf("validation health lost: %s", data)
	}
	modelRows, _ := payload["models"].([]interface{})
	modelRow, _ := modelRows[0].(map[string]interface{})
	if modelRow["name"] != "Claude Sonnet" || modelRow["model_identifier"] != "claude-sonnet" ||
		modelRow["credentials_status"] != "active" || modelRow["credential_type"] != "oauth_anthropic_claude_code" {
		t.Fatalf("model health lost: %s", data)
	}
	desktopRow, _ := payload["desktop"].(map[string]interface{})
	if desktopRow["installed"] != true || desktopRow["display"] != ":99" || desktopRow["vnc_port"] != float64(5900) {
		t.Fatalf("desktop status lost: %s", data)
	}
}

func TestAgentsStatusJSONOmitsLocalSecrets(t *testing.T) {
	secret := "synthetic-secret-never-emit"
	token := "sk-" + secret
	home := testenv.SetTempHome(t)
	t.Setenv("PATH", t.TempDir())
	t.Setenv("PRELOOP_TOKEN", "")
	t.Setenv("PRELOOP_URL", "")
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	configPath := filepath.Join(home, hermesBootstrapConfigPath)
	if err := os.MkdirAll(filepath.Dir(configPath), 0o700); err != nil {
		t.Fatal(err)
	}
	configBody := "mcp_servers:\n  preloop:\n    url: https://user:" + token +
		"@example.com/?token=" + token + "\n    headers:\n      Authorization: Bearer " + token +
		"\n    env:\n      ARBITRARY: " + token + "\n    auth:\n      token: " + token + "\n"
	if err := os.WriteFile(configPath, []byte(configBody), 0o600); err != nil {
		t.Fatal(err)
	}
	statePath, err := localEnrollmentStatePath("Hermes", configPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(statePath), 0o700); err != nil {
		t.Fatal(err)
	}
	state := localEnrollmentState{
		AgentName: "Hermes", DisplayName: secret, RuntimePrincipalID: secret, IdentitySalt: secret,
		EnrollmentID: "11111111-1111-1111-1111-111111111111",
		ConfigPath:   "/home/" + secret, ConfigExisted: true, BackupPath: "/home/" + secret,
		ManagedServerName: "preloop", ManagedServerURL: "https://example.com/?token=" + token,
		DiscoveredConfig: map[string]interface{}{"env": token, "headers": secret, "auth": token},
		ManagedConfig:    map[string]interface{}{"token": token},
		AppliedAt:        time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC),
	}
	encoded, err := json.Marshal(state)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(statePath, encoded, 0o600); err != nil {
		t.Fatal(err)
	}
	oldURL, oldToken := FlagURL, FlagToken
	FlagURL, FlagToken = "", ""
	t.Cleanup(func() { FlagURL, FlagToken = oldURL, oldToken })
	if err := agentsStatusCmd.Flags().Set("json", "true"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = agentsStatusCmd.Flags().Set("json", "false") })

	output := captureCommandStdout(t, func() error {
		return runAgentsStatus(agentsStatusCmd, []string{"Hermes"})
	})
	assertStatusJSONOmitsSecrets(t, []byte(output), secret, token)
	var payload map[string]interface{}
	if err := json.Unmarshal([]byte(output), &payload); err != nil {
		t.Fatalf("status json: %v\n%s", err, output)
	}
	agentRow, _ := payload["agent"].(map[string]interface{})
	if agentRow["name"] != "Hermes" || agentRow["app_id"] != "hermes" || agentRow["mcp_server_count"] != float64(1) {
		t.Fatalf("status identity lost: %s", output)
	}
	localRow, _ := payload["local_state"].(map[string]interface{})
	if localRow["agent_name"] != "Hermes" || localRow["config_existed"] != true ||
		localRow["managed_server_name"] != "preloop" {
		t.Fatalf("local status lost: %s", output)
	}
	if _, ok := payload["remote_state"]; !ok || payload["remote_state"] != nil {
		t.Fatalf("unauthenticated remote_state = %#v", payload["remote_state"])
	}
}

func assertStatusJSONOmitsSecrets(t *testing.T, data []byte, secret, token string) {
	t.Helper()
	text := string(data)
	for _, forbidden := range []string{secret, token, "Bearer ", "config_path", "discovered_config", "managed_server_url"} {
		if strings.Contains(text, forbidden) {
			t.Errorf("status json contains %q: %s", forbidden, text)
		}
	}
	var payload interface{}
	if err := json.Unmarshal(data, &payload); err != nil {
		t.Fatal(err)
	}
	assertNoStatusSecretKeys(t, payload)
}

func assertNoStatusSecretKeys(t *testing.T, value interface{}) {
	t.Helper()
	switch typed := value.(type) {
	case map[string]interface{}:
		for key, child := range typed {
			switch key {
			case "env", "headers", "auth", "config_path", "token", "tokens", "mcp_servers",
				"discovered_config", "managed_config", "managed_server_url", "managed_control_ws_url",
				"runtime_principal_id", "auth_detail", "runtime_detail", "drift_reasons",
				"key_prefix", "api_endpoint", "meta_data", "credentials_secret_id",
				"credentials_last_error", "live_validation_token", "live_validation_error",
				"expected_preloop_url", "backup_path", "identity_salt", "display_name",
				"target_config_path", "backup_metadata":
				t.Errorf("forbidden key %q", key)
			}
			assertNoStatusSecretKeys(t, child)
		}
	case []interface{}:
		for _, child := range typed {
			assertNoStatusSecretKeys(t, child)
		}
	}
}
