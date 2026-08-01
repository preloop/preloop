package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
)

func TestListGatewayModelChoicesIncludesInferredAndAccount(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/ai-models" {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode([]aiModelResponse{
			{
				ID:              "m1",
				ProviderName:    "anthropic",
				ModelIdentifier: "claude-opus-4-6",
				CredentialType:  "oauth_anthropic_claude_code",
				HasAPIKey:       true,
				MetaData: map[string]interface{}{
					"gateway": map[string]interface{}{"model_alias": "anthropic/claude-opus-4-6"},
				},
			},
			{
				ID:              "m2",
				ProviderName:    "openai",
				ModelIdentifier: "gpt-ignore",
				HasAPIKey:       false,
			},
		})
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "token")
	choices := listGatewayModelChoices(client, &managedGatewayUpstream{
		ManagedModelAlias: "openai/gpt-5.6-sol",
		CredentialType:    "oauth_openai_codex",
		APIKey:            "sk-test",
	})
	if len(choices) != 2 {
		t.Fatalf("expected 2 choices, got %#v", choices)
	}
	if !choices[0].Inferred || choices[0].Alias != "openai/gpt-5.6-sol" {
		t.Fatalf("expected inferred first, got %#v", choices[0])
	}
	if !strings.Contains(choices[0].Label, "subscription/OAuth") {
		t.Fatalf("expected credential label on inferred choice, got %q", choices[0].Label)
	}
	if choices[1].Alias != "anthropic/claude-opus-4-6" {
		t.Fatalf("expected account model second, got %#v", choices[1])
	}
}

func TestResolveManagedModelSelectionUsesPreferredModelFlag(t *testing.T) {
	var out bytes.Buffer
	alias, model, err := resolveManagedModelSelection(
		nil,
		AgentConfig{Name: "Hermes"},
		&managedGatewayUpstream{ManagedModelAlias: "openai/gpt-5.6-sol"},
		managedEnrollmentOptions{
			PreferredModel: "anthropic/claude-opus-4-6",
			AutoApprove:    true,
			Output:         &out,
		},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if alias != "anthropic/claude-opus-4-6" {
		t.Fatalf("expected preferred alias, got %q", alias)
	}
	if model != nil {
		t.Fatalf("expected nil account model for unknown preferred alias")
	}
	if strings.Contains(out.String(), "Using inferred model") {
		t.Fatalf("preferred model should skip non-interactive notice, got %q", out.String())
	}
}

func TestResolveManagedModelSelectionNonInteractiveFallsBackWithNotice(t *testing.T) {
	var out bytes.Buffer
	alias, _, err := resolveManagedModelSelection(
		nil,
		AgentConfig{Name: "Hermes"},
		&managedGatewayUpstream{ManagedModelAlias: "openai/gpt-5.6-sol"},
		managedEnrollmentOptions{
			AutoApprove: true,
			Output:      &out,
		},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if alias != "openai/gpt-5.6-sol" {
		t.Fatalf("expected inferred fallback, got %q", alias)
	}
	if !strings.Contains(out.String(), "Using inferred model openai/gpt-5.6-sol") {
		t.Fatalf("expected non-interactive notice, got %q", out.String())
	}
	if !strings.Contains(out.String(), "--model") {
		t.Fatalf("expected --model hint in notice, got %q", out.String())
	}
}

func TestPromptForManagedModelSelectsByNumber(t *testing.T) {
	var out bytes.Buffer
	selected, err := promptForManagedModel(
		AgentConfig{Name: "Hermes"},
		[]gatewayModelChoice{
			{Alias: "openai/gpt-5.6-sol", Label: "openai/gpt-5.6-sol", Inferred: true},
			{Alias: "anthropic/claude-opus-4-6", Label: "anthropic/claude-opus-4-6"},
		},
		"openai/gpt-5.6-sol",
		strings.NewReader("2\n"),
		&out,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if selected != "anthropic/claude-opus-4-6" {
		t.Fatalf("expected second choice, got %q", selected)
	}
	if !strings.Contains(out.String(), "Select the managed model") {
		t.Fatalf("expected picker prompt, got %q", out.String())
	}
}

func TestApplySelectedModelToUpstreamOverridesAlias(t *testing.T) {
	upstream := &managedGatewayUpstream{
		ProviderName:      "openai",
		ModelIdentifier:   "gpt-5.6-sol",
		ManagedModelAlias: "openai/gpt-5.6-sol",
		APIKey:            "sk-local",
	}
	updated := applySelectedModelToUpstream(upstream, "openai/gpt-5.4", nil)
	if updated.ManagedModelAlias != "openai/gpt-5.4" {
		t.Fatalf("expected alias override, got %#v", updated)
	}
	if updated.APIKey != "sk-local" {
		t.Fatalf("expected local credential preserved, got %#v", updated)
	}
}

func TestApplySelectedModelToUpstreamUsesAccountModelWhenLocalMissing(t *testing.T) {
	model := &aiModelResponse{
		ProviderName:    "openai",
		ModelIdentifier: "gpt-5.6-sol",
		CredentialType:  "oauth_openai_codex",
		HasAPIKey:       true,
		APIEndpoint:     "https://chatgpt.com/backend-api/codex",
		MetaData: map[string]interface{}{
			"gateway": map[string]interface{}{"model_alias": "openai/gpt-5.6-sol"},
		},
	}
	updated := applySelectedModelToUpstream(nil, "openai/gpt-5.6-sol", model)
	if updated == nil {
		t.Fatal("expected upstream from account model")
	}
	if !updated.AllowServerCredentialReuse {
		t.Fatalf("expected server credential reuse, got %#v", updated)
	}
	if updated.ManagedModelAlias != "openai/gpt-5.6-sol" {
		t.Fatalf("unexpected alias: %#v", updated)
	}
	if !upstreamEligibleForServerCredentialReuse(AgentConfig{Name: "Hermes"}, updated) {
		t.Fatalf("expected hermes explicit pick to be reuse-eligible")
	}
}

func TestInstallRuntimeModelFlagPassthrough(t *testing.T) {
	flag := agentsInstallRuntimeCmd.Flags().Lookup("model")
	if flag == nil {
		t.Fatal("expected --model flag on install-runtime")
	}
	flag = agentsEnrollCmd.Flags().Lookup("model")
	if flag == nil {
		t.Fatal("expected --model flag on onboard")
	}
}
