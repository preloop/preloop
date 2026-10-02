package cmd

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
)

func TestReconnectGroupsOnlyIncludeEnrolledSubscriptionModels(t *testing.T) {
	models := []aiModelResponse{
		{ID: "one", CredentialType: anthropicClaudeCodeOAuthCredentialType, CredentialsSecretID: "shared", MetaData: map[string]interface{}{"managed_agent_id": "agent"}},
		{ID: "two", CredentialType: anthropicClaudeCodeOAuthCredentialType, CredentialsSecretID: "shared", MetaData: map[string]interface{}{"managed_agent_id": "agent"}},
		{ID: "other-host", CredentialType: anthropicClaudeCodeOAuthCredentialType, CredentialsSecretID: "other", MetaData: map[string]interface{}{"managed_agent_id": "other-agent"}},
		{ID: "api-key", CredentialType: "api_key", CredentialsSecretID: "key", MetaData: map[string]interface{}{"managed_agent_id": "agent"}},
		{ID: "untagged", CredentialType: anthropicClaudeCodeOAuthCredentialType, CredentialsSecretID: "untagged"},
	}
	groups := reconnectCredentialGroupsForEnrollment(models, "agent", anthropicClaudeCodeOAuthCredentialType)
	if len(groups) != 1 || len(groups[0]) != 2 {
		t.Fatalf("unexpected credential groups: %#v", groups)
	}
	if got := reconnectCredentialGroupsForEnrollment(models, "", anthropicClaudeCodeOAuthCredentialType); len(got) != 0 {
		t.Fatal("missing enrollment must never match account models")
	}
}

func TestReconnectRepairsSharedAndSplitSecretsWithoutDuplicatingTokens(t *testing.T) {
	for _, credentialType := range []string{anthropicClaudeCodeOAuthCredentialType, openaiCodexOAuthCredentialType} {
		t.Run(credentialType, func(t *testing.T) {
			writes := []map[string]interface{}{}
			paths := []string{}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodPut || !strings.HasPrefix(r.URL.Path, "/api/v1/ai-models/") {
					t.Errorf("reconnect must only update existing models: %s %s", r.Method, r.URL.Path)
				}
				var body map[string]interface{}
				_ = json.NewDecoder(r.Body).Decode(&body)
				writes = append(writes, body)
				paths = append(paths, r.URL.Path)
				_ = json.NewEncoder(w).Encode(aiModelResponse{ID: "one", CredentialsSecretID: "shared"})
			}))
			defer server.Close()
			groups := [][]aiModelResponse{
				{{ID: "one", CredentialsSecretID: "shared"}, {ID: "two", CredentialsSecretID: "shared"}},
				{{ID: "three", CredentialsSecretID: "split"}, {ID: "four", CredentialsSecretID: "split"}},
			}
			payload := map[string]interface{}{"access": "synthetic-access", "refresh": "synthetic-refresh", "expires": time.Now().Add(time.Hour).UnixMilli()}
			count, err := reconnectCredentialGroups(api.NewClientWithToken(server.URL, "synthetic"), groups, credentialType, payload)
			if err != nil || count != 1 || len(writes) != 3 {
				t.Fatalf("count=%d writes=%#v error=%v", count, writes, err)
			}
			if writes[0]["credential_type"] != credentialType || writes[0]["credential_payload"] == nil {
				t.Fatal("first write must replace the existing owner credential")
			}
			for _, write := range writes[1:] {
				if len(write) != 1 || write["credentials_secret_id"] != "shared" {
					t.Fatalf("split rows must attach to the owner without copying tokens: %#v", write)
				}
			}
			if paths[1] != "/api/v1/ai-models/three" || paths[2] != "/api/v1/ai-models/four" {
				t.Fatalf("unexpected repair targets: %#v", paths)
			}
		})
	}
}

func TestReconnectRefusesStaleOrIncompleteLoginBeforeWriting(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("an invalid local login must not reach the server")
	}))
	defer server.Close()
	for _, payload := range []map[string]interface{}{
		{},
		{"access": "synthetic", "refresh": "synthetic"},
		{"access": "synthetic", "refresh": "synthetic", "expires": time.Now().Add(-time.Hour).UnixMilli()},
		{"access": "synthetic", "expires": time.Now().Add(time.Hour).UnixMilli()},
	} {
		_, err := reconnectCredentialGroups(api.NewClientWithToken(server.URL, "synthetic"), [][]aiModelResponse{{{ID: "one"}}}, anthropicClaudeCodeOAuthCredentialType, payload)
		if err == nil {
			t.Fatalf("expected stale or incomplete login to be refused: %#v", payload)
		}
	}
}

func TestReconnectReportsFailureWithoutContinuingToOtherModels(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(http.StatusForbidden)
	}))
	defer server.Close()
	payload := map[string]interface{}{"access": "synthetic", "refresh": "synthetic", "expires": time.Now().Add(time.Hour).UnixMilli()}
	_, err := reconnectCredentialGroups(api.NewClientWithToken(server.URL, "synthetic"), [][]aiModelResponse{{{ID: "one"}}, {{ID: "two"}}}, anthropicClaudeCodeOAuthCredentialType, payload)
	if err == nil || calls != 1 {
		t.Fatalf("reconnect should stop at the failed update: calls=%d error=%v", calls, err)
	}
}
