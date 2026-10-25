package cmd

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
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
	var out bytes.Buffer
	choices := listGatewayModelChoices(client, &managedGatewayUpstream{
		ManagedModelAlias: "openai/gpt-5.6-sol",
		CredentialType:    "oauth_openai_codex",
		APIKey:            "sk-test",
	}, &out)
	if len(choices) != 2 {
		t.Fatalf("expected 2 choices, got %#v", choices)
	}
	if strings.Contains(out.String(), "could not list account AI models") {
		t.Fatalf("unexpected API failure notice on success: %q", out.String())
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
		&managedGatewayUpstream{ProviderName: "openai", ManagedModelAlias: "openai/gpt-5.6-sol"},
		managedEnrollmentOptions{
			PreferredModel: "openai/gpt-6.1-sol",
			AutoApprove:    true,
			Output:         &out,
		},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if alias != "openai/gpt-6.1-sol" {
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

func TestListGatewayModelChoicesWarnsOnAPIFailure(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "token")
	var out bytes.Buffer
	choices := listGatewayModelChoices(client, &managedGatewayUpstream{
		ManagedModelAlias: "openai/gpt-5.6-sol",
		APIKey:            "sk-test",
	}, &out)
	if len(choices) != 1 || !choices[0].Inferred {
		t.Fatalf("expected inferred-only fallback, got %#v", choices)
	}
	if !strings.Contains(out.String(), "could not list account AI models") {
		t.Fatalf("expected API failure notice, got %q", out.String())
	}
	if !strings.Contains(out.String(), "showing local inference only") {
		t.Fatalf("expected soft-fail wording, got %q", out.String())
	}
}

func TestOpenClawDryRunHonorsSelectedAccountModelOnFreshHost(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	model := aiModelResponse{ID: "selected", ProviderName: "openai", ModelIdentifier: "chosen-model", HasAPIKey: true, MetaData: map[string]interface{}{"gateway": map[string]interface{}{"model_alias": "team/chosen-model"}}}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/api/v1/ai-models" {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode([]aiModelResponse{model})
	}))
	defer server.Close()
	t.Setenv("PRELOOP_URL", server.URL)
	output := captureStdout(t, func() error {
		return executeManagedEnrollment(AgentConfig{Name: "OpenClaw", ConfigPath: filepath.Join(home, ".openclaw", "openclaw.json")}, managedEnrollmentOptions{Client: api.NewClientWithToken(server.URL, "token"), PreferredModel: "team/chosen-model", DryRun: true, AutoApprove: true, Output: io.Discard})
	})
	if !strings.Contains(output, "Managed model: preloop/team/chosen-model") {
		t.Fatalf("selected model lost: %s", output)
	}
}

func TestSelectedCanonicalModelChangesConcreteIdentifier(t *testing.T) {
	upstream := &managedGatewayUpstream{ProviderName: "openai", ModelIdentifier: "gpt-5.6-sol", ManagedModelAlias: "openai/gpt-5.6-sol", APIKey: "synthetic-key"}
	updated := applySelectedModelToUpstream(upstream, "openai/gpt-6.1-sol", nil)
	if updated.ModelIdentifier != "gpt-6.1-sol" {
		t.Fatalf("selection relabeled the old identifier: %#v", updated)
	}
}

func TestSelectedFriendlyAliasPreservesConcreteIdentifier(t *testing.T) {
	upstream := &managedGatewayUpstream{ProviderName: "openai", ModelIdentifier: "gpt-6.1-sol", ManagedModelAlias: "openai/gpt-6.1-sol", APIKey: "synthetic-key"}
	updated := applySelectedModelToUpstream(upstream, "team/assistant", nil)
	if updated.ModelIdentifier != "gpt-6.1-sol" || updated.ManagedModelAlias != "team/assistant" {
		t.Fatalf("custom alias lost: %#v", updated)
	}
}

func TestPickerDoesNotReplaceDiscoveredIdentityWithStaleAlias(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode([]aiModelResponse{{ID: "stale", ProviderName: "openai", ModelIdentifier: "gpt-5.6-sol", HasAPIKey: true, MetaData: map[string]interface{}{"gateway": map[string]interface{}{"model_alias": "openai/gpt-6.1-sol"}}}})
	}))
	defer server.Close()
	inferred := &managedGatewayUpstream{ProviderName: "openai", ModelIdentifier: "gpt-6.1-sol", ManagedModelAlias: "openai/gpt-6.1-sol", APIKey: "synthetic-key"}
	choices := listGatewayModelChoices(api.NewClientWithToken(server.URL, "token"), inferred, io.Discard)
	if len(choices) != 1 || choices[0].Model != nil {
		t.Fatalf("stale model replaced discovered identity: %#v", choices)
	}
}

func TestSelectedAccountModelWinsOverDifferentLocalProvider(t *testing.T) {
	local := &managedGatewayUpstream{ProviderName: "anthropic", ModelIdentifier: "local-model", APIKey: "local-key"}
	selected := &aiModelResponse{ProviderName: "openai", ModelIdentifier: "gpt-6.1-sol", HasAPIKey: true}
	updated := applySelectedModelToUpstream(local, "team/assistant", selected)
	if updated.ProviderName != "openai" || updated.ModelIdentifier != "gpt-6.1-sol" || updated.APIKey != "" {
		t.Fatalf("selected account identity lost: %#v", updated)
	}
}

// Exercise selection through reconciliation: neither the create nor update path
// may relabel the old concrete model, and a second run must reuse the new row.
func TestHermesModelSelectionReconcilesByUpstreamIdentity(t *testing.T) {
	for _, existingRequested := range []bool{false, true} {
		t.Run(strconv.FormatBool(existingRequested), func(t *testing.T) {
			old := aiModelResponse{ID: "old-model", Name: "Old model", ProviderName: "openai", ModelIdentifier: "gpt-5.6-sol", HasAPIKey: true, MetaData: map[string]interface{}{"gateway": map[string]interface{}{"model_alias": "openai/gpt-5.6-sol"}}}
			rows := []aiModelResponse{old}
			if existingRequested {
				rows = append(rows, aiModelResponse{ID: "requested-model", Name: "Requested model", ProviderName: "openai", ModelIdentifier: "gpt-6.1-sol", HasAPIKey: true})
			}
			creates := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch {
				case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
					_ = json.NewEncoder(w).Encode(rows)
				case r.Method == http.MethodPost && r.URL.Path == "/api/v1/ai-models":
					var created aiModelResponse
					if err := json.NewDecoder(r.Body).Decode(&created); err != nil {
						t.Fatal(err)
					}
					created.ID = "requested-model"
					created.HasAPIKey = true
					rows = append(rows, created)
					creates++
					_ = json.NewEncoder(w).Encode(created)
				case r.Method == http.MethodPut && r.URL.Path == "/api/v1/ai-models/requested-model":
					var update struct {
						MetaData map[string]interface{} `json:"meta_data"`
					}
					if err := json.NewDecoder(r.Body).Decode(&update); err != nil {
						t.Fatal(err)
					}
					if update.MetaData != nil {
						rows[1].MetaData = update.MetaData
					}
					_ = json.NewEncoder(w).Encode(rows[1])
				default:
					t.Errorf("unexpected mutation of stale model: %s %s", r.Method, r.URL.Path)
					http.Error(w, "unexpected request", http.StatusBadRequest)
				}
			}))
			defer server.Close()
			client := api.NewClientWithToken(server.URL, "synthetic-token")
			for i := 0; i < 2; i++ {
				discovered := &managedGatewayUpstream{SourceAgent: "hermes", ProviderName: "openai", ModelIdentifier: "gpt-5.6-sol", ManagedModelAlias: "openai/gpt-5.6-sol", APIKey: "synthetic-key"}
				selected := applySelectedModelToUpstream(discovered, "openai/gpt-6.1-sol", nil)
				resolved, _, err := syncManagedGatewayAIModel(client, &managedAgentSummary{ID: "synthetic-agent"}, AgentConfig{Name: "Hermes"}, selected, server.URL)
				if err != nil {
					t.Fatal(err)
				}
				if resolved.ID != "requested-model" || resolved.ModelIdentifier != "gpt-6.1-sol" || selected.ManagedModelAlias != "openai/gpt-6.1-sol" {
					t.Fatalf("incorrect reconciliation: %#v, %#v", resolved, selected)
				}
			}
			expectedCreates := 1
			if existingRequested {
				expectedCreates = 0
			}
			if creates != expectedCreates || len(rows) != 2 || gatewayAliasForAIModel(rows[0]) != "openai/gpt-5.6-sol" {
				t.Fatalf("reconciliation was not isolated/idempotent: %#v; creates=%d", rows, creates)
			}
		})
	}
}

func TestRawCanonicalSelectionRejectsIncompatibleLocalProvider(t *testing.T) {
	local := &managedGatewayUpstream{ProviderName: "anthropic", SourceProviderID: "anthropic", ModelIdentifier: "local-model", APIKey: "synthetic-key", APIEndpoint: "https://example.com/anthropic"}
	_, _, err := resolveManagedModelSelection(nil, AgentConfig{Name: "Hermes"}, local, managedEnrollmentOptions{PreferredModel: "openai/gpt-6.1-sol", AutoApprove: true})
	if err == nil {
		t.Fatal("foreign provider selection must require configured credentials")
	}
	if local.ModelIdentifier != "local-model" || local.SourceProviderID != "anthropic" {
		t.Fatal("selection mutated discovered configuration")
	}
}

func TestAccountSelectionWithoutCredentialDoesNotBorrowForeignLocalCredential(t *testing.T) {
	for _, endpoint := range []string{"", "https://example.com/other"} {
		local := &managedGatewayUpstream{ProviderName: "anthropic", SourceProviderID: "anthropic", ModelIdentifier: "local-model", APIKey: "synthetic-key", APIEndpoint: "https://example.com/local"}
		selected := &aiModelResponse{ProviderName: "openai", ModelIdentifier: "gpt-6.1-sol", APIEndpoint: endpoint}
		updated := applySelectedModelToUpstream(local, "team/assistant", selected)
		if updated.ProviderName != "openai" || updated.SourceProviderID != "openai" || updated.APIEndpoint != endpoint || updated.APIKey != "" || updated.CanRouteThroughGateway() {
			t.Fatalf("borrowed foreign upstream configuration: %#v", updated)
		}
	}
}

func TestManagedAliasRecoveryPreservesUserAliases(t *testing.T) {
	for _, tc := range []struct {
		name, alias, managedBy string
		conflict               bool
	}{
		{"corrupt import", "openai/gpt-6.1-sol", "preloop agents onboard", true},
		{"friendly import", "team/assistant", "preloop agents onboard", false},
		{"user alias", "openai/gpt-6.1-sol", "", false},
		{"collision suffix", "openai/gpt-5.6-sol-2", "preloop agents onboard", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			model := aiModelResponse{ProviderName: "openai", ModelIdentifier: "gpt-5.6-sol", HasAPIKey: true, MetaData: map[string]interface{}{"managed_by": tc.managedBy, "gateway": map[string]interface{}{"model_alias": tc.alias}}}
			if got := managedGatewayAliasConflictsWithIdentity(model); got != tc.conflict {
				t.Fatalf("conflict=%v, expected %v", got, tc.conflict)
			}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _ = json.NewEncoder(w).Encode([]aiModelResponse{model}) }))
			defer server.Close()
			choices := listGatewayModelChoices(api.NewClientWithToken(server.URL, "token"), nil, io.Discard)
			if tc.conflict && len(choices) != 0 {
				t.Fatal("corrupted import remains selectable")
			}
			if !tc.conflict && len(choices) != 1 {
				t.Fatal("intentional alias was lost")
			}
		})
	}
}

func TestGatewaySelectionUsesActualSourceProvider(t *testing.T) {
	for _, tc := range []struct {
		name, adapter, source, selected string
		allowed                         bool
	}{
		{"non azure rejects azure", "openai", "openai", "azure", false},
		{"non nous rejects nous", "openai", "openai", "nous", false},
		{"non nous rejects nous-api", "openai", "openai", "nous-api", false},
		{"azure selection", "azure", "azure", "azure", true},
		{"nous spelling", "nous", "nous-api", "nous", true},
		{"nous-api spelling", "nous", "nous", "nous-api", true},
		{"gemini cli spelling", "google", "google-gemini-cli", "gemini", true},
		{"aws spelling", "bedrock", "aws", "amazon-bedrock", true},
		{"vertex spelling", "vertex_ai", "vertex", "vertex_ai", true},
		{"vertex is separate from google", "google", "google", "vertex", false},
		{"normalized zai rejects openai", "openai", "zai", "openai", false},
		{"normalized zai accepts zai", "openai", "zai", "zai", true},
		{"source beats same-family adapter", "openai-codex", "zai", "openai", false},
		{"openai codex spelling", "openai", "openai-codex", "openai", true},
		{"codex openai spelling", "openai-codex", "openai", "openai-codex", true},
		{"google gemini spelling", "google", "gemini", "google", true},
		{"gemini google spelling", "gemini", "google", "gemini", true},
		{"bedrock spelling", "amazon-bedrock", "bedrock", "amazon-bedrock", true},
		{"amazon bedrock spelling", "bedrock", "amazon-bedrock", "bedrock", true},
		{"legacy adapter fallback", "openai", "", "openai-codex", true},
		{"case and whitespace", "openai", " OpenAI-Codex ", "OPENAI", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			local := &managedGatewayUpstream{ProviderName: tc.adapter, SourceProviderID: tc.source,
				ModelIdentifier: "old-model", ManagedModelAlias: tc.source + "/old-model",
				APIEndpoint: "https://api.z.ai/api/paas/v4", APIKey: "synthetic-key"}
			alias := tc.selected + "/gpt-6.1-sol"
			_, _, err := resolveManagedModelSelection(nil, AgentConfig{Name: "Hermes"}, local,
				managedEnrollmentOptions{PreferredModel: alias, AutoApprove: true})
			if (err == nil) != tc.allowed {
				t.Errorf("selection allowed=%v, want %v: %v", err == nil, tc.allowed, err)
			}
			updated := applySelectedModelToUpstream(local, alias, nil)
			if tc.allowed {
				if updated.ModelIdentifier != "gpt-6.1-sol" || updated.APIKey != "synthetic-key" {
					t.Fatalf("compatible selection lost identity or credential: %#v", updated)
				}
			} else if updated.ModelIdentifier != "old-model" || updated.ManagedModelAlias == alias {
				t.Fatalf("incompatible selection relabeled local upstream: %#v", updated)
			}
		})
	}
}

func TestManagedAliasSourceIdentityChoicesAndReuse(t *testing.T) {
	for _, tc := range []struct {
		name, provider, source, alias, managedBy string
		conflict                                 bool
	}{
		{"azure stale model", "azure", "", "azure/other-model", "preloop agents onboard", true},
		{"azure foreign alias", "openai", "", "azure/old-model", "preloop agents onboard", true},
		{"nous stale model", "nous", "nous-api", "nous/other-model", "preloop agents onboard", true},
		{"nous-api stale model", "nous", "nous", "nous-api/other-model", "preloop agents onboard", true},
		{"nous foreign alias", "openai", "", "nous/old-model", "preloop agents onboard", true},
		{"nous family valid", "nous", "nous-api", "nous/old-model", "preloop agents onboard", false},
		{"cross provider", "openai", "", "anthropic/other-model", "preloop agents onboard", true},
		{"cross provider same model", "openai", "", "anthropic/old-model", "preloop agents onboard", true},
		{"cross provider collision", "openai", "", "anthropic/old-model-2", "preloop agents onboard", true},
		{"openrouter mismatch", "openrouter", "", "openrouter/other-model", "preloop agents onboard", true},
		{"mistral mismatch", "mistral", "", "mistral/other-model", "preloop agents onboard", true},
		{"moonshot mismatch", "moonshot", "", "moonshot/other-model", "preloop agents onboard", true},
		{"zai mismatch", "zai", "", "zai/other-model", "preloop agents onboard", true},
		{"normalized zai mismatch", "openai", "zai", "zai/other-model", "preloop agents onboard", true},
		{"normalized zai foreign alias", "openai", "zai", "openai/old-model", "preloop agents onboard", true},
		{"normalized zai valid", "openai", "zai", "zai/old-model", "preloop agents onboard", false},
		{"normalized zai collision", "openai", "zai", "preloop/zai/old-model-2", "preloop agents onboard", false},
		{"same family", "openai", "openai-codex", "openai/old-model", "preloop agents onboard", false},
		{"friendly managed", "openai", "zai", "team/assistant", "preloop agents onboard", false},
		{"user owned", "openai", "", "anthropic/other-model", "", false},
		{"custom mismatch", "custom", "", "custom/other-model", "preloop agents onboard", true},
		{"compatible mismatch", "openai-compatible", "", "openai-compatible/other-model", "preloop agents onboard", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			model := aiModelResponse{ID: "synthetic-model", ProviderName: tc.provider, ModelIdentifier: "old-model", HasAPIKey: true,
				MetaData: map[string]interface{}{"managed_by": tc.managedBy,
					"gateway":         map[string]interface{}{"model_alias": tc.alias},
					"upstream_config": map[string]interface{}{"source_provider_id": tc.source}}}
			if got := managedGatewayAliasConflictsWithIdentity(model); got != tc.conflict {
				t.Errorf("conflict=%v, want %v", got, tc.conflict)
			}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet {
					t.Errorf("unexpected mutation: %s", r.Method)
				}
				_ = json.NewEncoder(w).Encode([]aiModelResponse{model})
			}))
			defer server.Close()
			choices := listGatewayModelChoices(api.NewClientWithToken(server.URL, "token"), nil, io.Discard)
			if (len(choices) == 0) != tc.conflict {
				t.Errorf("unexpected choices: %#v", choices)
			}
			upstream := &managedGatewayUpstream{ProviderName: tc.provider, SourceProviderID: tc.source,
				ModelIdentifier: "old-model", ManagedModelAlias: tc.alias}
			if reused := findReusableManagedGatewayAIModel([]aiModelResponse{model}, upstream); (reused == nil) != tc.conflict {
				t.Errorf("unexpected reuse: %#v", reused)
			}
		})
	}
}

func TestAccountSelectionUsesSourceProviderIdentity(t *testing.T) {
	for _, source := range []string{"openai", "zai"} {
		for _, hasKey := range []bool{false, true} {
			t.Run(source+"/"+strconv.FormatBool(hasKey), func(t *testing.T) {
				endpoint := "https://api.z.ai/api/paas/v4"
				local := &managedGatewayUpstream{ProviderName: "openai", SourceProviderID: "zai",
					ModelIdentifier: "old-model", ManagedModelAlias: "zai/old-model", APIEndpoint: endpoint, APIKey: "synthetic-local-key"}
				selected := &aiModelResponse{ProviderName: "openai", ModelIdentifier: "old-model", APIEndpoint: endpoint, HasAPIKey: hasKey,
					MetaData: map[string]interface{}{"upstream_config": map[string]interface{}{"source_provider_id": source}}}
				updated := applySelectedModelToUpstream(local, "team/assistant", selected)
				if updated.SourceProviderID != source {
					t.Errorf("lost actual source: %#v", updated)
				}
				if source != "zai" && (updated.APIKey != "" || updated.AllowServerCredentialReuse != hasKey) {
					t.Fatalf("foreign account borrowed local credentials: %#v", updated)
				}
				if source == "zai" && updated.APIKey != "synthetic-local-key" {
					t.Fatalf("compatible credential lost: %#v", updated)
				}
				if reused := findReusableManagedGatewayAIModel([]aiModelResponse{*selected}, local); (reused != nil) != (source == "zai") {
					t.Fatalf("cross-source reuse: %#v", reused)
				}
			})
		}
	}
}

func TestManagedAliasReconciliationSkipsCorruptRows(t *testing.T) {
	for _, source := range []string{"openai", "zai"} {
		t.Run(source, func(t *testing.T) {
			staleAlias := "anthropic/other-model"
			if source == "zai" {
				staleAlias = "zai/other-model"
			}
			rows := []aiModelResponse{{ID: "corrupt-model", ProviderName: "openai", ModelIdentifier: "old-model", HasAPIKey: true,
				MetaData: map[string]interface{}{"managed_by": "preloop agents onboard",
					"gateway":         map[string]interface{}{"model_alias": staleAlias},
					"upstream_config": map[string]interface{}{"source_provider_id": source}}}}
			creates := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch {
				case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
					_ = json.NewEncoder(w).Encode(rows)
				case r.Method == http.MethodPost && r.URL.Path == "/api/v1/ai-models":
					var created aiModelResponse
					if err := json.NewDecoder(r.Body).Decode(&created); err != nil {
						t.Error(err)
						http.Error(w, "invalid create", http.StatusBadRequest)
						return
					}
					created.ID, created.HasAPIKey = "valid-model", true
					rows = append(rows, created)
					creates++
					_ = json.NewEncoder(w).Encode(created)
				default:
					t.Errorf("unexpected mutation: %s %s", r.Method, r.URL.Path)
					http.Error(w, "unexpected request", http.StatusBadRequest)
				}
			}))
			defer server.Close()
			client := api.NewClientWithToken(server.URL, "synthetic-token")
			for i := 0; i < 2; i++ {
				inferred := &managedGatewayUpstream{SourceAgent: "hermes", ProviderName: "openai", SourceProviderID: source,
					ModelIdentifier: "old-model", ManagedModelAlias: source + "/old-model", APIKey: "synthetic-key"}
				alias, selected, err := resolveManagedModelSelection(client, AgentConfig{Name: "Hermes"}, inferred,
					managedEnrollmentOptions{PreferredModel: source + "/old-model", AutoApprove: true})
				if err != nil {
					t.Fatal(err)
				}
				upstream := applySelectedModelToUpstream(inferred, alias, selected)
				resolved, _, err := syncManagedGatewayAIModel(client, &managedAgentSummary{ID: "synthetic-agent"}, AgentConfig{Name: "Hermes"}, upstream, server.URL)
				if err != nil {
					t.Fatal(err)
				}
				if resolved == nil || resolved.ID != "valid-model" || managedGatewayAliasConflictsWithIdentity(*resolved) {
					t.Fatalf("corrupt model reused: %#v", resolved)
				}
			}
			if creates != 1 || len(rows) != 2 || gatewayAliasForAIModel(rows[0]) != staleAlias {
				t.Fatalf("reconciliation changed stale row or was not idempotent: creates=%d, rows=%#v", creates, rows)
			}
		})
	}
}

func TestPickerDoesNotMergeDifferentSourceProviders(t *testing.T) {
	model := aiModelResponse{ProviderName: "openai", ModelIdentifier: "old-model", HasAPIKey: true,
		MetaData: map[string]interface{}{"gateway": map[string]interface{}{"model_alias": "team/assistant"},
			"upstream_config": map[string]interface{}{"source_provider_id": "openai"}}}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode([]aiModelResponse{model})
	}))
	defer server.Close()
	inferred := &managedGatewayUpstream{ProviderName: "openai", SourceProviderID: "zai", ModelIdentifier: "old-model", ManagedModelAlias: "team/assistant"}
	choices := listGatewayModelChoices(api.NewClientWithToken(server.URL, "token"), inferred, io.Discard)
	if len(choices) != 1 || choices[0].Model != nil {
		t.Fatalf("account replaced local source identity: %#v", choices)
	}
}

func TestAccountSelectionWithDifferentEndpointDoesNotBorrowLocalCredential(t *testing.T) {
	local := &managedGatewayUpstream{ProviderName: "openai", SourceProviderID: "zai", ModelIdentifier: "old-model",
		APIEndpoint: "https://api.z.ai/api/paas/v4", APIKey: "synthetic-key", ManagedModelAlias: "zai/old-model"}
	selected := &aiModelResponse{ProviderName: "openai", ModelIdentifier: "old-model", APIEndpoint: "https://example.com/v1",
		MetaData: map[string]interface{}{"upstream_config": map[string]interface{}{"source_provider_id": "zai"}}}
	updated := applySelectedModelToUpstream(local, "team/assistant", selected)
	if updated.SourceProviderID != "zai" || updated.APIEndpoint != selected.APIEndpoint || updated.APIKey != "" || updated.CanRouteThroughGateway() {
		t.Fatalf("account borrowed credentials for a different endpoint: %#v", updated)
	}
}

func TestRawCanonicalSelectionRejectsAdapterAliasAlreadyInferred(t *testing.T) {
	local := &managedGatewayUpstream{ProviderName: "openai", SourceProviderID: "zai",
		ModelIdentifier: "gpt-6.1-sol", ManagedModelAlias: "openai/gpt-6.1-sol",
		APIEndpoint: "https://api.z.ai/api/paas/v4", APIKey: "synthetic-key"}
	_, _, err := resolveManagedModelSelection(nil, AgentConfig{Name: "Hermes"}, local,
		managedEnrollmentOptions{PreferredModel: "openai/gpt-6.1-sol", AutoApprove: true})
	if err == nil {
		t.Fatal("an inferred adapter alias must not bypass source-provider validation")
	}
}

func TestSelectedNormalizedAccountModelPreservesSourceProvider(t *testing.T) {
	selected := &aiModelResponse{ProviderName: "openai", ModelIdentifier: "selected-model", HasAPIKey: true,
		APIEndpoint: "https://api.z.ai/api/paas/v4",
		MetaData:    map[string]interface{}{"upstream_config": map[string]interface{}{"source_provider_id": "zai"}}}
	for _, local := range []*managedGatewayUpstream{nil, {
		ProviderName: "openai", SourceProviderID: "zai", ModelIdentifier: "old-model",
		APIEndpoint: selected.APIEndpoint, APIKey: "synthetic-local-key", ManagedModelAlias: "zai/old-model",
	}} {
		updated := applySelectedModelToUpstream(local, "zai/selected-model", selected)
		if updated.ProviderName != "openai" || updated.SourceProviderID != "zai" ||
			updated.ModelIdentifier != "selected-model" || updated.APIEndpoint != selected.APIEndpoint ||
			updated.APIKey != "" || !updated.AllowServerCredentialReuse {
			t.Fatalf("selected account model lost source identity: %#v", updated)
		}
	}
}
