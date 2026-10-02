package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
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

func TestRunAgentsReconnectPreservesEnrollmentAndConfig(t *testing.T) {
	for _, tc := range []struct {
		name, agent, wantError                                                  string
		fromLocal, notEnrolled, notAuthenticated, loginFails, expired, noModels bool
	}{
		{name: "claude interactive", agent: "Claude Code"},
		{name: "claude local", agent: "Claude Code", fromLocal: true},
		{name: "codex interactive", agent: "Codex CLI"},
		{name: "codex local", agent: "Codex CLI", fromLocal: true},
		{name: "not enrolled", agent: "Claude Code", notEnrolled: true, wantError: "not enrolled"},
		{name: "not authenticated", agent: "Claude Code", notAuthenticated: true, wantError: "preloop login"},
		{name: "sign-in fails", agent: "Claude Code", loginFails: true, wantError: "sign-in failed"},
		{name: "stale local login", agent: "Claude Code", fromLocal: true, expired: true, wantError: "expired"},
		{name: "wrong account has no models", agent: "Claude Code", noModels: true, wantError: "no matching subscription"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			home := testenv.SetTempHome(t)
			t.Setenv("PATH", "")
			t.Setenv("CODEX_HOME", filepath.Join(home, ".codex"))
			oldLogin, oldRead, oldToken, oldURL := runReconnectSubscriptionLogin, readReconnectSubscriptionCredential, FlagToken, FlagURL
			t.Cleanup(func() {
				runReconnectSubscriptionLogin, readReconnectSubscriptionCredential, FlagToken, FlagURL = oldLogin, oldRead, oldToken, oldURL
			})
			agent := AgentConfig{Name: tc.agent}
			credentialType, source := anthropicClaudeCodeOAuthCredentialType, "claude_code"
			configBytes := []byte(`{"env":{"ANTHROPIC_BASE_URL":"https://gateway.example/anthropic","ANTHROPIC_API_KEY":"synthetic-managed"}}`)
			if tc.agent == "Codex CLI" {
				agent.ConfigPath = filepath.Join(home, ".codex", "config.toml")
				credentialType, source = openaiCodexOAuthCredentialType, "codex"
				configBytes = []byte("model_provider = \"preloop\"\n")
			} else {
				agent.ConfigPath = filepath.Join(home, ".claude", "settings.json")
			}
			if err := os.MkdirAll(filepath.Dir(agent.ConfigPath), 0o700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(agent.ConfigPath, configBytes, 0o600); err != nil {
				t.Fatal(err)
			}
			if !tc.notEnrolled {
				if err := saveLocalEnrollmentState(&localEnrollmentState{AgentName: tc.agent, ConfigPath: agent.ConfigPath, RuntimePrincipalID: stableRuntimePrincipalIDForAgent(agent, ""), BackupPath: "synthetic-backup"}); err != nil {
					t.Fatal(err)
				}
			}
			statePath, _ := localEnrollmentStatePath(tc.agent, agent.ConfigPath)
			stateBefore, _ := os.ReadFile(statePath)
			requests, writes, logins := 0, 0, 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests++
				switch {
				case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
					_ = json.NewEncoder(w).Encode(managedAgentListResponse{Items: []managedAgentSummary{{ID: "agent", SessionSourceType: source, SessionSourceID: runtimePrincipalIDForAgent(agent)}}})
				case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
					models := []aiModelResponse{}
					if !tc.noModels {
						models = append(models, aiModelResponse{ID: "model", CredentialType: credentialType, CredentialsSecretID: "secret", MetaData: map[string]interface{}{"managed_agent_id": "agent"}})
					}
					_ = json.NewEncoder(w).Encode(models)
				case r.Method == http.MethodPut && r.URL.Path == "/api/v1/ai-models/model":
					writes++
					_ = json.NewEncoder(w).Encode(aiModelResponse{ID: "model", CredentialsSecretID: "secret"})
				default:
					t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
				}
			}))
			defer server.Close()
			FlagURL, FlagToken = server.URL, "synthetic-session"
			if tc.notAuthenticated {
				FlagToken = ""
			}
			runReconnectSubscriptionLogin = func(cmd *cobra.Command, args []string) error {
				logins++
				want := []string{"claude", "auth", "login", "--claudeai"}
				if tc.agent == "Codex CLI" {
					want = []string{"codex", "login"}
				}
				if !reflect.DeepEqual(args, want) {
					t.Fatalf("login args = %#v, want %#v", args, want)
				}
				if tc.loginFails {
					return errors.New("synthetic login failure")
				}
				return nil
			}
			readReconnectSubscriptionCredential = func(selected AgentConfig) map[string]interface{} {
				if selected.Name != tc.agent {
					t.Fatalf("wrong credential store: %s", selected.Name)
				}
				expires := time.Now().Add(time.Hour).UnixMilli()
				if tc.expired {
					expires = time.Now().Add(-time.Hour).UnixMilli()
				}
				return map[string]interface{}{"access": "synthetic", "refresh": "synthetic", "expires": expires, "account_id": "synthetic-account"}
			}
			cmd := &cobra.Command{}
			cmd.Flags().Bool("from-local", tc.fromLocal, "")
			var output bytes.Buffer
			cmd.SetOut(&output)
			err := runAgentsReconnect(cmd, []string{tc.agent})
			if tc.wantError != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantError) || writes != 0 {
					t.Fatalf("error=%v writes=%d, wanted %q", err, writes, tc.wantError)
				}
			} else if err != nil || writes != 1 || !strings.Contains(output.String(), "Enrollment preserved.") {
				t.Fatalf("error=%v writes=%d output=%q", err, writes, output.String())
			}
			wantLogins := 0
			if !tc.fromLocal && !tc.notEnrolled && !tc.notAuthenticated && !tc.noModels {
				wantLogins = 1
			}
			if logins != wantLogins || ((tc.notEnrolled || tc.notAuthenticated) && requests != 0) {
				t.Fatalf("logins=%d want=%d requests=%d", logins, wantLogins, requests)
			}
			after, _ := os.ReadFile(agent.ConfigPath)
			stateAfter, _ := os.ReadFile(statePath)
			if !bytes.Equal(configBytes, after) || !bytes.Equal(stateBefore, stateAfter) {
				t.Fatal("credential recovery changed local config or enrollment")
			}
		})
	}
}

func TestClaudeReconnectPrefersNativeMacOSCredential(t *testing.T) {
	file := &claudeOAuthCredential{AccessToken: "synthetic-file"}
	keychain := &claudeOAuthCredential{AccessToken: "synthetic-keychain"}
	if selectClaudeReconnectCredential(file, keychain, true) != keychain {
		t.Fatal("macOS sign-in must use the Keychain over a leftover credential file")
	}
	if selectClaudeReconnectCredential(file, nil, true) != file || selectClaudeReconnectCredential(file, keychain, false) != file {
		t.Fatal("other hosts and missing Keychain entries must preserve file fallback")
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
