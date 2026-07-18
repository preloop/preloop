package cmd

import (
	"bytes"
	"encoding/json"
	"io"
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

func TestOpenClawConfigPathsIncludesCurrentLocations(t *testing.T) {
	home := "/tmp/preloop-home"
	t.Setenv("OPENCLAW_CONFIG_PATH", "~/custom/openclaw.json5")
	t.Setenv("OPENCLAW_HOME", "~/state/openclaw")
	t.Setenv("OPENCLAW_STATE_DIR", "")
	t.Setenv("OPENCLAW_CONFIG_DIR", "~/xdg/openclaw")

	paths := openClawConfigPaths(home)
	joined := strings.Join(paths, "\n")

	for _, want := range []string{
		filepath.Join(home, "custom", "openclaw.json5"),
		filepath.Join(home, ".openclaw", "openclaw.json"),
		filepath.Join(home, ".openclaw", "openclaw.json5"),
		filepath.Join(home, ".openclaw", "config.json"),
		filepath.Join(home, ".openclaw", "config.json5"),
		filepath.Join(home, ".config", "openclaw", "openclaw.json"),
		filepath.Join(home, ".config", "openclaw", "config.json5"),
		filepath.Join(home, "state", "openclaw", "config.json5"),
		filepath.Join(home, "xdg", "openclaw", "openclaw.json"),
	} {
		if !strings.Contains(joined, want) {
			t.Fatalf("expected %q in OpenClaw config candidates:\n%s", want, joined)
		}
	}
}

func TestParseOpenClawConfigJSON5(t *testing.T) {
	tempDir := t.TempDir()
	configPath := filepath.Join(tempDir, "openclaw.json5")
	t.Setenv("OPENAI_API_KEY", "provider-secret")

	config := `{
  // OpenClaw accepts JSON5 with comments and trailing commas.
  agents: {
    defaults: {
      model: {
        primary: "openai/gpt-5",
        fallbacks: ["openai/gpt-5.4"],
      },
    },
  },
  models: {
    providers: {
      openai: {
        baseUrl: "https://api.openai.com/v1",
        api: "openai-responses",
        apiKey: "${OPENAI_API_KEY}",
        models: [
          {
            id: "gpt-5",
            name: "GPT-5",
            contextWindow: 128000,
          },
        ],
      },
    },
  },
  mcp: {
    servers: {
      preexisting: {
        transport: "http",
        url: "https://mcp.example.com/v1",
      },
    },
  },
}`
	if err := os.WriteFile(configPath, []byte(config), 0o600); err != nil {
		t.Fatalf("failed to write test config: %v", err)
	}

	parsed, err := parseOpenClawConfig(configPath)
	if err != nil {
		t.Fatalf("parseOpenClawConfig returned error: %v", err)
	}

	if parsed.ModelRef != "openai/gpt-5" {
		t.Fatalf("expected model ref openai/gpt-5, got %q", parsed.ModelRef)
	}
	if parsed.ModelAlias != "openai/gpt-5" {
		t.Fatalf("expected model alias openai/gpt-5, got %q", parsed.ModelAlias)
	}
	if parsed.ProviderName != "openai" {
		t.Fatalf("expected provider name openai, got %q", parsed.ProviderName)
	}
	if parsed.ProviderAPI != "openai-responses" {
		t.Fatalf("expected provider API openai-responses, got %q", parsed.ProviderAPI)
	}
	if parsed.ProviderBaseURL != "https://api.openai.com/v1" {
		t.Fatalf("expected provider base URL to be preserved, got %q", parsed.ProviderBaseURL)
	}
	if parsed.ProviderAPIKey != "provider-secret" {
		t.Fatalf("expected resolved provider API key, got %q", parsed.ProviderAPIKey)
	}
	if len(parsed.MCPServers) != 1 {
		t.Fatalf("expected one MCP server, got %d", len(parsed.MCPServers))
	}
	if parsed.MCPServers["preexisting"].URL != "https://mcp.example.com/v1" {
		t.Fatalf("expected MCP server URL to parse, got %+v", parsed.MCPServers["preexisting"])
	}
	if got := parsed.ModelCatalog["name"]; got != "GPT-5" {
		t.Fatalf("expected model catalog to be preserved, got %#v", parsed.ModelCatalog)
	}
	if len(parsed.ConfiguredModels) != 2 {
		t.Fatalf("expected two configured models, got %#v", parsed.ConfiguredModels)
	}
	if parsed.ConfiguredModels[0].ConfigKey != "agents.defaults.model.primary" {
		t.Fatalf("unexpected primary config key: %#v", parsed.ConfiguredModels)
	}
	if parsed.ConfiguredModels[1].ConfigKey != "agents.defaults.model.fallbacks[0]" {
		t.Fatalf("unexpected fallback config key: %#v", parsed.ConfiguredModels)
	}
}

func TestParseOpenClawConfigResolvesBedrockCredentialsFromConfigEnv(t *testing.T) {
	tempDir := t.TempDir()
	configPath := filepath.Join(tempDir, "openclaw.json5")
	t.Setenv("AWS_BEARER_TOKEN_BEDROCK", "")
	t.Setenv("AWS_ACCESS_KEY_ID", "")
	t.Setenv("AWS_SECRET_ACCESS_KEY", "")
	t.Setenv("AWS_SESSION_TOKEN", "")
	t.Setenv("AWS_REGION", "")
	t.Setenv("AWS_DEFAULT_REGION", "")

	config := `{
  env: {
    vars: {
      AWS_ACCESS_KEY_ID: "AKIA_CFG",
      AWS_SECRET_ACCESS_KEY: "cfg-secret",
      AWS_SESSION_TOKEN: "cfg-session",
      AWS_REGION: "us-east-1",
    },
  },
  agents: {
    defaults: {
      model: {
        primary: "amazon-bedrock/us.anthropic.claude-opus-4-6-v1",
      },
    },
  },
  models: {
    providers: {
      "amazon-bedrock": {
        api: "bedrock-converse-stream",
        auth: "aws-sdk",
        models: [
          {
            id: "us.anthropic.claude-opus-4-6-v1",
            name: "Claude Opus 4.6 (Bedrock)",
          },
        ],
      },
    },
  },
}`
	if err := os.WriteFile(configPath, []byte(config), 0o600); err != nil {
		t.Fatalf("failed to write test config: %v", err)
	}

	parsed, err := parseOpenClawConfig(configPath)
	if err != nil {
		t.Fatalf("parseOpenClawConfig returned error: %v", err)
	}
	if parsed.UsesAmbientAuth {
		t.Fatal("expected imported Bedrock credentials, not ambient fallback")
	}
	if parsed.ProviderAPIKey == "" {
		t.Fatal("expected imported Bedrock credential payload")
	}

	var payload bedrockCredentialPayload
	if err := json.Unmarshal([]byte(parsed.ProviderAPIKey), &payload); err != nil {
		t.Fatalf("expected JSON credential payload, got %v", err)
	}
	if payload.AWSAccessKeyID != "AKIA_CFG" || payload.AWSSecretAccessKey != "cfg-secret" {
		t.Fatalf("unexpected credential payload %#v", payload)
	}
	if payload.AWSRegionName != "us-east-1" {
		t.Fatalf("expected region from config env, got %#v", payload)
	}
}

func TestParseOpenClawConfigPrefersInlineBedrockAPIKey(t *testing.T) {
	tempDir := t.TempDir()
	configPath := filepath.Join(tempDir, "openclaw.json")

	config := `{
  "agents": {
    "defaults": {
      "model": {
        "primary": "bedrock/anthropic.claude-opus-4-6-2025-11-01-v1:0"
      }
    }
  },
  "models": {
    "providers": {
      "bedrock": {
        "baseUrl": "https://bedrock-runtime.us-east-1.amazonaws.com",
        "apiKey": "ABSKQ-inline-test",
        "auth": "api-key",
        "api": "bedrock-converse-stream",
        "authHeader": true,
        "models": [
          {
            "id": "anthropic.claude-opus-4-6-2025-11-01-v1:0",
            "name": "Claude Opus 4.6 (Bedrock)"
          }
        ]
      }
    }
  }
}`
	if err := os.WriteFile(configPath, []byte(config), 0o600); err != nil {
		t.Fatalf("failed to write test config: %v", err)
	}

	parsed, err := parseOpenClawConfig(configPath)
	if err != nil {
		t.Fatalf("parseOpenClawConfig returned error: %v", err)
	}
	if parsed.UsesAmbientAuth {
		t.Fatal("expected inline Bedrock API key to disable ambient fallback")
	}
	if parsed.ProviderAPIKey != "ABSKQ-inline-test" {
		t.Fatalf("expected inline Bedrock API key, got %q", parsed.ProviderAPIKey)
	}
}

func TestParseOpenClawConfigResolvesAmazonBedrockRefToBedrockProviderBlock(t *testing.T) {
	tempDir := t.TempDir()
	configPath := filepath.Join(tempDir, "openclaw.json")

	config := `{
  "agents": {
    "defaults": {
      "model": {
        "primary": "amazon-bedrock/us.anthropic.claude-opus-4-6-v1"
      }
    }
  },
  "models": {
    "providers": {
      "bedrock": {
        "baseUrl": "https://bedrock-runtime.us-east-1.amazonaws.com",
        "apiKey": "ABSKQ-alias-test",
        "auth": "api-key",
        "api": "bedrock-converse-stream",
        "models": [
          {
            "id": "us.anthropic.claude-opus-4-6-v1",
            "name": "Claude Opus 4.6 (Bedrock)"
          }
        ]
      }
    }
  }
}`
	if err := os.WriteFile(configPath, []byte(config), 0o600); err != nil {
		t.Fatalf("failed to write test config: %v", err)
	}

	parsed, err := parseOpenClawConfig(configPath)
	if err != nil {
		t.Fatalf("parseOpenClawConfig returned error: %v", err)
	}
	if parsed.ProviderAPIKey != "ABSKQ-alias-test" {
		t.Fatalf("expected provider alias fallback to resolve inline key, got %q", parsed.ProviderAPIKey)
	}
}

func TestParseOpenClawConfigRecoversUpstreamFromManagedConfigState(t *testing.T) {
	tempDir := t.TempDir()
	testenv.SetHome(t, tempDir)

	configPath := filepath.Join(tempDir, ".openclaw", "openclaw.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0o755); err != nil {
		t.Fatalf("failed to create config dir: %v", err)
	}

	managedConfig := `{
  "agents": {
    "defaults": {
      "model": {
        "primary": "preloop/amazon-bedrock/us.anthropic.claude-opus-4-6-v1"
      }
    }
  },
  "models": {
    "mode": "replace",
    "providers": {
      "preloop": {
        "baseUrl": "https://review.preloop.ai/openai/v1",
        "apiKey": "runtime-session-token",
        "api": "openai-responses",
        "models": [
          {
            "id": "amazon-bedrock/us.anthropic.claude-opus-4-6-v1",
            "name": "amazon-bedrock/us.anthropic.claude-opus-4-6-v1"
          }
        ]
      }
    }
  }
}`
	if err := os.WriteFile(configPath, []byte(managedConfig), 0o600); err != nil {
		t.Fatalf("failed to write managed config: %v", err)
	}

	statePath, err := localEnrollmentStatePath("openclaw", configPath)
	if err != nil {
		t.Fatalf("failed to resolve state path: %v", err)
	}
	if err := os.MkdirAll(filepath.Dir(statePath), 0o755); err != nil {
		t.Fatalf("failed to create state dir: %v", err)
	}
	state := localEnrollmentState{
		AgentName:  "openclaw",
		ConfigPath: configPath,
		DiscoveredConfig: map[string]interface{}{
			"agents": map[string]interface{}{
				"defaults": map[string]interface{}{
					"model": map[string]interface{}{
						"primary": "amazon-bedrock/us.anthropic.claude-opus-4-6-v1",
					},
				},
			},
			"models": map[string]interface{}{
				"providers": map[string]interface{}{
					"bedrock": map[string]interface{}{
						"baseUrl": "https://bedrock-runtime.us-east-1.amazonaws.com",
						"apiKey":  "ABSKQ-state-test",
						"api":     "bedrock-converse-stream",
						"models": []interface{}{
							map[string]interface{}{
								"id":   "us.anthropic.claude-opus-4-6-v1",
								"name": "Claude Opus 4.6 (Bedrock)",
							},
						},
					},
				},
			},
		},
	}
	stateBytes, err := json.Marshal(state)
	if err != nil {
		t.Fatalf("failed to encode state: %v", err)
	}
	if err := os.WriteFile(statePath, stateBytes, 0o600); err != nil {
		t.Fatalf("failed to write state: %v", err)
	}

	parsed, err := parseOpenClawConfig(configPath)
	if err != nil {
		t.Fatalf("parseOpenClawConfig returned error: %v", err)
	}
	if parsed.ProviderID != "amazon-bedrock" {
		t.Fatalf("expected recovered provider id amazon-bedrock, got %q", parsed.ProviderID)
	}
	if parsed.ProviderName != "bedrock" {
		t.Fatalf("expected recovered provider name bedrock, got %q", parsed.ProviderName)
	}
	if parsed.ModelID != "us.anthropic.claude-opus-4-6-v1" {
		t.Fatalf("expected recovered model id, got %q", parsed.ModelID)
	}
	if parsed.ProviderBaseURL != "https://bedrock-runtime.us-east-1.amazonaws.com" {
		t.Fatalf("expected recovered base url, got %q", parsed.ProviderBaseURL)
	}
	if parsed.ProviderAPIKey != "ABSKQ-state-test" {
		t.Fatalf("expected recovered api key, got %q", parsed.ProviderAPIKey)
	}
	if parsed.ModelAlias != "amazon-bedrock/us.anthropic.claude-opus-4-6-v1" {
		t.Fatalf("expected recovered model alias, got %q", parsed.ModelAlias)
	}
}

func TestFindReusableAIModelPrefersMatchingUpstreamFingerprint(t *testing.T) {
	parsed := &openClawParsedConfig{
		ModelAlias:      "amazon-bedrock/us.anthropic.claude-opus-4-6-v1",
		ModelID:         "us.anthropic.claude-opus-4-6-v1",
		ProviderID:      "amazon-bedrock",
		ProviderName:    "amazon-bedrock",
		ProviderBaseURL: "https://bedrock-runtime.us-east-1.amazonaws.com/",
		ProviderAPIKey:  "ABSKQ-current",
	}
	managedModelAlias := openClawManagedModelAlias(parsed)
	otherConfig := &openClawParsedConfig{
		ModelID:         parsed.ModelID,
		ProviderName:    parsed.ProviderName,
		ProviderBaseURL: "https://bedrock-runtime.us-east-1.amazonaws.com",
		ProviderAPIKey:  "ABSKQ-other",
	}
	models := []aiModelResponse{
		{
			ID:              "model-alias-match",
			Name:            "Alias Match",
			ProviderName:    parsed.ProviderName,
			ModelIdentifier: parsed.ModelID,
			APIEndpoint:     "https://bedrock-runtime.us-east-1.amazonaws.com",
			HasAPIKey:       true,
			MetaData: mergeOpenClawUpstreamMeta(
				map[string]interface{}{
					"gateway": map[string]interface{}{"model_alias": managedModelAlias},
				},
				otherConfig,
			),
		},
		{
			ID:              "model-fingerprint-match",
			Name:            "Shared Upstream",
			ProviderName:    parsed.ProviderName,
			ModelIdentifier: parsed.ModelID,
			APIEndpoint:     "https://bedrock-runtime.us-east-1.amazonaws.com",
			HasAPIKey:       true,
			MetaData: mergeOpenClawUpstreamMeta(
				map[string]interface{}{
					"gateway": map[string]interface{}{"model_alias": "preloop/shared-bedrock"},
				},
				parsed,
			),
		},
	}

	reused := findReusableAIModel(models, parsed, managedModelAlias)
	if reused == nil {
		t.Fatal("expected reusable model")
	}
	if reused.ID != "model-fingerprint-match" {
		t.Fatalf("expected fingerprint match to win, got %q", reused.ID)
	}
}

func TestFindReusableAIModelFallsBackToSingleLegacyCandidate(t *testing.T) {
	parsed := &openClawParsedConfig{
		ModelAlias:      "openai/gpt-5",
		ModelID:         "gpt-5",
		ProviderID:      "openai",
		ProviderName:    "openai",
		ProviderBaseURL: "https://api.openai.com/v1/",
		ProviderAPIKey:  "sk-current",
	}
	models := []aiModelResponse{
		{
			ID:              "legacy-model",
			Name:            "Legacy OpenAI",
			ProviderName:    "openai",
			ModelIdentifier: "gpt-5",
			APIEndpoint:     "https://api.openai.com/v1",
			HasAPIKey:       true,
			MetaData: map[string]interface{}{
				"gateway": map[string]interface{}{"model_alias": "preloop/openai/gpt-5"},
			},
		},
	}

	reused := findReusableAIModel(models, parsed, openClawManagedModelAlias(parsed))
	if reused == nil {
		t.Fatal("expected legacy model to be reused")
	}
	if reused.ID != "legacy-model" {
		t.Fatalf("expected legacy model, got %q", reused.ID)
	}
}

func TestFindReusableAIModelPrefersConfiguredAliasMatchWhenEndpointUnknown(t *testing.T) {
	parsed := &openClawParsedConfig{
		ModelAlias:      "amazon-bedrock/us.anthropic.claude-opus-4-6-v1",
		ModelID:         "us.anthropic.claude-opus-4-6-v1",
		ProviderID:      "amazon-bedrock",
		ProviderName:    "bedrock",
		ProviderBaseURL: "",
	}
	managedModelAlias := openClawManagedModelAlias(parsed)
	models := []aiModelResponse{
		{
			ID:              "bedrock-empty",
			ProviderName:    "bedrock",
			ModelIdentifier: parsed.ModelID,
			APIEndpoint:     "",
			HasAPIKey:       false,
			MetaData: map[string]interface{}{
				"gateway": map[string]interface{}{"model_alias": managedModelAlias},
			},
		},
		{
			ID:              "bedrock-configured",
			ProviderName:    "bedrock",
			ModelIdentifier: parsed.ModelID,
			APIEndpoint:     "https://bedrock-runtime.us-east-1.amazonaws.com",
			HasAPIKey:       true,
			MetaData: map[string]interface{}{
				"gateway": map[string]interface{}{"model_alias": managedModelAlias},
			},
		},
	}

	reused := findReusableAIModel(models, parsed, managedModelAlias)
	if reused == nil {
		t.Fatal("expected configured model to be reused")
	}
	if reused.ID != "bedrock-configured" {
		t.Fatalf("expected configured alias match, got %q", reused.ID)
	}
}

func TestResolveOpenClawManagedGatewayToken(t *testing.T) {
	document := map[string]interface{}{
		"agents": map[string]interface{}{
			"defaults": map[string]interface{}{
				"model": map[string]interface{}{
					"primary": "preloop/amazon-bedrock/us.anthropic.claude-opus-4-6-v1",
				},
			},
		},
		"models": map[string]interface{}{
			"providers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"apiKey": "managed-runtime-token",
				},
			},
		},
	}

	if token := resolveOpenClawManagedGatewayToken(document); token != "managed-runtime-token" {
		t.Fatalf("expected managed token, got %q", token)
	}
}

func TestBuildOpenClawManagedMCPEnrollmentPlanRewritesGateway(t *testing.T) {
	tempDir := t.TempDir()
	configPath := filepath.Join(tempDir, "openclaw.json")
	t.Setenv("OPENAI_API_KEY", "provider-secret")

	config := `{
  agents: {
    defaults: {
      model: {
        primary: "openai/gpt-5",
        fallbacks: ["openai/gpt-5.4"],
      },
      models: {
        "openai/gpt-5": { alias: "GPT 5" },
      },
    },
    list: [
      {
        id: "main",
        model: "openai/gpt-5",
      },
    ],
  },
  models: {
    providers: {
      openai: {
        baseUrl: "https://api.openai.com/v1",
        api: "openai-responses",
        apiKey: "${OPENAI_API_KEY}",
        models: [
          { id: "gpt-5", name: "GPT-5" },
        ],
      },
    },
  },
  mcp: {
    servers: {
      filesystem: {
        command: "npx",
        args: ["-y", "@modelcontextprotocol/server-filesystem", "."],
        transport: "stdio",
      },
      remote: {
        transport: "http",
        url: "https://mcp.example.com/v1",
      },
    },
  },
}`
	if err := os.WriteFile(configPath, []byte(config), 0o600); err != nil {
		t.Fatalf("failed to write test config: %v", err)
	}

	agent := AgentConfig{Name: "OpenClaw", ConfigPath: configPath}
	plan, err := buildOpenClawManagedMCPEnrollmentPlan(
		agent,
		"https://preloop.example",
		"managed-token",
	)
	if err != nil {
		t.Fatalf("buildOpenClawManagedMCPEnrollmentPlan returned error: %v", err)
	}

	servers := ensureObjectPath(plan.ManagedDocument, "mcp", "servers")
	if len(servers) != 1 {
		t.Fatalf("expected rewritten config to keep only preloop MCP, got %#v", servers)
	}
	preloopServer, ok := servers["preloop"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected preloop MCP server in managed config, got %#v", servers)
	}
	if preloopServer["url"] != "https://preloop.example/mcp/v1" {
		t.Fatalf("unexpected managed MCP URL: %#v", preloopServer)
	}
	if preloopServer["transport"] != "streamable-http" {
		t.Fatalf("expected OpenClaw managed MCP transport streamable-http, got %#v", preloopServer)
	}
	control := ensureObjectPath(
		plan.ManagedDocument,
		"plugins",
		"entries",
		openClawPreloopPluginID,
		"config",
	)
	if control["control_ws_url"] != "wss://preloop.example/api/v1/agents/control/ws" {
		t.Fatalf("unexpected OpenClaw control WebSocket URL: %#v", control)
	}
	if control["bearer_token"] != "managed-token" {
		t.Fatalf("unexpected OpenClaw control bearer token: %#v", control)
	}
	if control["adapter_package"] != "@preloop-ai/openclaw-plugin" ||
		control["runtime"] != "openclaw" {
		t.Fatalf("unexpected OpenClaw control adapter metadata: %#v", control)
	}
	if control["runtime_principal_id"] != runtimePrincipalIDForAgent(agent) {
		t.Fatalf("unexpected OpenClaw runtime principal in control config: %#v", control)
	}

	providers := ensureObjectPath(plan.ManagedDocument, "models", "providers")
	managedProvider, ok := providers[openClawManagedProviderID].(map[string]interface{})
	if !ok {
		t.Fatalf("expected managed provider to be present, got %#v", providers)
	}
	if managedProvider["baseUrl"] != "https://preloop.example/openai/v1" {
		t.Fatalf("unexpected managed gateway URL: %#v", managedProvider)
	}
	if managedProvider["apiKey"] != "managed-token" {
		t.Fatalf("unexpected managed gateway token: %#v", managedProvider)
	}
	if extractOpenClawPrimaryModel(plan.ManagedDocument) != "preloop/openai/gpt-5" {
		t.Fatalf("expected primary model to be rewritten, got %#v", plan.ManagedDocument)
	}
	defaults, ok := lookupValue(plan.ManagedDocument, "agents", "defaults", "model").(map[string]interface{})
	if !ok {
		t.Fatalf("expected defaults model block, got %#v", plan.ManagedDocument)
	}
	fallbacks, ok := defaults["fallbacks"].([]interface{})
	if !ok || len(fallbacks) != 1 || fallbacks[0] != "preloop/openai/gpt-5.4" {
		t.Fatalf("expected fallback model to be rewritten, got %#v", defaults)
	}
	managedModels, ok := managedProvider["models"].([]interface{})
	if !ok || len(managedModels) != 2 {
		t.Fatalf("expected managed provider to expose both configured models, got %#v", managedProvider)
	}

	validation := openClawManagedMCPAdapter{}.ValidateManagedConfig(
		plan.ManagedDocument,
		"https://preloop.example",
	)
	if passed, _ := validation["validation_passed"].(bool); !passed {
		t.Fatalf("expected rewritten config to validate, got %#v", validation)
	}
	if validation["control_config_written"] != true ||
		validation["control_ws_url_ok"] != true ||
		validation["control_bearer_token_ok"] != true ||
		validation["control_adapter_package_ok"] != true {
		t.Fatalf("expected OpenClaw control config validation to pass, got %#v", validation)
	}
	if validation["control_plugin_installed"] != false ||
		validation["control_plugin_verified"] != false ||
		validation["control_channel_configured"] != false {
		t.Fatalf("expected OpenClaw runtime plugin to remain unverified, got %#v", validation)
	}
}

func TestApplyManagedAgentControlConfigAddsRuntimeMetadata(t *testing.T) {
	agent := AgentConfig{
		Name:        "OpenClaw",
		DisplayName: "Octavia",
		ConfigPath:  filepath.Join(t.TempDir(), "openclaw.json"),
	}
	plan := managedMCPEnrollmentPlan{
		Agent:           agent,
		ManagedDocument: map[string]interface{}{},
	}

	updated, err := applyManagedAgentControlConfig(
		plan,
		"http://localhost:8000",
		&managedAgentCredentialCreateResponse{
			Credential: managedAgentCredentialSummary{ID: "cred-123", Name: "octavia-mcp"},
			Token:      "durable-token",
		},
		&managedAgentSummary{ID: "agent-123"},
		&runtimeSessionTokenResponse{
			RuntimeSessionID:  "session-123",
			SessionSourceType: "openclaw",
			SessionSourceID:   "octavia-session",
			SessionReference:  agent.ConfigPath,
			Token:             "flow_short_lived_runtime_token",
		},
	)
	if err != nil {
		t.Fatalf("applyManagedAgentControlConfig returned error: %v", err)
	}

	control := ensureObjectPath(
		updated.ManagedDocument,
		"plugins",
		"entries",
		openClawPreloopPluginID,
		"config",
	)
	if control["control_ws_url"] != "ws://localhost:8000/api/v1/agents/control/ws" {
		t.Fatalf("unexpected control URL: %#v", control)
	}
	// The control channel has no token refresh: it must carry the durable
	// credential, never the short-lived runtime session token.
	if control["bearer_token"] != "durable-token" {
		t.Fatalf("expected durable credential as control bearer token, got %#v", control["bearer_token"])
	}
	if control["managed_agent_id"] != "agent-123" ||
		control["credential_id"] != "cred-123" ||
		control["runtime_session_id"] != "session-123" {
		t.Fatalf("expected runtime metadata in control config, got %#v", control)
	}
	sanitizedControl := ensureObjectPath(
		updated.SanitizedManaged,
		"plugins",
		"entries",
		openClawPreloopPluginID,
		"config",
	)
	if sanitizedControl["bearer_token"] != "<redacted>" {
		t.Fatalf("expected sanitized control bearer token, got %#v", sanitizedControl)
	}
}

func TestValidateAgentControlConfigVerifiesInstalledRuntimePlugin(t *testing.T) {
	skipNoShebangOnWindows(t, "OpenClaw runtime-plugin config verification")
	dir := t.TempDir()
	binDir := filepath.Join(dir, "bin")
	if err := os.MkdirAll(binDir, 0755); err != nil {
		t.Fatalf("failed to create bin dir: %v", err)
	}
	verifyPath := filepath.Join(binDir, "preloop-openclaw-plugin")
	if err := os.WriteFile(
		verifyPath,
		[]byte("#!/bin/sh\n[ \"$1\" = verify ] && [ \"$2\" = --config ]\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake plugin: %v", err)
	}
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	agent := AgentConfig{
		Name:       "OpenClaw",
		ConfigPath: filepath.Join(dir, "openclaw.json"),
	}
	doc := map[string]interface{}{}
	applyAgentControlConfigToDocument(
		agent,
		doc,
		buildManagedAgentControlConfig(agent, "https://preloop.example", "token", nil, nil, nil),
	)

	result := validateAgentControlConfig(agent, doc, "https://preloop.example")
	if result["control_plugin_installed"] != true ||
		result["control_plugin_verified"] != true ||
		result["control_channel_configured"] != true {
		t.Fatalf("expected installed runtime plugin to verify, got %#v", result)
	}
}

func TestValidateAgentControlConfigRejectsStaleOpenClawSidecarStatus(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	statusDir := filepath.Join(home, ".preloop-agent-control")
	if err := os.MkdirAll(statusDir, 0755); err != nil {
		t.Fatalf("failed to create sidecar status dir: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(statusDir, "openclaw.status.json"),
		[]byte(`{"state":"connected","runtime_session_id":"old-session"}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write stale sidecar status: %v", err)
	}

	agent := AgentConfig{
		Name:       "OpenClaw",
		ConfigPath: filepath.Join(home, ".openclaw", "openclaw.json"),
	}
	doc := map[string]interface{}{}
	applyAgentControlConfigToDocument(
		agent,
		doc,
		buildManagedAgentControlConfig(
			agent,
			"https://preloop.example",
			"token",
			nil,
			nil,
			&runtimeSessionTokenResponse{RuntimeSessionID: "new-session"},
		),
	)
	if err := os.MkdirAll(filepath.Dir(agent.ConfigPath), 0755); err != nil {
		t.Fatalf("failed to create OpenClaw config dir: %v", err)
	}
	configBytes, err := json.Marshal(doc)
	if err != nil {
		t.Fatalf("failed to marshal OpenClaw config: %v", err)
	}
	if err := os.WriteFile(agent.ConfigPath, configBytes, 0644); err != nil {
		t.Fatalf("failed to write OpenClaw config: %v", err)
	}

	result := validateAgentControlConfig(agent, doc, "https://preloop.example")
	if result["control_plugin_verified"] == true ||
		result["control_plugin_verification"] != "managed_sidecar_stale_status" {
		t.Fatalf("expected stale OpenClaw sidecar status to fail validation, got %#v", result)
	}
	if result["control_channel_configured"] == true {
		t.Fatalf("expected stale sidecar to keep channel unconfigured, got %#v", result)
	}
}

func TestAgentControlPluginInstallCommandUsesStandaloneOpenClawSource(t *testing.T) {
	pluginsRoot := t.TempDir()
	sourcePath := filepath.Join(pluginsRoot, "openclaw-preloop")
	if err := os.MkdirAll(sourcePath, 0755); err != nil {
		t.Fatalf("failed to create plugin source dir: %v", err)
	}
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", pluginsRoot)

	command, args, err := agentControlPluginInstallCommand("OpenClaw")
	if err != nil {
		t.Fatalf("unexpected install command error: %v", err)
	}
	if command != "openclaw" {
		t.Fatalf("expected openclaw installer, got %q", command)
	}
	if len(args) != 3 || args[0] != "plugins" || args[1] != "install" || args[2] != sourcePath {
		t.Fatalf("unexpected install args: %#v", args)
	}
}

func TestAgentControlPluginInstallCommandFallsBackToMarketplacePackage(t *testing.T) {
	pluginsRoot := t.TempDir()
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", pluginsRoot)

	command, args, err := agentControlPluginInstallCommand("OpenClaw")
	if err != nil {
		t.Fatalf("unexpected install command error: %v", err)
	}
	if command != "openclaw" {
		t.Fatalf("expected openclaw installer, got %q", command)
	}
	if len(args) != 3 ||
		args[0] != "plugins" ||
		args[1] != "install" ||
		args[2] != "@preloop-ai/openclaw-plugin" {
		t.Fatalf("unexpected marketplace install args: %#v", args)
	}
}

func TestInstallAgentControlRuntimePluginInstallsAndVerifiesOpenClaw(t *testing.T) {
	skipNoShebangOnWindows(t, "OpenClaw runtime-plugin install+verify flow")
	dir := t.TempDir()
	pluginsRoot := filepath.Join(dir, "runtime-plugins")
	sourcePath := filepath.Join(pluginsRoot, "openclaw-preloop")
	if err := os.MkdirAll(sourcePath, 0755); err != nil {
		t.Fatalf("failed to create plugin source dir: %v", err)
	}
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", pluginsRoot)

	binDir := filepath.Join(dir, "bin")
	if err := os.MkdirAll(binDir, 0755); err != nil {
		t.Fatalf("failed to create bin dir: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(binDir, "openclaw"),
		[]byte("#!/bin/sh\n[ \"$1\" = plugins ] && [ \"$2\" = install ] && [ -d \"$3\" ]\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake OpenClaw installer: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(binDir, "preloop-openclaw-plugin"),
		[]byte("#!/bin/sh\n[ \"$1\" = verify ] && [ \"$2\" = --config ]\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake OpenClaw verifier: %v", err)
	}
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	result := installAgentControlRuntimePlugin(
		AgentConfig{Name: "OpenClaw", ConfigPath: filepath.Join(dir, "openclaw.json")},
		io.Discard,
	)
	if result["control_plugin_install_status"] != "installed_and_verified" ||
		result["control_plugin_installed"] != true ||
		result["control_plugin_verified"] != true {
		t.Fatalf("expected install and verify success, got %#v", result)
	}
}

func TestInstallAgentControlRuntimePluginReportsOpenClawNodeMismatch(t *testing.T) {
	skipNoShebangOnWindows(t, "OpenClaw Node version mismatch reporting")
	dir := t.TempDir()
	pluginsRoot := filepath.Join(dir, "runtime-plugins")
	sourcePath := filepath.Join(pluginsRoot, "openclaw-preloop")
	if err := os.MkdirAll(sourcePath, 0755); err != nil {
		t.Fatalf("failed to create plugin source dir: %v", err)
	}
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", pluginsRoot)

	binDir := filepath.Join(dir, "bin")
	if err := os.MkdirAll(binDir, 0755); err != nil {
		t.Fatalf("failed to create bin dir: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(binDir, "openclaw"),
		[]byte("#!/bin/sh\necho 'openclaw requires Node >=22.16.0. Detected: node 22.13.1 (exec: /usr/local/bin/node).' >&2\nexit 1\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake OpenClaw installer: %v", err)
	}
	t.Setenv("PATH", binDir)

	var output bytes.Buffer
	result := installAgentControlRuntimePlugin(
		AgentConfig{Name: "OpenClaw", ConfigPath: filepath.Join(dir, "openclaw.json")},
		&output,
	)
	if result["control_plugin_install_status"] != "runtime_node_unsupported" {
		t.Fatalf("expected Node mismatch status, got %#v", result)
	}
	if remediation, _ := result["control_plugin_install_remediation"].(string); !strings.Contains(remediation, "Upgrade Node") {
		t.Fatalf("expected Node upgrade remediation, got %#v", result)
	}
	if !strings.Contains(output.String(), "Upgrade Node") {
		t.Fatalf("expected remediation in CLI output, got %q", output.String())
	}
}

func TestEnsureAgentControlRuntimePluginsInstallsSupportedDiscoveredAgents(t *testing.T) {
	skipNoShebangOnWindows(t, "runtime-plugin install across discovered agents")
	dir := t.TempDir()
	pluginsRoot := filepath.Join(dir, "runtime-plugins")
	sourcePath := filepath.Join(pluginsRoot, "openclaw-preloop")
	if err := os.MkdirAll(sourcePath, 0755); err != nil {
		t.Fatalf("failed to create plugin source dir: %v", err)
	}
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", pluginsRoot)

	binDir := filepath.Join(dir, "bin")
	if err := os.MkdirAll(binDir, 0755); err != nil {
		t.Fatalf("failed to create bin dir: %v", err)
	}
	installLog := filepath.Join(dir, "install.log")
	t.Setenv("PRELOOP_INSTALL_LOG", installLog)
	if err := os.WriteFile(
		filepath.Join(binDir, "openclaw"),
		[]byte("#!/bin/sh\necho \"$@\" >> \"$PRELOOP_INSTALL_LOG\"\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake OpenClaw installer: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(binDir, "preloop-openclaw-plugin"),
		[]byte("#!/bin/sh\n[ \"$1\" = verify ] && [ \"$2\" = --config ]\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake OpenClaw verifier: %v", err)
	}
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	var output bytes.Buffer
	ensureAgentControlRuntimePlugins(
		nil,
		[]AgentConfig{
			{Name: "OpenClaw", ConfigPath: filepath.Join(dir, "openclaw.json")},
			{Name: "OpenClaw", ConfigPath: filepath.Join(dir, "openclaw.json")},
			{Name: "Codex CLI", ConfigPath: filepath.Join(dir, "codex.toml")},
		},
		&output,
	)

	logBytes, err := os.ReadFile(installLog)
	if err != nil {
		t.Fatalf("expected plugin installer to run: %v", err)
	}
	log := strings.TrimSpace(string(logBytes))
	want := "plugins install " + sourcePath
	if log != want {
		t.Fatalf("expected one OpenClaw plugin install %q, got %q", want, log)
	}
	if strings.Contains(output.String(), "Codex") {
		t.Fatalf("did not expect unsupported Codex plugin ensure output: %s", output.String())
	}
}

func TestBuildManagedRemoteServerRequestImportsCommandBackedMCporter(t *testing.T) {
	server := MCPDef{
		Command:   "npx",
		Args:      []string{"-y", "mcporter", "--url=https://remote.example.com/mcp"},
		Transport: "stdio",
		Env: map[string]string{
			"AUTHORIZATION": "Bearer upstream-token",
		},
	}

	request, warning, importMode, ok := buildManagedRemoteServerRequest("remote", server)
	if !ok {
		t.Fatal("expected command-backed server to be imported")
	}
	if importMode != "command" {
		t.Fatalf("expected command import mode, got %q", importMode)
	}
	if request["url"] != "https://remote.example.com/mcp" {
		t.Fatalf("unexpected imported URL: %#v", request)
	}
	if request["transport"] != "http-streaming" {
		t.Fatalf("expected stdio transport to be normalized, got %#v", request)
	}
	if request["auth_type"] != "bearer" {
		t.Fatalf("expected bearer auth, got %#v", request)
	}
	authConfig, ok := request["auth_config"].(map[string]interface{})
	if !ok || authConfig["token"] != "upstream-token" {
		t.Fatalf("expected imported bearer token, got %#v", request)
	}
	if !strings.Contains(strings.ToLower(warning), "mcporter") {
		t.Fatalf("expected mcporter warning, got %q", warning)
	}
}

func TestInferOpenClawProviderNamePrefersProviderID(t *testing.T) {
	if got := inferOpenClawProviderName("google", "openai-completions"); got != "google" {
		t.Fatalf("expected google provider to remain google, got %q", got)
	}
}

func TestInferOpenClawProviderNameNormalizesBedrock(t *testing.T) {
	if got := inferOpenClawProviderName("amazon-bedrock", ""); got != "bedrock" {
		t.Fatalf("expected amazon-bedrock to normalize to bedrock, got %q", got)
	}
}

func TestResolveOpenClawProviderAPIKeyFallsBackToAuthProfiles(t *testing.T) {
	document := map[string]interface{}{
		"auth": map[string]interface{}{
			"profiles": map[string]interface{}{
				"google:default": map[string]interface{}{
					"mode":     "api_key",
					"provider": "google",
				},
			},
		},
	}

	value, note := resolveOpenClawProviderAPIKey(document, "google")
	if value != "" {
		t.Fatalf("expected no resolved credential value, got %q", value)
	}
	if !strings.Contains(note, "auth.profiles") || !strings.Contains(note, "google:default") {
		t.Fatalf("expected auth profile note, got %q", note)
	}
}

func TestResolveOpenClawBedrockCredentialsFromEnv(t *testing.T) {
	t.Setenv("AWS_BEARER_TOKEN_BEDROCK", "")
	t.Setenv("AWS_ACCESS_KEY_ID", "AKIA_TEST")
	t.Setenv("AWS_SECRET_ACCESS_KEY", "secret-test")
	t.Setenv("AWS_SESSION_TOKEN", "session-test")
	t.Setenv("AWS_REGION", "us-east-1")

	value, usesAmbient, note := resolveOpenClawProviderCredentials(
		map[string]interface{}{},
		"amazon-bedrock",
		"bedrock",
		"",
	)
	if value == "" {
		t.Fatal("expected Bedrock credential payload")
	}
	if usesAmbient {
		t.Fatal("expected imported Bedrock credentials, not ambient fallback")
	}
	if !strings.Contains(note, "AWS environment variables") {
		t.Fatalf("expected env note, got %q", note)
	}

	var payload bedrockCredentialPayload
	if err := json.Unmarshal([]byte(value), &payload); err != nil {
		t.Fatalf("expected JSON payload, got error %v", err)
	}
	if payload.AWSAccessKeyID != "AKIA_TEST" || payload.AWSSecretAccessKey != "secret-test" {
		t.Fatalf("unexpected payload %#v", payload)
	}
	if payload.AWSRegionName != "us-east-1" {
		t.Fatalf("expected region in payload, got %#v", payload)
	}
}

func TestResolveOpenClawBedrockCredentialsFromSharedAWSFiles(t *testing.T) {
	tempDir := t.TempDir()
	testenv.SetHome(t, tempDir)
	t.Setenv("AWS_PROFILE", "review")
	t.Setenv("AWS_BEARER_TOKEN_BEDROCK", "")
	t.Setenv("AWS_ACCESS_KEY_ID", "")
	t.Setenv("AWS_SECRET_ACCESS_KEY", "")
	t.Setenv("AWS_SESSION_TOKEN", "")
	t.Setenv("AWS_REGION", "")
	t.Setenv("AWS_DEFAULT_REGION", "")

	awsDir := filepath.Join(tempDir, ".aws")
	if err := os.MkdirAll(awsDir, 0o700); err != nil {
		t.Fatalf("failed to create aws dir: %v", err)
	}
	credentials := "[review]\naws_access_key_id = FILE_KEY\naws_secret_access_key = FILE_SECRET\naws_session_token = FILE_SESSION\n"
	if err := os.WriteFile(filepath.Join(awsDir, "credentials"), []byte(credentials), 0o600); err != nil {
		t.Fatalf("failed to write credentials file: %v", err)
	}
	config := "[profile review]\nregion = eu-west-1\n"
	if err := os.WriteFile(filepath.Join(awsDir, "config"), []byte(config), 0o600); err != nil {
		t.Fatalf("failed to write config file: %v", err)
	}

	value, usesAmbient, note := resolveOpenClawProviderCredentials(
		map[string]interface{}{},
		"amazon-bedrock",
		"bedrock",
		"",
	)
	if value == "" {
		t.Fatal("expected Bedrock credential payload from shared AWS files")
	}
	if usesAmbient {
		t.Fatal("expected imported Bedrock credentials, not ambient fallback")
	}
	// The note embeds a real filesystem path, so the separator is platform
	// native. Build the expected fragment the same way rather than hardcoding
	// a forward slash, which only matches on POSIX.
	sharedCredentialsFragment := filepath.Join(".aws", "credentials")
	if !strings.Contains(note, sharedCredentialsFragment) {
		t.Fatalf(
			"expected shared credentials note containing %q, got %q",
			sharedCredentialsFragment,
			note,
		)
	}
}

func TestFilterAgentsPendingLocalEnrollmentSkipsSavedState(t *testing.T) {
	tempDir := t.TempDir()
	testenv.SetHome(t, tempDir)

	enrolled := AgentConfig{
		Name:       "OpenClaw",
		ConfigPath: filepath.Join(tempDir, ".openclaw", "openclaw.json"),
	}
	pending := AgentConfig{
		Name:       "Codex CLI",
		ConfigPath: filepath.Join(tempDir, ".codex", "config.json"),
	}

	if err := saveLocalEnrollmentState(&localEnrollmentState{
		AgentName:          enrolled.Name,
		RuntimePrincipalID: runtimePrincipalIDForAgent(enrolled),
		ConfigPath:         enrolled.ConfigPath,
		BackupPath:         filepath.Join(tempDir, "backup.json"),
		ManagedServerName:  "preloop",
		ManagedServerURL:   "https://preloop.example/mcp/v1",
		AppliedAt:          time.Now().UTC(),
	}); err != nil {
		t.Fatalf("failed to save local enrollment state: %v", err)
	}

	candidates := filterAgentsPendingLocalEnrollment([]AgentConfig{enrolled, pending})
	if len(candidates) != 1 {
		t.Fatalf("expected one onboarding candidate, got %#v", candidates)
	}
	if candidates[0].Name != pending.Name {
		t.Fatalf("expected pending agent to remain, got %#v", candidates)
	}
}

// TestAgentsEnrollFlags_LiveValidateDefaultsTrue ensures the CLI's
// “--live-validate“ flag now defaults to true so onboarding actually
// performs end-to-end validation by default. This protects against an
// accidental future regression that flips the default back to false (which
// is what previously left every scripted re-onboard stuck on
// "Live check not run" in the UI).
func TestAgentsEnrollFlags_LiveValidateDefaultsTrue(t *testing.T) {
	flag := agentsEnrollCmd.Flags().Lookup("live-validate")
	if flag == nil {
		t.Fatalf("expected agents onboard to expose --live-validate")
	}
	if flag.DefValue != "true" {
		t.Fatalf("expected --live-validate to default to true, got %q", flag.DefValue)
	}
}

func TestAgentsEnrollFlags_SkipLiveValidateExists(t *testing.T) {
	flag := agentsEnrollCmd.Flags().Lookup("skip-live-validate")
	if flag == nil {
		t.Fatalf("expected agents onboard to expose --skip-live-validate as the supported opt-out")
	}
	if flag.DefValue != "false" {
		t.Fatalf("expected --skip-live-validate to default to false, got %q", flag.DefValue)
	}
}

func TestAgentsDiscoverFlags_SkipLiveValidateExists(t *testing.T) {
	flag := agentsDiscoverCmd.Flags().Lookup("skip-live-validate")
	if flag == nil {
		t.Fatalf("expected agents discover to expose --skip-live-validate so the discover-driven onboarding path can opt out")
	}
}

// shouldRunLiveValidation mirrors the gating logic inside
// executeManagedEnrollment so we can assert the resolution rules without
// spinning up a full enrollment integration.
func shouldRunLiveValidation(opts managedEnrollmentOptions, agent AgentConfig) bool {
	return opts.LiveValidate &&
		!opts.SkipLiveValidate &&
		supportsManagedLiveValidation(agent)
}

func TestLiveValidationGating_DefaultRunsForSupportedAgent(t *testing.T) {
	if !shouldRunLiveValidation(
		managedEnrollmentOptions{LiveValidate: true},
		AgentConfig{Name: "OpenClaw"},
	) {
		t.Fatalf("expected live validation to run by default for a supported agent")
	}
	if !shouldRunLiveValidation(
		managedEnrollmentOptions{LiveValidate: true},
		AgentConfig{Name: "Codex CLI"},
	) {
		t.Fatalf("expected live validation to run by default for Codex CLI")
	}
}

// Bulk and discover-driven onboarding both used to set SkipConfirmation
// and AutoApprove, which silently suppressed live validation. Make sure
// neither of those flags blocks live validation any more.
func TestLiveValidationGating_NotBlockedBySkipConfirmationOrAutoApprove(t *testing.T) {
	opts := managedEnrollmentOptions{
		LiveValidate:     true,
		AutoApprove:      true,
		SkipConfirmation: true,
	}
	if !shouldRunLiveValidation(opts, AgentConfig{Name: "OpenClaw"}) {
		t.Fatalf("expected live validation to still run for an --all / --yes / discover bulk onboard")
	}
}

func TestLiveValidationGating_SkipFlagOverridesEverything(t *testing.T) {
	opts := managedEnrollmentOptions{
		LiveValidate:     true,
		SkipLiveValidate: true,
	}
	if shouldRunLiveValidation(opts, AgentConfig{Name: "OpenClaw"}) {
		t.Fatalf("expected --skip-live-validate to suppress live validation even for supported agents")
	}
}

func TestLiveValidationGating_UnsupportedAgentNeverRuns(t *testing.T) {
	// Only kinds that have NO live-validate implementation should be skipped.
	// Every kind we ship a managed runtime adapter for now has a live check
	// (see runManagedAgentLiveValidation dispatch); listing them as
	// "unsupported" here would silently mask a regression where someone
	// drops a per-agent run<Kind>LiveValidation function.
	opts := managedEnrollmentOptions{LiveValidate: true}
	for _, name := range []string{
		"Claude Desktop",
		"Cursor",
		"Windsurf",
		"VSCode / Copilot",
	} {
		if shouldRunLiveValidation(opts, AgentConfig{Name: name}) {
			t.Fatalf("expected live validation to be skipped for unsupported agent %q", name)
		}
	}
}

func TestLiveValidationGating_SupportedAgents_AllRunByDefault(t *testing.T) {
	// Regression: the user reported "Live check not run / unsupported" for
	// every kind except OpenClaw and Codex CLI after onboarding via the CLI.
	// We now ship a live-validate implementation for every managed agent
	// kind, so each must report supported by default.
	opts := managedEnrollmentOptions{LiveValidate: true}
	for _, name := range []string{
		"OpenClaw",
		"Codex CLI",
		"Hermes",
		"OpenCode",
		"Claude Code",
		"Gemini CLI",
	} {
		if !shouldRunLiveValidation(opts, AgentConfig{Name: name}) {
			t.Fatalf("expected live validation to run for managed agent %q", name)
		}
	}
}

// TestBuildCodexLiveValidationPayload_IncludesGatewayRequiredFields proves
// the Codex live-validate request body carries the three fields the Preloop
// gateway (and the upstream Codex Responses backend) reject the request
// without: “instructions“ (non-empty string), “store“ set to false, and
// “input“ in Responses-API item form. It also asserts the validation
// token is embedded in the user-text item so the post-call gateway-usage
// search can match it. Regressing any of these caused the user-reported
// error "Instructions are required" + a usage-search timeout, which then
// aborted the rest of “preloop agents onboard --all“.
func TestBuildCodexLiveValidationPayload_IncludesGatewayRequiredFields(t *testing.T) {
	prompt := "Welcome to Preloop. Validation token: preloop-validation-12345. Reply with ACK only."
	payload := buildCodexLiveValidationPayload("preloop/openai/gpt-5.4", prompt)

	// Round-trip through JSON to assert the on-the-wire shape, since this is
	// exactly what the gateway will see.
	encoded, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("failed to JSON-marshal payload: %v", err)
	}
	var decoded map[string]interface{}
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		t.Fatalf("failed to JSON-unmarshal payload: %v", err)
	}

	if model, _ := decoded["model"].(string); model != "preloop/openai/gpt-5.4" {
		t.Fatalf("expected model 'preloop/openai/gpt-5.4', got %q", model)
	}
	instructions, _ := decoded["instructions"].(string)
	if strings.TrimSpace(instructions) == "" {
		t.Fatalf("expected non-empty 'instructions' field, got %q", instructions)
	}
	if store, ok := decoded["store"].(bool); !ok || store {
		t.Fatalf("expected 'store: false', got %#v", decoded["store"])
	}
	input, ok := decoded["input"].([]interface{})
	if !ok || len(input) == 0 {
		t.Fatalf("expected 'input' to be a non-empty array of Responses-API items, got %#v", decoded["input"])
	}
	first, ok := input[0].(map[string]interface{})
	if !ok {
		t.Fatalf("expected first input item to be an object, got %#v", input[0])
	}
	if first["type"] != "message" || first["role"] != "user" {
		t.Fatalf("expected first input item to be a user message, got %#v", first)
	}
	content, ok := first["content"].([]interface{})
	if !ok || len(content) == 0 {
		t.Fatalf("expected first input message to have non-empty content, got %#v", first)
	}
	firstContent, ok := content[0].(map[string]interface{})
	if !ok || firstContent["type"] != "input_text" {
		t.Fatalf("expected first content item to be input_text, got %#v", content[0])
	}
	text, _ := firstContent["text"].(string)
	if !strings.Contains(text, "preloop-validation-") {
		t.Fatalf("expected user-text to embed the preloop-validation token, got %q", text)
	}
	// max_output_tokens / max_completion_tokens MUST NOT be set: Codex'
	// chatgpt.com backend rejects them with HTTP 400 "Unsupported parameter:
	// max_output_tokens", which broke live validation for every Codex CLI
	// onboard until we dropped them.
	if _, present := decoded["max_output_tokens"]; present {
		t.Fatalf("expected 'max_output_tokens' to be absent (Codex backend rejects it), got %#v", decoded["max_output_tokens"])
	}
	if _, present := decoded["max_completion_tokens"]; present {
		t.Fatalf("expected 'max_completion_tokens' to be absent, got %#v", decoded["max_completion_tokens"])
	}
}

func TestManagedAPIKeyIDForTokenMatchesCredentialPrefix(t *testing.T) {
	credentials := []managedAgentCredentialSummary{
		{
			APIKeyID:  "new-key",
			KeyPrefix: "agt_new",
			Status:    "active",
		},
		{
			APIKeyID:  "configured-key",
			KeyPrefix: "agt_config",
			Status:    "active",
		},
	}

	got := managedAPIKeyIDForToken(credentials, "agt_config_secret")
	if got != "configured-key" {
		t.Fatalf("expected configured token API key id, got %q", got)
	}
}

func TestDefaultManagedLiveValidationResult_ForOpenClaw(t *testing.T) {
	result := defaultManagedLiveValidationResult(AgentConfig{Name: "OpenClaw"})
	if supported, _ := result["live_validation_supported"].(bool); !supported {
		t.Fatalf("expected live validation to be supported, got %#v", result)
	}
	if status := result["live_validation_status"]; status != "not_run" {
		t.Fatalf("expected not_run status, got %#v", result)
	}
}

func TestDefaultManagedLiveValidationResult_ForCodex(t *testing.T) {
	result := defaultManagedLiveValidationResult(AgentConfig{Name: "Codex CLI"})
	if supported, _ := result["live_validation_supported"].(bool); !supported {
		t.Fatalf("expected Codex live validation to be supported, got %#v", result)
	}
	if status := result["live_validation_status"]; status != "not_run" {
		t.Fatalf("expected Codex live validation status not_run, got %#v", result)
	}
}

func TestResolveCodexManagedGatewayTokenAndModelAlias(t *testing.T) {
	document := map[string]interface{}{
		"model_provider": "preloop",
		"model":          "openai/gpt-5.4",
		"model_providers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"experimental_bearer_token": "codex-durable-token",
				"wire_api":                  "responses",
			},
		},
	}
	if got := resolveCodexManagedGatewayToken(document); got != "codex-durable-token" {
		t.Fatalf("expected Codex managed gateway token, got %#v", got)
	}
	if got := resolveCodexManagedModelAlias(document); got != "openai/gpt-5.4" {
		t.Fatalf("expected Codex managed model alias, got %#v", got)
	}
}

func TestExtractClaudeTokenFromCredentialBlobSupportsClaudeAiOauthAccessToken(t *testing.T) {
	raw := `{"claudeAiOauth":{"accessToken":"claude-access-token","refreshToken":"claude-refresh-token","expiresAt":1893456000000}}`
	if got := extractClaudeTokenFromCredentialBlob(raw); got != "claude-access-token" {
		t.Fatalf("expected Claude access token from claudeAiOauth blob, got %#v", got)
	}
}

func TestResolveClaudePrimaryAPIKey(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	if err := os.WriteFile(
		filepath.Join(home, ".claude.json"),
		[]byte(`{"primaryApiKey":"sk-ant-api03-managed"}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write Claude config: %v", err)
	}
	if got := resolveClaudePrimaryAPIKey(); got != "sk-ant-api03-managed" {
		t.Fatalf("expected primary API key, got %#v", got)
	}
}

func TestParseClaudeOAuthCredentialBlob(t *testing.T) {
	raw := `{"claudeAiOauth":{"accessToken":"sk-ant-oat01-access","refreshToken":"refresh-token","expiresAt":1893456000000}}`
	credential := parseClaudeOAuthCredentialBlob(raw, 0)
	if credential == nil {
		t.Fatal("expected Claude OAuth credential")
	}
	if credential.AccessToken != "sk-ant-oat01-access" {
		t.Fatalf("unexpected access token %#v", credential.AccessToken)
	}
	if credential.RefreshToken != "refresh-token" {
		t.Fatalf("unexpected refresh token %#v", credential.RefreshToken)
	}
	if credential.ExpiresAtMS != 1893456000000 {
		t.Fatalf("unexpected expiry %#v", credential.ExpiresAtMS)
	}
	payload := credential.Payload()
	if payload["access"] != "sk-ant-oat01-access" ||
		payload["refresh"] != "refresh-token" ||
		payload["expires"] != int64(1893456000000) {
		t.Fatalf("unexpected payload %#v", payload)
	}
}

func TestWaitForManagedValidationUsage_FindsIndexedEvent(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.Contains(r.URL.RawQuery, "runtime_principal_id=octavia-123") {
			t.Fatalf("expected runtime principal filter in query, got %q", r.URL.RawQuery)
		}
		if !strings.Contains(r.URL.RawQuery, "api_key_id=key-1") {
			t.Fatalf("expected api key filter in query, got %q", r.URL.RawQuery)
		}
		_ = json.NewEncoder(w).Encode(gatewayUsageSearchResponse{
			Items: []gatewayUsageSearchItem{
				{
					APIUsageID:         "usage-1",
					Timestamp:          "2026-03-10T10:00:00Z",
					StatusCode:         200,
					ModelAlias:         "openai/gpt-5",
					RuntimePrincipalID: "octavia-123",
					APIKeyID:           "key-1",
				},
			},
		})
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	item, err := waitForManagedValidationUsage(
		client,
		"octavia-123",
		"key-1",
		"openai/gpt-5",
		"validation-token",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if item == nil || item.APIUsageID != "usage-1" {
		t.Fatalf("expected indexed usage item, got %#v", item)
	}
}

func TestExtractOpenClawProfileAPIKeyMaterial(t *testing.T) {
	t.Setenv("GEMINI_TEST_KEY", "from-env")
	profile := map[string]interface{}{
		"mode":   "api_key",
		"apiKey": "${GEMINI_TEST_KEY}",
	}
	key, note := extractOpenClawProfileAPIKeyMaterial(profile)
	if key != "from-env" {
		t.Fatalf("expected env-expanded key, got %q (note=%q)", key, note)
	}
	profileInline := map[string]interface{}{
		"mode":   "api_key",
		"apiKey": "inline-secret",
	}
	key2, _ := extractOpenClawProfileAPIKeyMaterial(profileInline)
	if key2 != "inline-secret" {
		t.Fatalf("expected inline key, got %q", key2)
	}
}

func TestMergeGatewayMetaForAIModelGatewayEnabledOnlyWithFlag(t *testing.T) {
	meta := mergeGatewayMetaForAIModel(
		nil,
		nil,
		AgentConfig{},
		"https://preloop.example/openai/v1",
		"google/gemini-2.5-pro",
		false,
	)
	gw, _ := meta["gateway"].(map[string]interface{})
	if gw["enabled"] != false {
		t.Fatalf("expected gateway disabled, got %#v", gw)
	}
	metaOn := mergeGatewayMetaForAIModel(
		nil,
		nil,
		AgentConfig{},
		"https://preloop.example/openai/v1",
		"google/gemini-2.5-pro",
		true,
	)
	gwOn, _ := metaOn["gateway"].(map[string]interface{})
	if gwOn["enabled"] != true {
		t.Fatalf("expected gateway enabled, got %#v", gwOn)
	}
}

func TestMergeOpenClawAmbientProviderMetaAddsRegion(t *testing.T) {
	meta := mergeOpenClawAmbientProviderMeta(
		map[string]interface{}{},
		&openClawParsedConfig{
			UsesAmbientAuth: true,
			ProviderRegion:  "us-east-1",
		},
	)
	providerRuntime, _ := meta["provider_runtime"].(map[string]interface{})
	if providerRuntime["ambient_credentials"] != true {
		t.Fatalf("expected ambient_credentials flag, got %#v", providerRuntime)
	}
	if providerRuntime["region"] != "us-east-1" {
		t.Fatalf("expected region to be preserved, got %#v", providerRuntime)
	}
}

func TestAIModelUsesAmbientProviderCredentials(t *testing.T) {
	model := &aiModelResponse{
		MetaData: map[string]interface{}{
			"provider_runtime": map[string]interface{}{
				"ambient_credentials": true,
			},
		},
	}
	if !aiModelUsesAmbientProviderCredentials(model) {
		t.Fatal("expected ambient provider credentials to be detected")
	}
}
