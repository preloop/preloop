package cmd

// Tests for `preloop agents refresh`: the pure per-agent-kind document
// rewriters that re-synchronize managed model sections with the account's
// authorized model list. Fixtures mirror what onboarding writes; assertions
// cover the before/after model diff, selected-model preservation, and the
// unauthorized-selection fallback the brief requires.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
)

// refreshTestModel builds one account AI model row as the CLI sees it from
// GET /api/v1/ai-models, with the gateway alias stored in metadata the way
// onboarding and the console write it.
func refreshTestModel(id, provider, identifier, alias string) aiModelResponse {
	return aiModelResponse{
		ID:              id,
		Name:            provider + " " + identifier,
		ProviderName:    provider,
		ModelIdentifier: identifier,
		HasAPIKey:       true,
		MetaData: map[string]interface{}{
			"gateway": map[string]interface{}{
				"enabled":     true,
				"model_alias": alias,
			},
		},
	}
}

func refreshTestOAuthModel(id, identifier, alias, secretID string) aiModelResponse {
	model := refreshTestModel(id, "anthropic", identifier, alias)
	model.CredentialType = "oauth_anthropic_claude_code"
	model.CredentialsSecretID = secretID
	return model
}

func sortedCopy(values []string) []string {
	out := append([]string{}, values...)
	sort.Strings(out)
	return out
}

func TestAuthorizedGatewayModelAliasesAccountWideAndPrincipalBound(t *testing.T) {
	models := []aiModelResponse{
		refreshTestModel("m1", "openai", "gpt-5.4", "openai/gpt-5.4"),
		refreshTestOAuthModel("m2", "claude-fable-5", "anthropic/claude-fable-5", "sec-1"),
		refreshTestOAuthModel("m3", "claude-opus-4-6", "anthropic/claude-opus-4-6", "sec-1"),
	}

	// Without a binding, principal-bound OAuth models are excluded (the
	// gateway would reject them for this agent too).
	got := authorizedGatewayModelAliases(models, nil)
	want := []string{"openai/gpt-5.4"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("authorizedGatewayModelAliases(no bindings) = %#v, want %#v", got, want)
	}

	// A binding admits exactly the bound model.
	bindings := []managedAgentModelBindingSummary{
		{AIModelID: "m2", GatewayAlias: "anthropic/claude-fable-5"},
	}
	got = authorizedGatewayModelAliases(models, bindings)
	want = []string{"anthropic/claude-fable-5", "openai/gpt-5.4"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("authorizedGatewayModelAliases(bound) = %#v, want %#v", got, want)
	}
}

func TestAuthorizedGatewayModelAliasesSkipsCredentiallessAndNonGatewayModels(t *testing.T) {
	credentialless := refreshTestModel("m1", "anthropic", "claude-opus-4-6", "anthropic/claude-opus-4-6")
	credentialless.HasAPIKey = false
	noGatewayMeta := aiModelResponse{
		ID:              "m2",
		ProviderName:    "openai",
		ModelIdentifier: "gpt-5.4",
		HasAPIKey:       true,
	}
	got := authorizedGatewayModelAliases([]aiModelResponse{credentialless, noGatewayMeta}, nil)
	if len(got) != 0 {
		t.Fatalf("expected no authorized aliases, got %#v", got)
	}
}

func TestDiffModelAliasSets(t *testing.T) {
	added, removed := diffModelAliasSets(
		[]string{"anthropic/claude-fable-5", "anthropic/claude-opus-4-6"},
		[]string{"anthropic/claude-fable-5-1", "anthropic/claude-opus-4-6"},
	)
	if !reflect.DeepEqual(added, []string{"anthropic/claude-fable-5-1"}) {
		t.Fatalf("unexpected added: %#v", added)
	}
	if !reflect.DeepEqual(removed, []string{"anthropic/claude-fable-5"}) {
		t.Fatalf("unexpected removed: %#v", removed)
	}
}

// claudeRefreshFixtureDoc mirrors the managed settings.json Claude Code
// onboarding writes for a fable-pinned account.
func claudeRefreshFixtureDoc() map[string]interface{} {
	return map[string]interface{}{
		"model": "fable",
		"env": map[string]interface{}{
			"ANTHROPIC_BASE_URL":                  "https://preloop.example/anthropic",
			"ANTHROPIC_API_KEY":                   "claude-durable-token",
			"ANTHROPIC_MODEL":                     "fable",
			"ANTHROPIC_CUSTOM_MODEL_OPTION":       "anthropic/claude-fable-5",
			"ANTHROPIC_CUSTOM_MODEL_OPTION_NAME":  "Preloop anthropic/claude-fable-5",
			"ANTHROPIC_DEFAULT_FABLE_MODEL":       "anthropic/claude-fable-5",
			"ANTHROPIC_DEFAULT_FABLE_MODEL_NAME":  "Fable (Preloop)",
			"ANTHROPIC_DEFAULT_OPUS_MODEL":        "anthropic/claude-opus-4-6",
			"ANTHROPIC_DEFAULT_OPUS_MODEL_NAME":   "Opus (Preloop)",
			"ANTHROPIC_DEFAULT_HAIKU_MODEL":       "anthropic/claude-haiku-4-5",
			"ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME":  "Haiku (Preloop)",
			"ANTHROPIC_DEFAULT_SONNET_MODEL":      "anthropic/claude-sonnet-4-5",
			"ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "Sonnet (Preloop)",
		},
		"mcpServers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"url": "https://preloop.example/mcp/v1",
			},
		},
	}
}

func claudeRefreshFixtureModels() []aiModelResponse {
	return []aiModelResponse{
		refreshTestModel("m-fable", "anthropic", "claude-fable-5", "anthropic/claude-fable-5"),
		refreshTestModel("m-opus", "anthropic", "claude-opus-4-6", "anthropic/claude-opus-4-6"),
		refreshTestModel("m-haiku", "anthropic", "claude-haiku-4-5", "anthropic/claude-haiku-4-5"),
		refreshTestModel("m-sonnet", "anthropic", "claude-sonnet-4-5", "anthropic/claude-sonnet-4-5"),
	}
}

func TestRefreshClaudeManagedModelDocumentPicksUpNewFamilyRelease(t *testing.T) {
	// Founder repro: Fable 5.1 released and entered the account catalog, but
	// the onboarded Claude Code still pins claude-fable-5. Refresh must
	// upgrade the fable family key to the newest authorized release while
	// keeping the selector-form selection, the token, and the MCP config.
	doc := claudeRefreshFixtureDoc()
	models := append(
		claudeRefreshFixtureModels(),
		refreshTestModel("m-fable-51", "anthropic", "claude-fable-5-1", "anthropic/claude-fable-5-1"),
	)

	outcome, err := refreshClaudeManagedModelDocument(
		AgentConfig{Name: "Claude Code"}, doc, models, nil,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	env := outcome.Doc["env"].(map[string]interface{})
	if env["ANTHROPIC_DEFAULT_FABLE_MODEL"] != "anthropic/claude-fable-5-1" {
		t.Errorf("unexpected fable pin: %#v", env["ANTHROPIC_DEFAULT_FABLE_MODEL"])
	}
	if env["ANTHROPIC_MODEL"] != "fable" {
		t.Errorf("family selection must stay in selector form, got %#v", env["ANTHROPIC_MODEL"])
	}
	if outcome.Doc["model"] != "fable" {
		t.Errorf("settings.model must stay in selector form, got %#v", outcome.Doc["model"])
	}
	if env["ANTHROPIC_API_KEY"] != "claude-durable-token" {
		t.Errorf("managed token must be preserved, got %#v", env["ANTHROPIC_API_KEY"])
	}
	if env["ANTHROPIC_BASE_URL"] != "https://preloop.example/anthropic" {
		t.Errorf("managed base URL must be preserved, got %#v", env["ANTHROPIC_BASE_URL"])
	}
	if _, ok := outcome.Doc["mcpServers"].(map[string]interface{})["preloop"]; !ok {
		t.Error("managed MCP server entry must be preserved")
	}
	added := outcome.added()
	removed := outcome.removed()
	if !reflect.DeepEqual(added, []string{"anthropic/claude-fable-5-1"}) {
		t.Errorf("unexpected added diff: %#v", added)
	}
	if !reflect.DeepEqual(removed, []string{"anthropic/claude-fable-5"}) {
		t.Errorf("unexpected removed diff: %#v", removed)
	}
	if len(outcome.Warnings) != 0 {
		t.Errorf("no warnings expected, got %#v", outcome.Warnings)
	}
}

func TestRefreshClaudeManagedModelDocumentUnchangedWhenCatalogMatches(t *testing.T) {
	doc := claudeRefreshFixtureDoc()
	outcome, err := refreshClaudeManagedModelDocument(
		AgentConfig{Name: "Claude Code"}, doc, claudeRefreshFixtureModels(), nil,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	if outcome.changed() {
		t.Fatalf(
			"expected no diff when the catalog matches the pins; added=%#v removed=%#v",
			outcome.added(), outcome.removed(),
		)
	}
}

func TestRefreshClaudeManagedModelDocumentPreservesNonFamilyPin(t *testing.T) {
	doc := claudeRefreshFixtureDoc()
	env := doc["env"].(map[string]interface{})
	env["ANTHROPIC_MODEL"] = "moonshot/kimi-k3"
	env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = "moonshot/kimi-k3"
	doc["model"] = "moonshot/kimi-k3"

	models := append(
		claudeRefreshFixtureModels(),
		refreshTestModel("m-kimi", "moonshot", "kimi-k3", "moonshot/kimi-k3"),
	)
	outcome, err := refreshClaudeManagedModelDocument(
		AgentConfig{Name: "Claude Code"}, doc, models, nil,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	refreshedEnv := outcome.Doc["env"].(map[string]interface{})
	if refreshedEnv["ANTHROPIC_MODEL"] != "moonshot/kimi-k3" {
		t.Errorf(
			"authorized non-family pin must be preserved verbatim, got %#v",
			refreshedEnv["ANTHROPIC_MODEL"],
		)
	}
	if len(outcome.Warnings) != 0 {
		t.Errorf("no warnings expected, got %#v", outcome.Warnings)
	}
}

func TestRefreshClaudeManagedModelDocumentFallsBackWhenPinUnauthorized(t *testing.T) {
	doc := claudeRefreshFixtureDoc()
	env := doc["env"].(map[string]interface{})
	env["ANTHROPIC_MODEL"] = "moonshot/kimi-k3"
	env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = "moonshot/kimi-k3"
	doc["model"] = "moonshot/kimi-k3"

	models := claudeRefreshFixtureModels()
	models[0].IsDefault = true // anthropic/claude-fable-5 is the account default
	outcome, err := refreshClaudeManagedModelDocument(
		AgentConfig{Name: "Claude Code"}, doc, models, nil,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	refreshedEnv := outcome.Doc["env"].(map[string]interface{})
	if refreshedEnv["ANTHROPIC_MODEL"] != "fable" {
		t.Errorf(
			"unauthorized pin must fall back to the account default family, got %#v",
			refreshedEnv["ANTHROPIC_MODEL"],
		)
	}
	if len(outcome.Warnings) == 0 ||
		!strings.Contains(outcome.Warnings[0], "no longer authorized") {
		t.Errorf("expected an unauthorized-fallback warning, got %#v", outcome.Warnings)
	}
}

func TestRefreshClaudeManagedModelDocumentSkipsWithoutManagedGateway(t *testing.T) {
	outcome, err := refreshClaudeManagedModelDocument(
		AgentConfig{Name: "Claude Code"},
		map[string]interface{}{"model": "opus"},
		claudeRefreshFixtureModels(),
		nil,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	if outcome.SkipReason == "" {
		t.Fatal("expected a skip reason for a config without managed gateway env")
	}
}

func openCodeRefreshFixtureDoc() map[string]interface{} {
	return map[string]interface{}{
		"model": "preloop/anthropic/claude-fable-5",
		"provider": map[string]interface{}{
			"preloop": map[string]interface{}{
				"npm": "@ai-sdk/openai-compatible",
				"options": map[string]interface{}{
					"baseURL": "https://preloop.example/openai/v1",
					"apiKey":  "opencode-durable-token",
				},
				"models": map[string]interface{}{
					"anthropic/claude-fable-5": map[string]interface{}{
						"name": "anthropic/claude-fable-5",
					},
				},
			},
		},
		"mcp": map[string]interface{}{
			"preloop": map[string]interface{}{
				"type": "remote",
				"url":  "https://preloop.example/mcp/v1",
			},
		},
	}
}

func TestRefreshOpenCodeManagedModelDocumentExpandsModelsMap(t *testing.T) {
	doc := openCodeRefreshFixtureDoc()
	models := []aiModelResponse{
		refreshTestModel("m-fable", "anthropic", "claude-fable-5", "anthropic/claude-fable-5"),
		refreshTestModel("m-fable-51", "anthropic", "claude-fable-5-1", "anthropic/claude-fable-5-1"),
		refreshTestModel("m-gpt", "openai", "gpt-5.4", "openai/gpt-5.4"),
	}
	outcome, err := refreshOpenCodeManagedModelDocument(
		AgentConfig{Name: "OpenCode"}, doc, models, nil,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	if outcome.Doc["model"] != "preloop/anthropic/claude-fable-5" {
		t.Errorf("authorized selection must be preserved, got %#v", outcome.Doc["model"])
	}
	want := []string{
		"anthropic/claude-fable-5",
		"anthropic/claude-fable-5-1",
		"openai/gpt-5.4",
	}
	if !reflect.DeepEqual(sortedCopy(outcome.After), want) {
		t.Errorf("unexpected refreshed model map: %#v", outcome.After)
	}
	options := outcome.Doc["provider"].(map[string]interface{})["preloop"].(map[string]interface{})["options"].(map[string]interface{})
	if options["apiKey"] != "opencode-durable-token" {
		t.Errorf("managed token must be preserved, got %#v", options["apiKey"])
	}
	if options["baseURL"] != "https://preloop.example/openai/v1" {
		t.Errorf("managed base URL must be preserved, got %#v", options["baseURL"])
	}
	if !reflect.DeepEqual(outcome.added(), []string{"anthropic/claude-fable-5-1", "openai/gpt-5.4"}) {
		t.Errorf("unexpected added diff: %#v", outcome.added())
	}
}

func TestRefreshOpenCodeManagedModelDocumentFallsBackWhenSelectionUnauthorized(t *testing.T) {
	doc := openCodeRefreshFixtureDoc()
	doc["model"] = "preloop/anthropic/claude-fable-4"
	defaultModel := refreshTestModel("m-gpt", "openai", "gpt-5.4", "openai/gpt-5.4")
	defaultModel.IsDefault = true
	models := []aiModelResponse{
		defaultModel,
		refreshTestModel("m-fable", "anthropic", "claude-fable-5", "anthropic/claude-fable-5"),
	}
	outcome, err := refreshOpenCodeManagedModelDocument(
		AgentConfig{Name: "OpenCode"}, doc, models, nil,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	if outcome.Doc["model"] != "preloop/openai/gpt-5.4" {
		t.Errorf(
			"unauthorized selection must fall back to the account default, got %#v",
			outcome.Doc["model"],
		)
	}
	if len(outcome.Warnings) == 0 ||
		!strings.Contains(outcome.Warnings[0], "no longer authorized") {
		t.Errorf("expected an unauthorized-fallback warning, got %#v", outcome.Warnings)
	}
}

func TestRefreshOpenCodeManagedModelDocumentSkipsWithoutManagedProvider(t *testing.T) {
	outcome, err := refreshOpenCodeManagedModelDocument(
		AgentConfig{Name: "OpenCode"},
		map[string]interface{}{"model": "anthropic/claude-fable-5"},
		claudeRefreshFixtureModels(),
		nil,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	if outcome.SkipReason == "" {
		t.Fatal("expected a skip reason for a config without the managed provider")
	}
}

func openClawRefreshFixtureDoc() map[string]interface{} {
	return map[string]interface{}{
		"models": map[string]interface{}{
			"providers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"baseUrl":    "https://preloop.example/openai/v1",
					"apiKey":     "openclaw-durable-token",
					"api":        "openai-completions",
					"authHeader": true,
					"models": []interface{}{
						map[string]interface{}{
							"id":            "anthropic/claude-fable-5",
							"name":          "anthropic/claude-fable-5",
							"api":           "openai-completions",
							"contextWindow": float64(200000),
							"compat": map[string]interface{}{
								"supportsPromptCacheKey": true,
							},
						},
					},
				},
			},
		},
		"agents": map[string]interface{}{
			"defaults": map[string]interface{}{
				"model": "preloop/anthropic/claude-fable-5",
			},
		},
		"mcp": map[string]interface{}{
			"servers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"url": "https://preloop.example/mcp/v1",
				},
			},
		},
	}
}

func TestRefreshOpenClawManagedModelDocumentExpandsProviderModels(t *testing.T) {
	doc := openClawRefreshFixtureDoc()
	models := []aiModelResponse{
		refreshTestModel("m-fable", "anthropic", "claude-fable-5", "anthropic/claude-fable-5"),
		refreshTestModel("m-fable-51", "anthropic", "claude-fable-5-1", "anthropic/claude-fable-5-1"),
	}
	outcome, err := refreshOpenClawManagedModelDocument(
		AgentConfig{Name: "OpenClaw"}, doc, models, nil,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	provider := lookupValue(outcome.Doc, "models", "providers", "preloop").(map[string]interface{})
	if provider["apiKey"] != "openclaw-durable-token" {
		t.Errorf("managed token must be preserved, got %#v", provider["apiKey"])
	}
	entries := provider["models"].([]interface{})
	ids := make([]string, 0, len(entries))
	var keptEntry map[string]interface{}
	for _, raw := range entries {
		entry := raw.(map[string]interface{})
		ids = append(ids, entry["id"].(string))
		if entry["id"] == "anthropic/claude-fable-5" {
			keptEntry = entry
		}
	}
	want := []string{"anthropic/claude-fable-5", "anthropic/claude-fable-5-1"}
	if !reflect.DeepEqual(sortedCopy(ids), want) {
		t.Errorf("unexpected provider model ids: %#v", ids)
	}
	if keptEntry == nil || keptEntry["contextWindow"] != float64(200000) {
		t.Errorf("kept entries must preserve catalog fields, got %#v", keptEntry)
	}
	if selector := lookupString(outcome.Doc, "agents", "defaults", "model"); selector != "preloop/anthropic/claude-fable-5" {
		t.Errorf("authorized selector must be preserved, got %q", selector)
	}
	if !reflect.DeepEqual(outcome.added(), []string{"anthropic/claude-fable-5-1"}) {
		t.Errorf("unexpected added diff: %#v", outcome.added())
	}
}

func TestRefreshOpenClawManagedModelDocumentRepointsUnauthorizedSelector(t *testing.T) {
	doc := openClawRefreshFixtureDoc()
	defaultModel := refreshTestModel("m-opus", "anthropic", "claude-opus-4-6", "anthropic/claude-opus-4-6")
	defaultModel.IsDefault = true
	// claude-fable-5 is gone from the catalog: the selector must repoint.
	outcome, err := refreshOpenClawManagedModelDocument(
		AgentConfig{Name: "OpenClaw"}, doc, []aiModelResponse{defaultModel}, nil,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	if selector := lookupString(outcome.Doc, "agents", "defaults", "model"); selector != "preloop/anthropic/claude-opus-4-6" {
		t.Errorf("unauthorized selector must repoint to the account default, got %q", selector)
	}
	if len(outcome.Warnings) == 0 ||
		!strings.Contains(outcome.Warnings[0], "no longer authorized") {
		t.Errorf("expected a repoint warning, got %#v", outcome.Warnings)
	}
	if !reflect.DeepEqual(outcome.removed(), []string{"anthropic/claude-fable-5"}) {
		t.Errorf("unexpected removed diff: %#v", outcome.removed())
	}
}

func TestRefreshGeminiManagedModelDocumentPreservesAuthorizedPin(t *testing.T) {
	doc := map[string]interface{}{
		"apiKey":  "gemini-durable-token",
		"baseUrl": "https://preloop.example/gemini",
		"model": map[string]interface{}{
			"name": "gemini-2.5-pro",
		},
	}
	models := []aiModelResponse{
		refreshTestModel("m-gem", "google", "gemini-2.5-pro", "google/gemini-2.5-pro"),
		refreshTestModel("m-gpt", "openai", "gpt-5.4", "openai/gpt-5.4"),
	}
	outcome, err := refreshGeminiManagedModelDocument(
		AgentConfig{Name: "Gemini CLI"}, doc, models, nil,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	if name := lookupString(outcome.Doc, "model", "name"); name != "gemini-2.5-pro" {
		t.Errorf("authorized pin must be preserved, got %q", name)
	}
	if outcome.Doc["apiKey"] != "gemini-durable-token" {
		t.Errorf("managed token must be preserved, got %#v", outcome.Doc["apiKey"])
	}
	if outcome.changed() {
		t.Errorf("no diff expected for an authorized pin: %#v -> %#v", outcome.Before, outcome.After)
	}
}

func TestRefreshHermesManagedModelDocumentFallsBackWhenPinUnauthorized(t *testing.T) {
	doc := map[string]interface{}{
		"model": map[string]interface{}{
			"provider": "custom",
			"base_url": "https://preloop.example/openai/v1",
			"api_key":  "hermes-durable-token",
			"default":  "anthropic/claude-fable-4",
		},
	}
	defaultModel := refreshTestModel("m-fable", "anthropic", "claude-fable-5", "anthropic/claude-fable-5")
	defaultModel.IsDefault = true
	outcome, err := refreshHermesManagedModelDocument(
		AgentConfig{Name: "Hermes"}, doc, []aiModelResponse{defaultModel}, nil,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	model := outcome.Doc["model"].(map[string]interface{})
	if model["default"] != "anthropic/claude-fable-5" {
		t.Errorf("unauthorized pin must fall back to the account default, got %#v", model["default"])
	}
	if model["api_key"] != "hermes-durable-token" {
		t.Errorf("managed token must be preserved, got %#v", model["api_key"])
	}
	if len(outcome.Warnings) == 0 ||
		!strings.Contains(outcome.Warnings[0], "no longer authorized") {
		t.Errorf("expected an unauthorized-fallback warning, got %#v", outcome.Warnings)
	}
}

func TestRefreshManagedModelDocumentCodexIsNoop(t *testing.T) {
	outcome, err := refreshAgentManagedModels(
		nil, AgentConfig{Name: "Codex CLI"}, nil, nil,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	if !outcome.Noop {
		t.Fatal("Codex CLI refresh must be a no-op")
	}
	if !strings.Contains(outcome.SkipReason, "dynamically") {
		t.Errorf("Codex no-op must explain the dynamic model list, got %q", outcome.SkipReason)
	}
}

func TestStaleModelCatalogHint(t *testing.T) {
	// Catalog newer than or equal to the CLI's built-in GA table: no hint.
	current := []aiModelResponse{
		refreshTestModel("m1", "anthropic", "claude-fable-5", "anthropic/claude-fable-5"),
	}
	if hint := staleModelCatalogHint(current); hint != "" {
		t.Errorf("no hint expected for a current catalog, got %q", hint)
	}
	// Catalog older than the built-in table: hint at `preloop models sync`.
	stale := []aiModelResponse{
		refreshTestModel("m1", "anthropic", "claude-fable-4", "anthropic/claude-fable-4"),
	}
	if hint := staleModelCatalogHint(stale); !strings.Contains(hint, "preloop models sync") {
		t.Errorf("expected a models-sync hint for a stale catalog, got %q", hint)
	}
	// No Anthropic models at all: nothing to compare against.
	if hint := staleModelCatalogHint(nil); hint != "" {
		t.Errorf("no hint expected without Anthropic models, got %q", hint)
	}
}

// End-to-end flow of `preloop agents refresh` for an onboarded OpenCode
// config: the account grows a newly released model, the refresh rewrites the
// managed provider's models map in place, preserves the token and the
// selected model, and updates the local enrollment snapshot.
func TestExecuteAgentsRefreshEndToEndOpenCode(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	cfgDir := filepath.Join(home, ".config", "opencode")
	if err := os.MkdirAll(cfgDir, 0o755); err != nil {
		t.Fatal(err)
	}
	cfgPath := filepath.Join(cfgDir, "config.json")
	initial := map[string]interface{}{
		"model": "preloop/anthropic/claude-fable-5-20260415",
		"provider": map[string]interface{}{
			"preloop": map[string]interface{}{
				"npm": "@ai-sdk/openai-compatible",
				"options": map[string]interface{}{
					"baseURL": "https://preloop.example/openai/v1",
					"apiKey":  "managed-token",
				},
				"models": map[string]interface{}{
					"anthropic/claude-fable-5-20260415": map[string]interface{}{
						"name": "anthropic/claude-fable-5-20260415",
					},
				},
			},
		},
		"mcp": map[string]interface{}{
			"preloop": map[string]interface{}{
				"type": "remote",
				"url":  "https://preloop.example/api/v1/mcp/sse",
			},
		},
	}
	initialJSON, err := json.MarshalIndent(initial, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cfgPath, initialJSON, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := saveLocalEnrollmentState(&localEnrollmentState{
		AgentName:         "OpenCode",
		DisplayName:       "OpenCode",
		ConfigPath:        cfgPath,
		ConfigExisted:     true,
		ManagedServerName: "preloop",
		ManagedServerURL:  "https://preloop.example/api/v1/mcp/sse",
		AppliedAt:         time.Now().UTC(),
	}); err != nil {
		t.Fatalf("saveLocalEnrollmentState: %v", err)
	}

	accountModels := []map[string]interface{}{
		{
			"id":               "11111111-1111-1111-1111-111111111111",
			"name":             "claude-fable-5-20260415",
			"provider_name":    "anthropic",
			"model_identifier": "claude-fable-5-20260415",
			"meta_data": map[string]interface{}{
				"gateway": map[string]interface{}{
					"enabled":     true,
					"model_alias": "anthropic/claude-fable-5-20260415",
				},
			},
			"credential_type": "api_key",
			"has_api_key":     true,
			"is_default":      true,
		},
		{
			// The newly released model the onboarding snapshot is missing.
			"id":               "22222222-2222-2222-2222-222222222222",
			"name":             "claude-fable-5-1-20260901",
			"provider_name":    "anthropic",
			"model_identifier": "claude-fable-5-1-20260901",
			"meta_data": map[string]interface{}{
				"gateway": map[string]interface{}{
					"enabled":     true,
					"model_alias": "anthropic/claude-fable-5-1-20260901",
				},
			},
			"credential_type": "api_key",
			"has_api_key":     true,
		},
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasPrefix(r.URL.Path, "/api/v1/ai-models"):
			_ = json.NewEncoder(w).Encode(accountModels)
		case strings.HasPrefix(r.URL.Path, "/api/v1/agents"):
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"items": []interface{}{}})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	agent := AgentConfig{Name: "OpenCode", ConfigPath: cfgPath}
	var out strings.Builder
	if err := executeAgentsRefresh(client, []AgentConfig{agent}, &out); err != nil {
		t.Fatalf("executeAgentsRefresh: %v", err)
	}

	rendered := out.String()
	for _, want := range []string{
		"Refreshing OpenCode",
		"+ anthropic/claude-fable-5-1-20260901",
		"Selected model: anthropic/claude-fable-5-20260415",
		"Refresh complete: 1 refreshed, 0 already up to date, 0 skipped, 0 failed.",
	} {
		if !strings.Contains(rendered, want) {
			t.Fatalf("output missing %q:\n%s", want, rendered)
		}
	}

	refreshedDoc, err := loadJSONDocument(cfgPath)
	if err != nil {
		t.Fatalf("reload config: %v", err)
	}
	providers, _ := asObjectMap(refreshedDoc["provider"])
	preloopProvider, _ := asObjectMap(providers["preloop"])
	options, _ := asObjectMap(preloopProvider["options"])
	if got := lookupString(options, "apiKey"); got != "managed-token" {
		t.Fatalf("managed token must be preserved, got %q", got)
	}
	models, _ := asObjectMap(preloopProvider["models"])
	if _, ok := models["anthropic/claude-fable-5-1-20260901"]; !ok {
		t.Fatalf("new model missing from provider models: %#v", models)
	}
	if _, ok := models["anthropic/claude-fable-5-20260415"]; !ok {
		t.Fatalf("existing model must be preserved: %#v", models)
	}
	if got := lookupString(refreshedDoc, "model"); got != "preloop/anthropic/claude-fable-5-20260415" {
		t.Fatalf("selected model must be preserved, got %q", got)
	}

	state, err := loadLocalEnrollmentState(agent)
	if err != nil {
		t.Fatalf("reload enrollment state: %v", err)
	}
	if state.ManagedConfig == nil {
		t.Fatal("enrollment snapshot was not updated with the refreshed config")
	}
}
