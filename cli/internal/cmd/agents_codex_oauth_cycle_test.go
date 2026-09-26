package cmd

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
)

func apiTimePtr(value string) *api.Time {
	parsed, err := time.Parse(time.RFC3339, value)
	if err != nil {
		panic(err)
	}
	wrapped := api.Time{Time: parsed.UTC()}
	return &wrapped
}

func codexManagedOAuthSiblingForTest(id, identifier, alias, secretID string) aiModelResponse {
	return aiModelResponse{
		ID:                  id,
		Name:                "Codex CLI " + alias,
		ProviderName:        "openai-codex",
		ModelIdentifier:     identifier,
		APIEndpoint:         "https://api.openai.com/v1",
		CredentialType:      "oauth_openai_codex",
		CredentialsSecretID: secretID,
		HasAPIKey:           secretID != "",
		MetaData: map[string]interface{}{
			"source_agent":     "codex",
			"managed_by":       "preloop agents onboard",
			"managed_agent_id": "agent-codex-1",
			"gateway": map[string]interface{}{
				"enabled":     true,
				"model_alias": alias,
			},
		},
	}
}

func codexUpstreamForTest(identifier, alias string, payload map[string]interface{}) *managedGatewayUpstream {
	upstream := &managedGatewayUpstream{
		SourceAgent:       "codex",
		SourceProviderID:  "openai-codex",
		ProviderName:      "openai-codex",
		ModelIdentifier:   identifier,
		ManagedModelAlias: alias,
		APIEndpoint:       "https://api.openai.com/v1",
	}
	if payload != nil {
		upstream.CredentialType = "oauth_openai_codex"
		upstream.CredentialPayload = payload
	}
	return upstream
}

func newCodexFamilyLineageServer(
	t *testing.T,
	models []aiModelResponse,
	writes *[]recordedAIModelWrite,
) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
			_ = json.NewEncoder(w).Encode(models)
		case r.Method == http.MethodPut && strings.HasPrefix(r.URL.Path, "/api/v1/ai-models/"):
			body := map[string]interface{}{}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("failed to decode ai-model update: %v", err)
			}
			*writes = append(*writes, recordedAIModelWrite{
				Method: http.MethodPut,
				Path:   r.URL.Path,
				Body:   body,
			})
			updated := models[0]
			for _, m := range models {
				if strings.HasSuffix(r.URL.Path, "/"+m.ID) {
					updated = m
					break
				}
			}
			if secretID, ok := body["credentials_secret_id"].(string); ok {
				updated.CredentialsSecretID = secretID
				updated.HasAPIKey = true
			}
			_ = json.NewEncoder(w).Encode(updated)
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/ai-models":
			body := map[string]interface{}{}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("failed to decode ai-model create: %v", err)
			}
			*writes = append(*writes, recordedAIModelWrite{
				Method: http.MethodPost,
				Path:   r.URL.Path,
				Body:   body,
			})
			created := codexManagedOAuthSiblingForTest(
				"created-model",
				"gpt-4o",
				"openai/gpt-4o",
				"",
			)
			if secretID, ok := body["credentials_secret_id"].(string); ok {
				created.CredentialsSecretID = secretID
				created.HasAPIKey = true
			}
			_ = json.NewEncoder(w).Encode(created)
		default:
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
}

func TestSyncManagedGatewayAIModelCreateReusesCodexSiblingSecret(t *testing.T) {
	sibling := codexManagedOAuthSiblingForTest(
		"codex-o3-mini",
		"o3-mini",
		"openai/o3-mini",
		"secret-codex-live",
	)
	writes := []recordedAIModelWrite{}
	server := newCodexFamilyLineageServer(t, []aiModelResponse{sibling}, &writes)
	defer server.Close()

	freshExpiry := time.Now().UTC().Add(4 * time.Hour).UnixMilli()
	upstream := codexUpstreamForTest("gpt-4o", "openai/gpt-4o", map[string]interface{}{
		"access":  "sk-codex-oat-fresh",
		"refresh": "sk-codex-ort-fresh",
		"expires": freshExpiry,
	})

	model, _, err := syncManagedGatewayAIModel(
		api.NewClientWithToken(server.URL, "tok"),
		&managedAgentSummary{ID: "agent-codex-1"},
		AgentConfig{Name: "Codex CLI"},
		upstream,
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if model == nil {
		t.Fatalf("expected created codex row, got nil")
	}
	var createBody map[string]interface{}
	for _, write := range writes {
		if write.Method == http.MethodPut {
			t.Fatalf("must not update on create; got %#v", write)
		}
		if write.Method == http.MethodPost {
			createBody = write.Body
		}
	}
	if createBody == nil {
		t.Fatalf("expected a create for the new codex row; writes: %#v", writes)
	}
	if createBody["credentials_secret_id"] != "secret-codex-live" {
		t.Fatalf("expected create to reuse sibling secret, got %#v", createBody)
	}
	if _, ok := createBody["credential_payload"]; ok {
		t.Fatalf("create must not mint a second OAuth secret; got %#v", createBody)
	}
	if _, ok := createBody["credential_type"]; ok {
		t.Fatalf("create must not send credential_type with a reused secret; got %#v", createBody)
	}
}

func TestSyncManagedGatewayAIModelUpdateAttachesCodexSiblingSecret(t *testing.T) {
	target := codexManagedOAuthSiblingForTest(
		"target-codex-gpt4",
		"gpt-4o",
		"openai/gpt-4o",
		"separate-secret-id",
	)
	sibling := codexManagedOAuthSiblingForTest(
		"sibling-codex-o3",
		"o3-mini",
		"openai/o3-mini",
		"shared-codex-secret",
	)
	writes := []recordedAIModelWrite{}
	server := newCodexFamilyLineageServer(t, []aiModelResponse{target, sibling}, &writes)
	defer server.Close()

	freshExpiry := time.Now().UTC().Add(4 * time.Hour).UnixMilli()
	upstream := codexUpstreamForTest("gpt-4o", "openai/gpt-4o", map[string]interface{}{
		"access":  "sk-codex-oat-fresh",
		"refresh": "sk-codex-ort-fresh",
		"expires": freshExpiry,
	})

	model, _, err := syncManagedGatewayAIModel(
		api.NewClientWithToken(server.URL, "tok"),
		&managedAgentSummary{ID: "agent-codex-1"},
		AgentConfig{Name: "Codex CLI"},
		upstream,
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if model == nil {
		t.Fatalf("expected updated codex row, got nil")
	}
	repointed := false
	for _, write := range writes {
		if write.Method == http.MethodPut && strings.HasSuffix(write.Path, "/target-codex-gpt4") {
			if write.Body["credentials_secret_id"] == "shared-codex-secret" {
				repointed = true
			}
		}
	}
	if !repointed {
		t.Fatalf("expected PUT repointing target to shared-codex-secret; writes: %#v", writes)
	}
}

func TestSyncManagedGatewayAIModelDifferentManagedAgentDoesNotAttachCodex(t *testing.T) {
	otherMachine := codexManagedOAuthSiblingForTest(
		"codex-other",
		"o3-mini",
		"openai/o3-mini",
		"secret-other-machine",
	)
	otherMachine.MetaData["managed_agent_id"] = "agent-other-machine"
	otherMachine.CredentialsStatus = "error"

	writes := []recordedAIModelWrite{}
	server := newCodexFamilyLineageServer(t, []aiModelResponse{otherMachine}, &writes)
	defer server.Close()

	freshExpiry := time.Now().UTC().Add(4 * time.Hour).UnixMilli()
	upstream := codexUpstreamForTest("gpt-4o", "openai/gpt-4o", map[string]interface{}{
		"access":  "sk-codex-oat-fresh",
		"refresh": "sk-codex-ort-fresh",
		"expires": freshExpiry,
	})

	_, _, err := syncManagedGatewayAIModel(
		api.NewClientWithToken(server.URL, "tok"),
		&managedAgentSummary{ID: "agent-codex-1"},
		AgentConfig{Name: "Codex CLI"},
		upstream,
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for _, write := range writes {
		if strings.Contains(write.Path, "/codex-other") {
			t.Fatalf("must never repoint another machine's row: %#v", write)
		}
		if write.Body["credentials_secret_id"] == "secret-other-machine" {
			t.Fatalf("must never attach to another machine's managed_agent_id secret: %#v", write)
		}
	}
}

func TestFindManagedOAuthCredentialSiblingPrefersFresherOverFirstMatch(t *testing.T) {
	staleFirst := codexManagedOAuthSiblingForTest(
		"codex-stale-first",
		"o3-mini",
		"openai/o3-mini",
		"secret-stale",
	)
	staleFirst.UpdatedAt = apiTimePtr("2026-09-01T00:00:00Z")
	staleFirst.CredentialsLastVerifiedAt = apiTimePtr("2026-09-01T00:00:00Z")

	freshLater := codexManagedOAuthSiblingForTest(
		"codex-fresh-later",
		"gpt-4o",
		"openai/gpt-4o",
		"secret-live",
	)
	freshLater.UpdatedAt = apiTimePtr("2026-09-18T00:00:00Z")
	freshLater.CredentialsLastVerifiedAt = apiTimePtr("2026-09-18T12:00:00Z")

	got := findManagedOAuthCredentialSibling(
		[]aiModelResponse{staleFirst, freshLater},
		&managedAgentSummary{ID: "agent-codex-1"},
		"oauth_openai_codex",
		"",
	)
	if got == nil || got.ID != "codex-fresh-later" {
		t.Fatalf("stale first-listed sibling must lose to the fresher copy, got %#v", got)
	}
	if got.CredentialsSecretID != "secret-live" {
		t.Fatalf("expected live secret, got %#v", got)
	}
}

func TestSyncManagedGatewayAIModelCreateReusesFresherCodexSiblingSecret(t *testing.T) {
	staleFirst := codexManagedOAuthSiblingForTest(
		"codex-stale-first",
		"o3-mini",
		"openai/o3-mini",
		"secret-stale",
	)
	staleFirst.CredentialsLastVerifiedAt = apiTimePtr("2026-09-01T00:00:00Z")
	staleFirst.UpdatedAt = apiTimePtr("2026-09-01T00:00:00Z")

	freshLater := codexManagedOAuthSiblingForTest(
		"codex-fresh-later",
		"gpt-5",
		"openai/gpt-5",
		"secret-live",
	)
	freshLater.CredentialsLastVerifiedAt = apiTimePtr("2026-09-18T12:00:00Z")
	freshLater.UpdatedAt = apiTimePtr("2026-09-18T12:00:00Z")

	writes := []recordedAIModelWrite{}
	server := newCodexFamilyLineageServer(t, []aiModelResponse{staleFirst, freshLater}, &writes)
	defer server.Close()

	freshExpiry := time.Now().UTC().Add(4 * time.Hour).UnixMilli()
	upstream := codexUpstreamForTest("gpt-4o", "openai/gpt-4o", map[string]interface{}{
		"access":  "sk-codex-oat-fresh",
		"refresh": "sk-codex-ort-fresh",
		"expires": freshExpiry,
	})

	_, _, err := syncManagedGatewayAIModel(
		api.NewClientWithToken(server.URL, "tok"),
		&managedAgentSummary{ID: "agent-codex-1"},
		AgentConfig{Name: "Codex CLI"},
		upstream,
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var createBody map[string]interface{}
	for _, write := range writes {
		if write.Method == http.MethodPost {
			createBody = write.Body
		}
	}
	if createBody == nil {
		t.Fatalf("expected a create for the new codex row; writes: %#v", writes)
	}
	if createBody["credentials_secret_id"] != "secret-live" {
		t.Fatalf("create must reuse the fresher sibling secret, got %#v", createBody)
	}
}

func TestSyncManagedGatewayAIModelDoesNotRepointLiveTargetOntoStaleSibling(t *testing.T) {
	liveTarget := codexManagedOAuthSiblingForTest(
		"target-codex-gpt4",
		"gpt-4o",
		"openai/gpt-4o",
		"secret-live",
	)
	liveTarget.CredentialsLastVerifiedAt = apiTimePtr("2026-09-18T12:00:00Z")
	liveTarget.UpdatedAt = apiTimePtr("2026-09-18T12:00:00Z")

	staleSibling := codexManagedOAuthSiblingForTest(
		"sibling-codex-o3",
		"o3-mini",
		"openai/o3-mini",
		"secret-stale",
	)
	staleSibling.CredentialsLastVerifiedAt = apiTimePtr("2026-09-01T00:00:00Z")
	staleSibling.UpdatedAt = apiTimePtr("2026-09-01T00:00:00Z")

	writes := []recordedAIModelWrite{}
	// Stale sibling first, so first-match without a target comparison
	// would converge the live holder onto the consumed grant.
	server := newCodexFamilyLineageServer(t, []aiModelResponse{staleSibling, liveTarget}, &writes)
	defer server.Close()

	freshExpiry := time.Now().UTC().Add(4 * time.Hour).UnixMilli()
	upstream := codexUpstreamForTest("gpt-4o", "openai/gpt-4o", map[string]interface{}{
		"access":  "sk-codex-oat-fresh",
		"refresh": "sk-codex-ort-fresh",
		"expires": freshExpiry,
	})

	if _, _, err := syncManagedGatewayAIModel(
		api.NewClientWithToken(server.URL, "tok"),
		&managedAgentSummary{ID: "agent-codex-1"},
		AgentConfig{Name: "Codex CLI"},
		upstream,
		server.URL+"/openai/v1",
	); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for _, write := range writes {
		if write.Method != http.MethodPut || !strings.HasSuffix(write.Path, "/target-codex-gpt4") {
			continue
		}
		if write.Body["credentials_secret_id"] == "secret-stale" {
			t.Fatalf("live target must not be repointed onto a stale sibling: %#v", write)
		}
		if _, ok := write.Body["credential_payload"]; ok {
			t.Fatalf("live target must not be re-seeded over its own secret: %#v", write)
		}
	}
}

func TestSyncManagedGatewayAIModelCredentiallessTargetWithNewerUpdatedAtAttachesLiveSibling(t *testing.T) {
	// A credentialless target whose row was edited recently (e.g. metadata sync or rename)
	// has a recent UpdatedAt. That row-edit timestamp must not masquerade as a live secret
	// or block attaching to an older, live OAuth sibling.
	credentiallessTarget := codexManagedOAuthSiblingForTest(
		"target-codex-gpt4",
		"gpt-4o",
		"openai/gpt-4o",
		"",
	)
	credentiallessTarget.HasAPIKey = false
	credentiallessTarget.CredentialType = ""
	credentiallessTarget.CredentialsLastVerifiedAt = nil
	credentiallessTarget.UpdatedAt = apiTimePtr("2026-09-18T12:00:00Z")

	// Sibling holds a live OAuth secret verified earlier (e.g. 5 days ago over an idle weekend).
	liveSibling := codexManagedOAuthSiblingForTest(
		"sibling-codex-o3",
		"o3-mini",
		"openai/o3-mini",
		"secret-live",
	)
	liveSibling.CredentialsLastVerifiedAt = apiTimePtr("2026-09-13T00:00:00Z")
	liveSibling.UpdatedAt = apiTimePtr("2026-09-13T00:00:00Z")

	writes := []recordedAIModelWrite{}
	server := newCodexFamilyLineageServer(t, []aiModelResponse{liveSibling, credentiallessTarget}, &writes)
	defer server.Close()

	freshExpiry := time.Now().UTC().Add(4 * time.Hour).UnixMilli()
	upstream := codexUpstreamForTest("gpt-4o", "openai/gpt-4o", map[string]interface{}{
		"access":  "sk-codex-oat-fresh",
		"refresh": "sk-codex-ort-fresh",
		"expires": freshExpiry,
	})

	if _, _, err := syncManagedGatewayAIModel(
		api.NewClientWithToken(server.URL, "tok"),
		&managedAgentSummary{ID: "agent-codex-1"},
		AgentConfig{Name: "Codex CLI"},
		upstream,
		server.URL+"/openai/v1",
	); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	attached := false
	for _, write := range writes {
		if write.Method != http.MethodPut || !strings.HasSuffix(write.Path, "/target-codex-gpt4") {
			continue
		}
		if write.Body["credentials_secret_id"] == "secret-live" {
			attached = true
		}
		if _, ok := write.Body["credential_payload"]; ok {
			t.Fatalf("credentialless target must attach sibling secret rather than re-seeding: %#v", write)
		}
	}
	if !attached {
		t.Fatalf("expected credentialless target to attach live sibling secret even with newer UpdatedAt; writes: %#v", writes)
	}
}

func TestCodexServerHasReusableGatewayCredential(t *testing.T) {
	sibling := codexManagedOAuthSiblingForTest(
		"codex-o3-mini",
		"o3-mini",
		"openai/o3-mini",
		"secret-codex-live",
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models" {
			_ = json.NewEncoder(w).Encode([]aiModelResponse{sibling})
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	agent := AgentConfig{Name: "Codex CLI"}
	upstream := codexUpstreamForTest("o3-mini", "openai/o3-mini", nil)

	if !serverHasReusableGatewayCredential(client, agent, upstream) {
		t.Fatalf("expected serverHasReusableGatewayCredential to be true for Codex CLI with stored model")
	}
}

func TestCodexServerHasReusableGatewayCredentialFalseWhenSecretInError(t *testing.T) {
	sibling := codexManagedOAuthSiblingForTest(
		"codex-o3-mini",
		"o3-mini",
		"openai/o3-mini",
		"secret-codex-live",
	)
	sibling.CredentialsStatus = "error"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models" {
			_ = json.NewEncoder(w).Encode([]aiModelResponse{sibling})
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	upstream := codexUpstreamForTest("o3-mini", "openai/o3-mini", map[string]interface{}{
		"access":  "sk-codex-oat-local",
		"refresh": "sk-codex-ort-local",
		"expires": time.Now().UTC().Add(4 * time.Hour).UnixMilli(),
	})
	if serverHasReusableGatewayCredential(client, AgentConfig{Name: "Codex CLI"}, upstream) {
		t.Fatal("a Codex secret in error is not a reusable live lineage")
	}
}

func TestCodexServerHasReusableGatewayCredentialRequiresSameOAuthType(t *testing.T) {
	sibling := codexManagedOAuthSiblingForTest(
		"codex-o3-mini",
		"o3-mini",
		"openai/o3-mini",
		"secret-codex-live",
	)
	sibling.CredentialsStatus = "active"
	sibling.CredentialType = "oauth_anthropic_claude_code"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models" {
			_ = json.NewEncoder(w).Encode([]aiModelResponse{sibling})
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	upstream := codexUpstreamForTest("o3-mini", "openai/o3-mini", map[string]interface{}{
		"access":  "sk-codex-oat-local",
		"refresh": "sk-codex-ort-local",
		"expires": time.Now().UTC().Add(4 * time.Hour).UnixMilli(),
	})
	if serverHasReusableGatewayCredential(client, AgentConfig{Name: "Codex CLI"}, upstream) {
		t.Fatal("expected a different OAuth type to be rejected")
	}
}

func TestCodexAddModelAttachesActiveSecret(t *testing.T) {
	sibling := codexManagedOAuthSiblingForTest(
		"codex-o3-mini",
		"o3-mini",
		"openai/o3-mini",
		"secret-codex-live",
	)
	sibling.CredentialsStatus = "active"
	writes := []recordedAIModelWrite{}
	server := newCodexFamilyLineageServer(t, []aiModelResponse{sibling}, &writes)
	defer server.Close()

	upstream := codexUpstreamForTest("gpt-4o", "openai/gpt-4o", codexFreshOAuthPayload())
	_, _, err := syncManagedGatewayAIModel(
		api.NewClientWithToken(server.URL, "tok"),
		&managedAgentSummary{ID: "agent-codex-1"},
		AgentConfig{Name: "Codex CLI"},
		upstream,
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var createBody map[string]interface{}
	for _, write := range writes {
		if write.Method == http.MethodPut {
			t.Fatalf("active secret must not be overwritten while adding a model; got %#v", write)
		}
		if _, ok := write.Body["credential_payload"]; ok {
			t.Fatalf("active secret must not be re-uploaded; got %#v", write)
		}
		if write.Method == http.MethodPost {
			createBody = write.Body
		}
	}
	if createBody["credentials_secret_id"] != "secret-codex-live" {
		t.Fatalf("expected the new row to attach the active secret, got %#v", createBody)
	}
}

func TestCodexAddModelUploadsInPlaceWhenSecretInError(t *testing.T) {
	sibling := codexManagedOAuthSiblingForTest(
		"codex-o3-mini",
		"o3-mini",
		"openai/o3-mini",
		"secret-codex-live",
	)
	sibling.CredentialsStatus = "error"
	writes := []recordedAIModelWrite{}
	server := newCodexFamilyLineageServer(t, []aiModelResponse{sibling}, &writes)
	defer server.Close()

	upstream := codexUpstreamForTest("gpt-4o", "openai/gpt-4o", codexFreshOAuthPayload())
	_, _, err := syncManagedGatewayAIModel(
		api.NewClientWithToken(server.URL, "tok"),
		&managedAgentSummary{ID: "agent-codex-1"},
		AgentConfig{Name: "Codex CLI"},
		upstream,
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	repaired := false
	var createBody map[string]interface{}
	for _, write := range writes {
		if write.Method == http.MethodPut && strings.HasSuffix(write.Path, "/codex-o3-mini") {
			if _, ok := write.Body["credential_payload"]; !ok {
				t.Fatalf("error secret must be refreshed in place; got %#v", write)
			}
			if _, ok := write.Body["credentials_secret_id"]; ok {
				t.Fatalf("in-place refresh must not repoint the secret; got %#v", write)
			}
			repaired = true
		}
		if write.Method == http.MethodPost {
			createBody = write.Body
			if _, ok := write.Body["credential_payload"]; ok {
				t.Fatalf("adding a model must not mint a second secret; got %#v", write)
			}
		}
	}
	if !repaired {
		t.Fatalf("expected an in-place upload onto the error secret; writes: %#v", writes)
	}
	if createBody["credentials_secret_id"] != "secret-codex-live" {
		t.Fatalf("new row must attach the repaired secret, got %#v", createBody)
	}
}

func TestCodexReonboardKeepsLiveSecret(t *testing.T) {
	target := codexManagedOAuthSiblingForTest(
		"codex-gpt4",
		"gpt-4o",
		"openai/gpt-4o",
		"secret-codex-live",
	)
	target.CredentialsStatus = "active"
	writes := []recordedAIModelWrite{}
	server := newCodexFamilyLineageServer(t, []aiModelResponse{target}, &writes)
	defer server.Close()

	_, _, err := syncManagedGatewayAIModel(
		api.NewClientWithToken(server.URL, "tok"),
		&managedAgentSummary{ID: "agent-codex-1"},
		AgentConfig{Name: "Codex CLI"},
		codexUpstreamForTest("gpt-4o", "openai/gpt-4o", codexFreshOAuthPayload()),
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for _, write := range writes {
		if write.Method == http.MethodPost {
			t.Fatalf("re-onboard must reuse the existing row; got %#v", write)
		}
		if _, ok := write.Body["credential_payload"]; ok {
			t.Fatalf("live Codex secret must not be re-uploaded; got %#v", write)
		}
		if secretID, ok := write.Body["credentials_secret_id"].(string); ok && secretID != "secret-codex-live" {
			t.Fatalf("re-onboard must keep the existing secret, got %#v", write)
		}
	}
}

func TestCodexReonboardUploadsInPlaceWhenSecretInError(t *testing.T) {
	target := codexManagedOAuthSiblingForTest(
		"codex-gpt4",
		"gpt-4o",
		"openai/gpt-4o",
		"secret-codex-live",
	)
	target.CredentialsStatus = "error"
	writes := []recordedAIModelWrite{}
	server := newCodexFamilyLineageServer(t, []aiModelResponse{target}, &writes)
	defer server.Close()

	_, _, err := syncManagedGatewayAIModel(
		api.NewClientWithToken(server.URL, "tok"),
		&managedAgentSummary{ID: "agent-codex-1"},
		AgentConfig{Name: "Codex CLI"},
		codexUpstreamForTest("gpt-4o", "openai/gpt-4o", codexFreshOAuthPayload()),
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	repaired := false
	for _, write := range writes {
		if write.Method == http.MethodPost {
			t.Fatalf("in-place refresh must not create a row; got %#v", write)
		}
		if write.Method == http.MethodPut && strings.HasSuffix(write.Path, "/codex-gpt4") {
			if _, ok := write.Body["credential_payload"]; ok {
				repaired = true
			}
			if _, ok := write.Body["credentials_secret_id"]; ok {
				t.Fatalf("in-place refresh must not repoint the secret; got %#v", write)
			}
		}
	}
	if !repaired {
		t.Fatalf("expected the error secret to be updated in place; writes: %#v", writes)
	}
}

func codexFreshOAuthPayload() map[string]interface{} {
	return map[string]interface{}{
		"access":  "sk-codex-oat-fresh",
		"refresh": "sk-codex-ort-fresh",
		"expires": time.Now().UTC().Add(4 * time.Hour).UnixMilli(),
	}
}

type codexLineageLedger struct {
	mu           sync.Mutex
	models       []aiModelResponse
	payloadPosts int
	payloadPuts  int
}

func (l *codexLineageLedger) snapshot() (models []aiModelResponse, payloadPosts, payloadPuts int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	models = append([]aiModelResponse(nil), l.models...)
	return models, l.payloadPosts, l.payloadPuts
}

func newStatefulCodexOAuthServer(t *testing.T) (*httptest.Server, *codexLineageLedger) {
	t.Helper()
	ledger := &codexLineageLedger{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
			ledger.mu.Lock()
			models := append([]aiModelResponse(nil), ledger.models...)
			ledger.mu.Unlock()
			_ = json.NewEncoder(w).Encode(models)
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/ai-models":
			body := map[string]interface{}{}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("failed to decode ai-model create: %v", err)
			}
			ledger.mu.Lock()
			defer ledger.mu.Unlock()
			secretID, _ := body["credentials_secret_id"].(string)
			if _, ok := body["credential_payload"]; ok {
				ledger.payloadPosts++
				if secretID == "" {
					secretID = "secret-lineage-1"
				}
			}
			meta, _ := body["meta_data"].(map[string]interface{})
			created := aiModelResponse{
				ID:                  "model-" + strconv.Itoa(len(ledger.models)+1),
				Name:                stringField(body, "name"),
				ProviderName:        stringField(body, "provider_name"),
				ModelIdentifier:     stringField(body, "model_identifier"),
				APIEndpoint:         stringField(body, "api_endpoint"),
				CredentialType:      stringField(body, "credential_type"),
				CredentialsSecretID: secretID,
				CredentialsStatus:   "active",
				HasAPIKey:           secretID != "",
				MetaData:            meta,
			}
			if created.CredentialType == "" && secretID != "" {
				created.CredentialType = "oauth_openai_codex"
			}
			ledger.models = append(ledger.models, created)
			_ = json.NewEncoder(w).Encode(created)
		case r.Method == http.MethodPut && strings.HasPrefix(r.URL.Path, "/api/v1/ai-models/"):
			body := map[string]interface{}{}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("failed to decode ai-model update: %v", err)
			}
			ledger.mu.Lock()
			defer ledger.mu.Unlock()
			if _, ok := body["credential_payload"]; ok {
				ledger.payloadPuts++
			}
			var updated aiModelResponse
			for i := range ledger.models {
				if strings.HasSuffix(r.URL.Path, "/"+ledger.models[i].ID) {
					updated = ledger.models[i]
					if meta, ok := body["meta_data"].(map[string]interface{}); ok {
						updated.MetaData = meta
					}
					if secretID, ok := body["credentials_secret_id"].(string); ok {
						updated.CredentialsSecretID = secretID
						updated.HasAPIKey = true
					}
					if credType, ok := body["credential_type"].(string); ok && credType != "" {
						updated.CredentialType = credType
					}
					updated.CredentialsStatus = "active"
					ledger.models[i] = updated
					break
				}
			}
			_ = json.NewEncoder(w).Encode(updated)
		default:
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	return server, ledger
}

func TestCodexFreshOnboardAndReonboardKeepOneSecret(t *testing.T) {
	server, ledger := newStatefulCodexOAuthServer(t)
	defer server.Close()
	client := api.NewClientWithToken(server.URL, "tok")
	agent := AgentConfig{Name: "Codex CLI"}
	managed := &managedAgentSummary{ID: "agent-codex-1"}
	rows := [][2]string{
		{"gpt-4o", "openai/gpt-4o"},
		{"o3-mini", "openai/o3-mini"},
		{"gpt-5", "openai/gpt-5"},
	}
	for _, row := range rows {
		model, _, err := syncManagedGatewayAIModel(
			client,
			managed,
			agent,
			codexUpstreamForTest(row[0], row[1], codexFreshOAuthPayload()),
			server.URL+"/openai/v1",
		)
		if err != nil {
			t.Fatalf("onboard %s: %v", row[0], err)
		}
		if model == nil {
			t.Fatalf("onboard %s returned nil", row[0])
		}
	}
	models, payloadPosts, payloadPuts := ledger.snapshot()
	if payloadPosts != 1 {
		t.Fatalf("fresh onboard of %d rows created %d credential_payload posts, want 1", len(rows), payloadPosts)
	}
	if payloadPuts != 0 {
		t.Fatalf("fresh onboard uploaded in place %d times, want 0", payloadPuts)
	}
	if len(models) != len(rows) {
		t.Fatalf("expected %d rows, got %d", len(rows), len(models))
	}
	for _, model := range models {
		if model.CredentialsSecretID != "secret-lineage-1" {
			t.Fatalf("row %s secret = %q, want secret-lineage-1", model.ID, model.CredentialsSecretID)
		}
		agentID, _ := model.MetaData["managed_agent_id"].(string)
		if agentID != "agent-codex-1" {
			t.Fatalf("row %s managed_agent_id = %q", model.ID, agentID)
		}
	}

	for _, row := range rows {
		if _, _, err := syncManagedGatewayAIModel(
			client,
			managed,
			agent,
			codexUpstreamForTest(row[0], row[1], codexFreshOAuthPayload()),
			server.URL+"/openai/v1",
		); err != nil {
			t.Fatalf("re-onboard %s: %v", row[0], err)
		}
	}
	models, payloadPosts, payloadPuts = ledger.snapshot()
	if payloadPosts != 1 || payloadPuts != 0 {
		t.Fatalf("re-onboard changed secret uploads: posts=%d puts=%d", payloadPosts, payloadPuts)
	}
	if len(models) != len(rows) {
		t.Fatalf("re-onboard changed row count: got %d want %d", len(models), len(rows))
	}
	for _, model := range models {
		if model.CredentialsSecretID != "secret-lineage-1" {
			t.Fatalf("re-onboard row %s secret = %q", model.ID, model.CredentialsSecretID)
		}
		agentID, _ := model.MetaData["managed_agent_id"].(string)
		if agentID != "agent-codex-1" {
			t.Fatalf("re-onboard row %s managed_agent_id = %q", model.ID, agentID)
		}
	}
}
