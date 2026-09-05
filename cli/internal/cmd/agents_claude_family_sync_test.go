package cmd

// Tests for Claude Code sibling-family AI model sync.
//
// Sibling family rows must share the primary model's SecretReference. A reuse
// that only refreshes meta_data leaves an older secret in place, which splits
// the single-use Anthropic refresh-token lineage.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
)

func claudeFamilySiblingForTest(id, identifier, alias, secretID string) aiModelResponse {
	return aiModelResponse{
		ID:                  id,
		Name:                "Claude Code " + alias,
		ProviderName:        "anthropic",
		ModelIdentifier:     identifier,
		CredentialsSecretID: secretID,
		HasAPIKey:           secretID != "",
		MetaData: map[string]interface{}{
			"gateway": map[string]interface{}{
				"enabled":     true,
				"model_alias": alias,
			},
		},
	}
}

func claudeFamilyPrimaryForTest(secretID string) *aiModelResponse {
	model := claudeFamilySiblingForTest(
		"primary-fable",
		"claude-fable-5-1",
		"anthropic/claude-fable-5-1",
		secretID,
	)
	return &model
}

func claudeFamilySiblingUpstreamForTest() *managedGatewayUpstream {
	return &managedGatewayUpstream{
		SourceAgent:       "claude_code",
		ProviderName:      "anthropic",
		ModelIdentifier:   "claude-sonnet-4-5",
		ManagedModelAlias: "anthropic/claude-sonnet-4-5",
	}
}

func newClaudeFamilyAIModelServer(
	t *testing.T,
	models []aiModelResponse,
	puts *[]map[string]interface{},
) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPut && strings.HasPrefix(r.URL.Path, "/api/v1/ai-models/"):
			update := map[string]interface{}{}
			if err := json.NewDecoder(r.Body).Decode(&update); err != nil {
				t.Fatalf("failed to decode ai-model update: %v", err)
			}
			*puts = append(*puts, update)
			updated := models[0]
			if secretID, ok := update["credentials_secret_id"].(string); ok {
				updated.CredentialsSecretID = secretID
			}
			_ = json.NewEncoder(w).Encode(updated)
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/ai-models":
			t.Errorf("unexpected AI model create: %s", r.URL.Path)
			w.WriteHeader(http.StatusCreated)
		default:
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
}

func TestEnsureClaudeFamilyAIModelRepointsSiblingOnDifferentSecret(t *testing.T) {
	primarySecret := "11111111-1111-1111-1111-111111111111"
	siblingSecret := "22222222-2222-2222-2222-222222222222"
	sibling := claudeFamilySiblingForTest(
		"sibling-sonnet",
		"claude-sonnet-4-5",
		"anthropic/claude-sonnet-4-5",
		siblingSecret,
	)
	puts := []map[string]interface{}{}
	server := newClaudeFamilyAIModelServer(t, []aiModelResponse{sibling}, &puts)
	defer server.Close()

	got, err := ensureClaudeFamilyAIModel(
		api.NewClientWithToken(server.URL, "tok"),
		[]aiModelResponse{sibling},
		&managedAgentSummary{ID: "agent-1"},
		AgentConfig{Name: "Claude Code"},
		claudeFamilySiblingUpstreamForTest(),
		claudeFamilyPrimaryForTest(primarySecret),
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got == nil {
		t.Fatalf("expected reused sibling model, got nil")
	}
	if len(puts) != 1 {
		t.Fatalf("expected one PUT to realign the sibling secret, got %d: %#v", len(puts), puts)
	}
	secretID, ok := puts[0]["credentials_secret_id"].(string)
	if !ok || secretID != primarySecret {
		t.Fatalf("expected PUT credentials_secret_id %q, got %#v", primarySecret, puts[0])
	}
	if _, ok := puts[0]["meta_data"]; !ok {
		t.Fatalf("expected PUT to include meta_data alongside the secret, got %#v", puts[0])
	}
}

func TestEnsureClaudeFamilyAIModelSkipsSecretWhenAlreadyShared(t *testing.T) {
	sharedSecret := "11111111-1111-1111-1111-111111111111"
	sibling := claudeFamilySiblingForTest(
		"sibling-sonnet",
		"claude-sonnet-4-5",
		"anthropic/claude-sonnet-4-5",
		sharedSecret,
	)
	puts := []map[string]interface{}{}
	server := newClaudeFamilyAIModelServer(t, []aiModelResponse{sibling}, &puts)
	defer server.Close()

	got, err := ensureClaudeFamilyAIModel(
		api.NewClientWithToken(server.URL, "tok"),
		[]aiModelResponse{sibling},
		&managedAgentSummary{ID: "agent-1"},
		AgentConfig{Name: "Claude Code"},
		claudeFamilySiblingUpstreamForTest(),
		claudeFamilyPrimaryForTest(sharedSecret),
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got == nil {
		t.Fatalf("expected reused sibling model, got nil")
	}
	for _, update := range puts {
		if _, ok := update["credentials_secret_id"]; ok {
			t.Fatalf(
				"sibling already on the primary secret must get meta-only or no PUT; got %#v",
				update,
			)
		}
	}
}
