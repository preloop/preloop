package cmd

// Regression tests for issue #952: Claude Code onboarding and refresh stop
// writing the stock opus/sonnet/haiku family pins by default so a new Anthropic
// release arrives with the next Claude Code binary update (the gateway
// auto-registers the unseen claude-* id on first use) instead of waiting for a
// manual `preloop agents refresh`. Fable keeps its pair because stock Claude
// Code has no built-in fable default; a custom/non-family model is still pinned
// explicitly so its background and subagent requests do not 404.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestApplyClaudeManagedGatewayUnpinnedStockFamilyDropsPins(t *testing.T) {
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{},
	}
	plan, err := applyClaudeManagedGateway(
		plan,
		"https://preloop.example",
		"claude-durable-token",
		"anthropic/claude-sonnet-4-5",
		[]string{
			"anthropic/claude-opus-4-6",
			"anthropic/claude-haiku-4-5",
			"anthropic/claude-fable-5",
		},
		false,
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}

	env := plan.ManagedDocument["env"].(map[string]interface{})
	for _, key := range []string{
		"ANTHROPIC_MODEL",
		"ANTHROPIC_DEFAULT_OPUS_MODEL",
		"ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
		"ANTHROPIC_DEFAULT_SONNET_MODEL",
		"ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
	} {
		if value, ok := env[key]; ok {
			t.Fatalf("unpinned stock onboarding must not write %s: %#v", key, value)
		}
	}
	if env["ANTHROPIC_DEFAULT_FABLE_MODEL"] != "anthropic/claude-fable-5" {
		t.Errorf("fable pair must stay pinned: %#v", env)
	}
	if env["ANTHROPIC_DEFAULT_FABLE_MODEL_NAME"] != "Fable (Preloop)" {
		t.Errorf("fable name must stay pinned: %#v", env)
	}
	if env["ANTHROPIC_CUSTOM_MODEL_OPTION"] != "anthropic/claude-sonnet-4-5" {
		t.Errorf("custom model option must stay: %#v", env)
	}

	found := false
	for _, note := range plan.Notes {
		if strings.Contains(note, "stock family defaults") {
			found = true
		}
	}
	if !found {
		t.Errorf("expected an unpinned-defaults note, got %#v", plan.Notes)
	}
}

func TestApplyClaudeManagedGatewayUnpinnedFableKeepsPin(t *testing.T) {
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{},
	}
	plan, err := applyClaudeManagedGateway(
		plan,
		"https://preloop.example",
		"claude-durable-token",
		"anthropic/claude-fable-5",
		nil,
		false,
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}
	env := plan.ManagedDocument["env"].(map[string]interface{})
	if plan.ManagedDocument["model"] != "fable" {
		t.Errorf("fable selection must stay in settings.model, got %#v", plan.ManagedDocument["model"])
	}
	if env["ANTHROPIC_MODEL"] != "fable" {
		t.Errorf("fable has no built-in default, ANTHROPIC_MODEL must stay: %#v", env)
	}
	if env["ANTHROPIC_DEFAULT_FABLE_MODEL"] != "anthropic/claude-fable-5" {
		t.Errorf("fable family alias must stay pinned: %#v", env)
	}
	for _, key := range []string{
		"ANTHROPIC_DEFAULT_OPUS_MODEL",
		"ANTHROPIC_DEFAULT_SONNET_MODEL",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL",
	} {
		if _, ok := env[key]; ok {
			t.Errorf("unpinned fable onboarding must not write %s", key)
		}
	}
}

func TestApplyClaudeManagedGatewayUnpinnedCustomModelMapsSelectors(t *testing.T) {
	// A non-Anthropic model still needs every selector pointed at it: stock
	// Claude Code's background (haiku) and subagent requests would otherwise
	// ask the gateway for ids the custom model cannot serve.
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{},
	}
	plan, err := applyClaudeManagedGateway(
		plan,
		"https://preloop.example",
		"claude-durable-token",
		"moonshot/kimi-k3-0905",
		nil,
		false,
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}
	env := plan.ManagedDocument["env"].(map[string]interface{})
	for _, key := range []string{
		"ANTHROPIC_MODEL",
		"ANTHROPIC_DEFAULT_OPUS_MODEL",
		"ANTHROPIC_DEFAULT_SONNET_MODEL",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL",
		"CLAUDE_CODE_SUBAGENT_MODEL",
	} {
		if env[key] != "moonshot/kimi-k3-0905" {
			t.Fatalf("expected %s pinned to the custom alias, got %#v", key, env[key])
		}
	}
}

func TestRefreshClaudeManagedModelDocumentUnpinnedRemovesStockPins(t *testing.T) {
	doc := claudeRefreshFixtureDoc()
	// Unmanaged keys and hooks must survive untouched.
	doc["hooks"] = map[string]interface{}{
		"PreToolUse": []interface{}{map[string]interface{}{"matcher": "Bash"}},
	}
	doc["permissions"] = map[string]interface{}{"allow": []interface{}{"Read"}}
	outcome, err := refreshClaudeManagedModelDocument(
		AgentConfig{Name: "Claude Code"}, doc, claudeRefreshFixtureModels(), nil, false,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	env := outcome.Doc["env"].(map[string]interface{})
	for _, key := range []string{
		"ANTHROPIC_DEFAULT_OPUS_MODEL",
		"ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
		"ANTHROPIC_DEFAULT_SONNET_MODEL",
		"ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
	} {
		if value, ok := env[key]; ok {
			t.Fatalf("unpinned refresh must remove %s, got %#v", key, value)
		}
	}
	// Every other managed key stays byte-identical.
	if env["ANTHROPIC_DEFAULT_FABLE_MODEL"] != "anthropic/claude-fable-5" {
		t.Errorf("fable pin must survive: %#v", env)
	}
	if env["ANTHROPIC_MODEL"] != "fable" {
		t.Errorf("fable selection must survive in selector form: %#v", env)
	}
	if outcome.Doc["model"] != "fable" {
		t.Errorf("settings.model must survive: %#v", outcome.Doc["model"])
	}
	if env["ANTHROPIC_API_KEY"] != "claude-durable-token" ||
		env["ANTHROPIC_BASE_URL"] != "https://preloop.example/anthropic" {
		t.Errorf("managed token/base URL must survive: %#v", env)
	}
	if _, ok := outcome.Doc["mcpServers"].(map[string]interface{})["preloop"]; !ok {
		t.Error("managed MCP entry must survive")
	}
	if _, ok := outcome.Doc["hooks"].(map[string]interface{})["PreToolUse"]; !ok {
		t.Error("unmanaged hooks must survive")
	}
	if _, ok := outcome.Doc["permissions"].(map[string]interface{})["allow"]; !ok {
		t.Error("unmanaged keys must survive")
	}

	wantRemoved := []string{
		"anthropic/claude-opus-4-6",
		"anthropic/claude-sonnet-4-5",
		"anthropic/claude-haiku-4-5",
	}
	if !reflect.DeepEqual(outcome.removed(), wantRemoved) {
		t.Errorf("unexpected removed diff: %#v, want %#v", outcome.removed(), wantRemoved)
	}
	if len(outcome.Notices) == 0 ||
		!strings.Contains(outcome.Notices[0], "ANTHROPIC_DEFAULT_OPUS_MODEL") {
		t.Errorf("expected a one-line removal explanation, got %#v", outcome.Notices)
	}
}

func TestRefreshClaudeManagedModelDocumentUnpinnedNoticeOnlyWhenPinsRemoved(t *testing.T) {
	doc := claudeRefreshFixtureDoc()
	first, err := refreshClaudeManagedModelDocument(
		AgentConfig{Name: "Claude Code"}, doc, claudeRefreshFixtureModels(), nil, false,
	)
	if err != nil {
		t.Fatalf("unexpected first refresh error: %v", err)
	}
	if len(first.Notices) == 0 {
		t.Fatal("first removal must explain itself")
	}
	second, err := refreshClaudeManagedModelDocument(
		AgentConfig{Name: "Claude Code"}, first.Doc, claudeRefreshFixtureModels(), nil, false,
	)
	if err != nil {
		t.Fatalf("unexpected second refresh error: %v", err)
	}
	if len(second.Notices) != 0 {
		t.Errorf("later flag-less runs have nothing to remove and must stay quiet, got %#v", second.Notices)
	}
	if second.changed() {
		t.Errorf("a second unpinned refresh should be a no-op: %#v -> %#v", second.Before, second.After)
	}
}

func TestRefreshClaudeManagedModelDocumentUnpinnedStockSelectionDropsAnthropicModel(t *testing.T) {
	doc := claudeRefreshFixtureDoc()
	env := doc["env"].(map[string]interface{})
	env["ANTHROPIC_MODEL"] = "sonnet"
	// A previous non-family enrollment may have left a stale settings.model;
	// the unpinned stock path replaces it with the family selector (which
	// Claude Code resolves to its own built-in default).
	doc["model"] = "moonshot/kimi-k3"

	outcome, err := refreshClaudeManagedModelDocument(
		AgentConfig{Name: "Claude Code"}, doc, claudeRefreshFixtureModels(), nil, false,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	refreshedEnv := outcome.Doc["env"].(map[string]interface{})
	if value, ok := refreshedEnv["ANTHROPIC_MODEL"]; ok {
		t.Errorf("stock selection must not keep an ANTHROPIC_MODEL pin, got %#v", value)
	}
	if refreshedEnv["ANTHROPIC_DEFAULT_FABLE_MODEL"] != "anthropic/claude-fable-5" {
		t.Errorf("fable pair must stay: %#v", refreshedEnv)
	}
	if outcome.Doc["model"] != "sonnet" {
		t.Errorf("settings.model must hold the stock family selector, got %#v", outcome.Doc["model"])
	}
}

func TestResolveEnrollmentPinModelFamiliesPrefersExplicitFlagAndPersistedState(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	agent := AgentConfig{
		Name:       "Claude Code",
		ConfigPath: filepath.Join(home, ".claude", "settings.json"),
	}

	// No enrollment state: the unpinned default.
	if resolveEnrollmentPinModelFamilies(agent, managedEnrollmentOptions{}) {
		t.Fatal("a fresh enrollment must default to unpinned")
	}

	if err := saveLocalEnrollmentState(&localEnrollmentState{
		AgentName:        "Claude Code",
		ConfigPath:       agent.ConfigPath,
		PinModelFamilies: true,
	}); err != nil {
		t.Fatalf("saveLocalEnrollmentState: %v", err)
	}
	if !resolveEnrollmentPinModelFamilies(agent, managedEnrollmentOptions{}) {
		t.Fatal("a persisted pin must be honoured without the flag")
	}
	if resolveEnrollmentPinModelFamilies(agent, managedEnrollmentOptions{
		PinModelFamilies:    false,
		PinModelFamiliesSet: true,
	}) {
		t.Fatal("an explicit --pin-model-families=false must override the persisted choice")
	}

	if err := saveLocalEnrollmentState(&localEnrollmentState{
		AgentName:        "Claude Code",
		ConfigPath:       agent.ConfigPath,
		PinModelFamilies: false,
	}); err != nil {
		t.Fatalf("saveLocalEnrollmentState: %v", err)
	}
	if !resolveEnrollmentPinModelFamilies(agent, managedEnrollmentOptions{
		PinModelFamilies:    true,
		PinModelFamiliesSet: true,
	}) {
		t.Fatal("an explicit --pin-model-families must override the persisted choice")
	}
}

func TestClaudePinModelFamiliesFlagRegistered(t *testing.T) {
	if agentsEnrollCmd.Flags().Lookup("pin-model-families") == nil {
		t.Fatal("onboard must expose --pin-model-families")
	}
	if agentsRefreshCmd.Flags().Lookup("pin-model-families") == nil {
		t.Fatal("refresh must expose --pin-model-families")
	}
}

func TestUnpinnedClaudeOnboardConfigPassesManagedValidation(t *testing.T) {
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{
			"mcpServers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"url": "https://preloop.example/mcp/v1",
					"headers": map[string]interface{}{
						"Authorization": "Bearer durable-token",
					},
					"transport": "http",
				},
			},
		},
	}
	plan, err := applyClaudeManagedGateway(
		plan,
		"https://preloop.example",
		"durable-token",
		"anthropic/claude-sonnet-4-5",
		[]string{"anthropic/claude-fable-5"},
		false,
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}
	result := managedMCPAdapterForAgent(AgentConfig{Name: "Claude Code"}).ValidateManagedConfig(
		plan.ManagedDocument,
		"https://preloop.example",
	)
	if result["validation_passed"] != true {
		t.Fatalf("an unpinned Claude Code config must still validate, got %+v", result)
	}
	if result["gateway_provider_ok"] != true {
		t.Fatalf("the custom model option must keep the gateway marked configured, got %+v", result)
	}
}

func TestExecuteAgentsRefreshHonoursPersistedPinModelFamilies(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	cfgDir := filepath.Join(home, ".claude")
	if err := os.MkdirAll(cfgDir, 0o755); err != nil {
		t.Fatal(err)
	}
	cfgPath := filepath.Join(cfgDir, "settings.json")
	initial, err := json.MarshalIndent(claudeRefreshFixtureDoc(), "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cfgPath, initial, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := saveLocalEnrollmentState(&localEnrollmentState{
		AgentName:        "Claude Code",
		DisplayName:      "Claude Code",
		ConfigPath:       cfgPath,
		ConfigExisted:    true,
		AppliedAt:        time.Now().UTC(),
		PinModelFamilies: true,
	}); err != nil {
		t.Fatalf("saveLocalEnrollmentState: %v", err)
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasPrefix(r.URL.Path, "/api/v1/ai-models"):
			_ = json.NewEncoder(w).Encode(claudeRefreshFixtureModels())
		case strings.HasPrefix(r.URL.Path, "/api/v1/agents"):
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"items": []interface{}{}})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	agent := AgentConfig{Name: "Claude Code", ConfigPath: cfgPath}

	// Flag not passed: the persisted pin=true must be honoured.
	var out strings.Builder
	if err := executeAgentsRefresh(client, []AgentConfig{agent}, &out, false, false); err != nil {
		t.Fatalf("executeAgentsRefresh (persisted pin): %v", err)
	}
	doc, err := loadJSONDocument(cfgPath)
	if err != nil {
		t.Fatalf("reload config: %v", err)
	}
	env := doc["env"].(map[string]interface{})
	for _, key := range []string{
		"ANTHROPIC_DEFAULT_OPUS_MODEL",
		"ANTHROPIC_DEFAULT_SONNET_MODEL",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL",
	} {
		if _, ok := env[key]; !ok {
			t.Fatalf("persisted pin must survive a flag-less refresh: %s missing", key)
		}
	}

	// Explicit --pin-model-families=false: drop the pins and persist the
	// override so the next flag-less run stays unpinned.
	out.Reset()
	if err := executeAgentsRefresh(client, []AgentConfig{agent}, &out, false, true); err != nil {
		t.Fatalf("executeAgentsRefresh (explicit unpin): %v", err)
	}
	doc, err = loadJSONDocument(cfgPath)
	if err != nil {
		t.Fatalf("reload config: %v", err)
	}
	env = doc["env"].(map[string]interface{})
	for _, key := range []string{
		"ANTHROPIC_DEFAULT_OPUS_MODEL",
		"ANTHROPIC_DEFAULT_SONNET_MODEL",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL",
	} {
		if value, ok := env[key]; ok {
			t.Fatalf("explicit unpin must remove %s, got %#v", key, value)
		}
	}
	state, err := loadLocalEnrollmentState(agent)
	if err != nil {
		t.Fatalf("loadLocalEnrollmentState: %v", err)
	}
	if state.PinModelFamilies {
		t.Fatal("an explicit refresh override must be persisted for later flag-less runs")
	}
	if !strings.Contains(out.String(), "Note: Removed the managed stock Claude Code family pins") {
		t.Errorf("refresh output must explain the removal, got:\n%s", out.String())
	}
}

func TestRefreshUnpinnedClaudePreservesSettingsFamilySelection(t *testing.T) {
	doc := claudeRefreshFixtureDoc()
	env := doc["env"].(map[string]interface{})
	delete(env, "ANTHROPIC_MODEL")
	doc["model"] = "sonnet"
	// A custom option may still name a different family after /model switching.
	env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = "anthropic/claude-fable-5"
	outcome, err := refreshClaudeManagedModelDocument(AgentConfig{Name: "Claude Code"}, doc, claudeRefreshFixtureModels(), nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if outcome.Doc["model"] != "sonnet" {
		t.Fatalf("settings selection overwritten: %#v", outcome.Doc["model"])
	}
	if _, ok := outcome.Doc["env"].(map[string]interface{})["ANTHROPIC_MODEL"]; ok {
		t.Fatal("stock family must remain unpinned")
	}
}

func TestRefreshUnpinnedCustomModelDoesNotClaimPinsRemoved(t *testing.T) {
	doc := claudeRefreshFixtureDoc()
	env := doc["env"].(map[string]interface{})
	env["ANTHROPIC_MODEL"] = "example/custom-model"
	models := append(claudeRefreshFixtureModels(), refreshTestModel("custom", "example", "custom-model", "example/custom-model"))
	outcome, err := refreshClaudeManagedModelDocument(AgentConfig{Name: "Claude Code"}, doc, models, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if len(outcome.Notices) != 0 {
		t.Fatalf("custom routing retains family pins: %#v", outcome.Notices)
	}
	if outcome.Doc["env"].(map[string]interface{})["ANTHROPIC_DEFAULT_HAIKU_MODEL"] != "example/custom-model" {
		t.Fatal("background routing lost")
	}
}

func TestUnpinnedClaudeOnboardRefreshOffboardRestoresOriginalSettings(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	t.Setenv("PATH", t.TempDir()) // Never invoke an installed Claude binary.
	agent := AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}
	original := []byte(`{"model":"sonnet","env":{"LOCAL_SETTING":"keep","ANTHROPIC_AUTH_TOKEN":"local-token"},"hooks":{"PreToolUse":[{"matcher":"Read","hooks":[{"type":"command","command":"echo local"}]}]},"permissions":{"allow":["Read"]}}`)
	if err := os.MkdirAll(filepath.Dir(agent.ConfigPath), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(agent.ConfigPath, original, 0600); err != nil {
		t.Fatal(err)
	}
	doc, err := loadAgentConfigDocument(agent)
	if err != nil {
		t.Fatal(err)
	}
	plan, err := applyClaudeManagedGateway(managedMCPEnrollmentPlan{Agent: agent, ManagedDocument: doc}, "https://preloop.example", "managed-token", "anthropic/claude-sonnet-4-5", []string{"anthropic/claude-fable-5"}, false)
	if err != nil {
		t.Fatal(err)
	}
	state, err := createLocalEnrollmentBackup(agent, true, original, plan)
	if err != nil {
		t.Fatal(err)
	}
	if err := saveLocalEnrollmentState(state); err != nil {
		t.Fatal(err)
	}
	if err := writeAgentConfigDocument(agent, plan.ManagedDocument); err != nil {
		t.Fatal(err)
	}
	outcome, err := refreshAgentManagedModels(nil, agent, claudeRefreshFixtureModels(), claudeLiveModelList{}, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if outcome.Doc["env"].(map[string]interface{})["LOCAL_SETTING"] != "keep" {
		t.Fatal("unmanaged env lost")
	}
	if !reflect.DeepEqual(outcome.Doc["hooks"], doc["hooks"]) {
		t.Fatal("unmanaged hooks lost")
	}
	if _, err := restoreAgentFromBackup(agent, state); err != nil {
		t.Fatal(err)
	}
	restored, err := os.ReadFile(agent.ConfigPath)
	if err != nil {
		t.Fatal(err)
	}
	var want, got map[string]interface{}
	if err := json.Unmarshal(original, &want); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(restored, &got); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("original settings not restored: %s", restored)
	}
}
