package cmd

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
	"github.com/spf13/cobra"
)

func silenceCodexKeychain(t *testing.T) {
	t.Helper()
	prev := readCodexKeychainOAuthForSync
	readCodexKeychainOAuthForSync = func() (*codexOAuthCredential, string) {
		return nil, ""
	}
	t.Cleanup(func() { readCodexKeychainOAuthForSync = prev })
}

func codexTestJWT(t *testing.T, claims map[string]interface{}) string {
	t.Helper()
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"none","typ":"JWT"}`))
	payload, err := json.Marshal(claims)
	if err != nil {
		t.Fatalf("marshal claims: %v", err)
	}
	return header + "." + base64.RawURLEncoding.EncodeToString(payload) + ".sig"
}

func writeCodexAuthFile(t *testing.T, dir, access, refresh, accountID, lastRefresh string) string {
	t.Helper()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatalf("mkdir codex home: %v", err)
	}
	document := map[string]interface{}{
		"tokens": map[string]interface{}{
			"access_token":  access,
			"refresh_token": refresh,
			"account_id":    accountID,
		},
		"last_refresh": lastRefresh,
	}
	data, err := json.MarshalIndent(document, "", "  ")
	if err != nil {
		t.Fatalf("marshal auth: %v", err)
	}
	path := filepath.Join(dir, "auth.json")
	if err := os.WriteFile(path, append(data, '\n'), 0o600); err != nil {
		t.Fatalf("write auth: %v", err)
	}
	return path
}

func codexSyncAgent(t *testing.T, home string) AgentConfig {
	t.Helper()
	configPath := filepath.Join(home, ".codex", "config.toml")
	if err := os.MkdirAll(filepath.Dir(configPath), 0o700); err != nil {
		t.Fatalf("mkdir config: %v", err)
	}
	if err := os.WriteFile(configPath, []byte("model = \"gpt-5.4\"\n"), 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	return AgentConfig{Name: "Codex CLI", ConfigPath: configPath}
}

func saveCodexSyncState(t *testing.T, agent AgentConfig, stamp string, mtimeNS int64) {
	t.Helper()
	state := &localEnrollmentState{
		AgentName:                   agent.Name,
		ConfigPath:                  agent.ConfigPath,
		CodexOAuthSyncedLastRefresh: stamp,
		CodexOAuthSyncedAuthMtimeNS: mtimeNS,
	}
	if err := saveLocalEnrollmentState(state); err != nil {
		t.Fatalf("save state: %v", err)
	}
}

type codexSyncPut struct {
	Path string
	Type string
	Raw  json.RawMessage
}

func TestCodexOAuthSyncPushesOnePutPerDistinctSecretThenStops(t *testing.T) {
	silenceCodexKeychain(t)
	home := testenv.SetTempHome(t)
	codexDir := filepath.Join(home, ".codex")
	t.Setenv("CODEX_HOME", codexDir)
	agent := codexSyncAgent(t, home)
	access := codexTestJWT(t, map[string]interface{}{"exp": 1893456000})
	const refresh = "refresh-example-1"
	const lastRefresh = "2026-09-18T11:43:27.789Z"
	authPath := writeCodexAuthFile(t, codexDir, access, refresh, "acct-example", lastRefresh)
	saveCodexSyncState(t, agent, "2020-01-01T00:00:00Z", 1)

	var puts []codexSyncPut
	var requests int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
			principal := runtimePrincipalIDForAgent(agent)
			_ = json.NewEncoder(w).Encode(managedAgentListResponse{
				Items: []managedAgentSummary{{
					ID:                "agent-codex-1",
					DisplayName:       "Codex CLI",
					SessionSourceType: "codex",
					SessionSourceID:   principal,
					LifecycleState:    "active",
				}},
			})
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
			_ = json.NewEncoder(w).Encode([]aiModelResponse{
				codexSyncModel("model-alpha", "Example Alpha", "secret-shared", "agent-codex-1"),
				codexSyncModel("model-beta", "Example Beta", "secret-shared", "agent-codex-1"),
				codexSyncModel("model-gamma", "Example Gamma", "secret-other", "agent-codex-1"),
				codexSyncModel("model-delta", "Example Delta", "secret-shared", "agent-other"),
			})
		case r.Method == http.MethodPut && strings.HasPrefix(r.URL.Path, "/api/v1/ai-models/"):
			var body struct {
				CredentialType    string          `json:"credential_type"`
				CredentialPayload json.RawMessage `json:"credential_payload"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode put: %v", err)
			}
			puts = append(puts, codexSyncPut{
				Path: r.URL.Path,
				Type: body.CredentialType,
				Raw:  append(json.RawMessage(nil), body.CredentialPayload...),
			})
			_ = json.NewEncoder(w).Encode(aiModelResponse{ID: strings.TrimPrefix(r.URL.Path, "/api/v1/ai-models/")})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	restoreFlags := setCodexSyncFlags(t, server.URL)
	defer restoreFlags()

	state, err := loadLocalEnrollmentState(agent)
	if err != nil {
		t.Fatal(err)
	}
	outcome, err := syncCodexOAuthCredentials(agent, state, false)
	if err != nil {
		t.Fatal(err)
	}
	if len(puts) != 2 {
		t.Fatalf("PUTs=%d want 2 (one per distinct secret): %#v", len(puts), putPaths(puts))
	}
	if puts[0].Path != "/api/v1/ai-models/model-alpha" || puts[1].Path != "/api/v1/ai-models/model-gamma" {
		t.Fatalf("PUT paths = %v", putPaths(puts))
	}
	for _, put := range puts {
		if put.Type != "oauth_openai_codex" {
			t.Fatalf("credential_type = %q", put.Type)
		}
	}
	info, err := os.Stat(authPath)
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(authPath)
	if err != nil {
		t.Fatal(err)
	}
	parsed := parseCodexOAuthCredentialBlob(data, info.ModTime().UTC().Add(time.Hour).UnixMilli())
	if parsed == nil {
		t.Fatal("expected parsed credential")
	}
	expected, err := json.Marshal(parsed.Payload())
	if err != nil {
		t.Fatal(err)
	}
	if parsed.ExpiresAtMS != 1893456000*1000 {
		t.Fatalf("expires = %d, want JWT exp in milliseconds", parsed.ExpiresAtMS)
	}
	for _, put := range puts {
		if string(put.Raw) != string(expected) {
			t.Fatalf("payload bytes\n got %s\nwant %s", put.Raw, expected)
		}
	}
	if len(outcome.Updated) != 3 {
		t.Fatalf("updated rows = %d, want alpha, beta, and gamma", len(outcome.Updated))
	}
	reloaded, err := loadLocalEnrollmentState(agent)
	if err != nil {
		t.Fatal(err)
	}
	if reloaded.CodexOAuthSyncedLastRefresh != lastRefresh {
		t.Fatalf("stamp = %q", reloaded.CodexOAuthSyncedLastRefresh)
	}

	requestsAfterFirst := requests
	clientCalls := 0
	prevClient := newCodexOAuthSyncClient
	newCodexOAuthSyncClient = func() (*api.Client, error) {
		clientCalls++
		return nil, fmt.Errorf("client must not be opened")
	}
	t.Cleanup(func() { newCodexOAuthSyncClient = prevClient })
	second, err := syncCodexOAuthCredentials(agent, reloaded, false)
	if err != nil {
		t.Fatal(err)
	}
	if !second.Unchanged {
		t.Fatal("second sync should be the no-change path")
	}
	if clientCalls != 0 {
		t.Fatalf("no-change path opened an HTTP client %d times", clientCalls)
	}
	if requests != requestsAfterFirst {
		t.Fatalf("second sync made %d API calls, want 0", requests-requestsAfterFirst)
	}
}

func TestCodexOAuthPayloadFallbackWhenExpMissing(t *testing.T) {
	silenceCodexKeychain(t)
	home := testenv.SetTempHome(t)
	codexDir := filepath.Join(home, ".codex")
	t.Setenv("CODEX_HOME", codexDir)
	agent := codexSyncAgent(t, home)
	access := codexTestJWT(t, map[string]interface{}{"sub": "example-user"})
	const lastRefresh = "2026-09-18T11:43:27.789Z"
	authPath := writeCodexAuthFile(t, codexDir, access, "refresh-example-2", "acct-example", lastRefresh)
	saveCodexSyncState(t, agent, "2020-01-01T00:00:00Z", 1)

	var raw json.RawMessage
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
			_ = json.NewEncoder(w).Encode(managedAgentListResponse{
				Items: []managedAgentSummary{{
					ID:                "agent-codex-1",
					DisplayName:       "Codex CLI",
					SessionSourceType: "codex",
					SessionSourceID:   runtimePrincipalIDForAgent(agent),
				}},
			})
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
			_ = json.NewEncoder(w).Encode([]aiModelResponse{
				codexSyncModel("model-alpha", "Example Alpha", "secret-shared", "agent-codex-1"),
			})
		case r.Method == http.MethodPut && strings.HasPrefix(r.URL.Path, "/api/v1/ai-models/"):
			var body struct {
				CredentialPayload json.RawMessage `json:"credential_payload"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			raw = append(json.RawMessage(nil), body.CredentialPayload...)
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	restore := setCodexSyncFlags(t, server.URL)
	defer restore()

	state, err := loadLocalEnrollmentState(agent)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := syncCodexOAuthCredentials(agent, state, false); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(authPath)
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(authPath)
	if err != nil {
		t.Fatal(err)
	}
	parsed := parseCodexOAuthCredentialBlob(data, info.ModTime().UTC().Add(time.Hour).UnixMilli())
	if parsed == nil {
		t.Fatal("expected parsed credential")
	}
	wantExpires := time.Date(2026, 9, 18, 12, 43, 27, 789000000, time.UTC).UnixMilli()
	if parsed.ExpiresAtMS != wantExpires {
		t.Fatalf("fallback expires = %d, want last_refresh plus one hour (%d)", parsed.ExpiresAtMS, wantExpires)
	}
	expected, err := json.Marshal(parsed.Payload())
	if err != nil {
		t.Fatal(err)
	}
	if string(raw) != string(expected) {
		t.Fatalf("payload bytes\n got %s\nwant %s", raw, expected)
	}
}

func TestCodexOAuthSyncNoChangeDoesNotOpenHTTPClient(t *testing.T) {
	silenceCodexKeychain(t)
	home := testenv.SetTempHome(t)
	codexDir := filepath.Join(home, ".codex")
	t.Setenv("CODEX_HOME", codexDir)
	agent := codexSyncAgent(t, home)
	const lastRefresh = "2026-09-18T11:43:27.789Z"
	authPath := writeCodexAuthFile(
		t,
		codexDir,
		codexTestJWT(t, map[string]interface{}{"exp": 1893456000}),
		"refresh-example-1",
		"acct-example",
		lastRefresh,
	)
	info, err := os.Stat(authPath)
	if err != nil {
		t.Fatal(err)
	}
	saveCodexSyncState(t, agent, lastRefresh, info.ModTime().UnixNano())

	calls := 0
	prev := newCodexOAuthSyncClient
	newCodexOAuthSyncClient = func() (*api.Client, error) {
		calls++
		return nil, fmt.Errorf("client must not be opened")
	}
	t.Cleanup(func() { newCodexOAuthSyncClient = prev })

	state, err := loadLocalEnrollmentState(agent)
	if err != nil {
		t.Fatal(err)
	}
	outcome, err := syncCodexOAuthCredentials(agent, state, false)
	if err != nil {
		t.Fatal(err)
	}
	if !outcome.Unchanged || calls != 0 {
		t.Fatalf("unchanged=%v client calls=%d", outcome.Unchanged, calls)
	}
}

func TestCodexOAuthSyncPushFailureLeavesStampAndDecision(t *testing.T) {
	silenceCodexKeychain(t)
	home := testenv.SetTempHome(t)
	codexDir := filepath.Join(home, ".codex")
	t.Setenv("CODEX_HOME", codexDir)
	agent := codexSyncAgent(t, home)
	writeCodexAuthFile(
		t,
		codexDir,
		codexTestJWT(t, map[string]interface{}{"exp": 1893456000}),
		"refresh-example-1",
		"acct-example",
		"2026-09-18T11:43:27.789Z",
	)
	const originalStamp = "2020-01-01T00:00:00Z"
	saveCodexSyncState(t, agent, originalStamp, 1)
	writeTestPermissionCredential(t, home, "codex-agent", permissionHookCredential{
		BaseURL:    "placeholder",
		Token:      "agt_synthetic",
		Source:     permissionSourceCodexCLI,
		ConfigPath: agent.ConfigPath,
	})

	logs := 0
	prevLog := logCodexOAuthSyncFailure
	logCodexOAuthSyncFailure = func(err error) {
		if err != nil {
			logs++
		}
	}
	t.Cleanup(func() { logCodexOAuthSyncFailure = prevLog })

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
			_ = json.NewEncoder(w).Encode(managedAgentListResponse{
				Items: []managedAgentSummary{{
					ID:                "agent-codex-1",
					DisplayName:       "Codex CLI",
					SessionSourceType: "codex",
					SessionSourceID:   runtimePrincipalIDForAgent(agent),
				}},
			})
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
			_ = json.NewEncoder(w).Encode([]aiModelResponse{
				codexSyncModel("model-alpha", "Example Alpha", "secret-shared", "agent-codex-1"),
			})
		case r.Method == http.MethodPut:
			http.Error(w, "unavailable", http.StatusInternalServerError)
		case r.Method == http.MethodPost && r.URL.Path == permissionCheckPath:
			_ = json.NewEncoder(w).Encode(permissionCheckResponse{Decision: "allow", Reason: "Approved via Preloop."})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	restore := setCodexSyncFlags(t, server.URL)
	defer restore()
	credPath := filepath.Join(home, ".preloop", "agents", "codex-agent", permissionHookCredentialFileName)
	credRaw, err := os.ReadFile(credPath)
	if err != nil {
		t.Fatal(err)
	}
	var cred permissionHookCredential
	if err := json.Unmarshal(credRaw, &cred); err != nil {
		t.Fatal(err)
	}
	cred.BaseURL = server.URL
	rewritten, err := json.MarshalIndent(cred, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(credPath, rewritten, 0o600); err != nil {
		t.Fatal(err)
	}

	cmd := &cobra.Command{}
	cmd.Flags().String("source", permissionSourceCodexCLI, "")
	cmd.Flags().String("hook-event", "PermissionRequest", "")
	cmd.Flags().Bool("fail-open", false, "")
	cmd.SetIn(bytes.NewBufferString(`{"hook_event_name":"PermissionRequest","session_id":"session-a","tool_name":"Bash","tool_input":{"command":"ls"}}`))
	var out bytes.Buffer
	cmd.SetOut(&out)
	if err := runAgentsPermissionHook(cmd, nil); err != nil {
		t.Fatal(err)
	}
	var decision map[string]interface{}
	if err := json.Unmarshal(out.Bytes(), &decision); err != nil {
		t.Fatalf("decision %q: %v", out.String(), err)
	}
	specific, _ := decision["hookSpecificOutput"].(map[string]interface{})
	inner, _ := specific["decision"].(map[string]interface{})
	if inner["behavior"] != "allow" {
		t.Fatalf("permission decision changed: %#v", decision)
	}
	if logs != 1 {
		t.Fatalf("log calls = %d, want 1", logs)
	}
	reloaded, err := loadLocalEnrollmentState(agent)
	if err != nil {
		t.Fatal(err)
	}
	if reloaded.CodexOAuthSyncedLastRefresh != originalStamp {
		t.Fatalf("stamp advanced to %q", reloaded.CodexOAuthSyncedLastRefresh)
	}
}

func TestCodexOAuthSyncFailureModesDoNotAdvanceStamp(t *testing.T) {
	silenceCodexKeychain(t)
	for _, tc := range []struct {
		name  string
		setup func(t *testing.T, agent AgentConfig) func()
	}{
		{
			name: "network down",
			setup: func(t *testing.T, agent AgentConfig) func() {
				return setCodexSyncFlags(t, "http://127.0.0.1:1")
			},
		},
		{
			name: "stale session",
			setup: func(t *testing.T, agent AgentConfig) func() {
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					http.Error(w, "unauthorized", http.StatusUnauthorized)
				}))
				t.Cleanup(server.Close)
				return setCodexSyncFlags(t, server.URL)
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			silenceCodexKeychain(t)
			home := testenv.SetTempHome(t)
			codexDir := filepath.Join(home, ".codex")
			t.Setenv("CODEX_HOME", codexDir)
			agent := codexSyncAgent(t, home)
			writeCodexAuthFile(
				t,
				codexDir,
				codexTestJWT(t, map[string]interface{}{"exp": 1893456000}),
				"refresh-example-1",
				"acct-example",
				"2026-09-18T11:43:27.789Z",
			)
			const originalStamp = "2020-01-01T00:00:00Z"
			saveCodexSyncState(t, agent, originalStamp, 1)
			restore := tc.setup(t, agent)
			defer restore()
			state, err := loadLocalEnrollmentState(agent)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := syncCodexOAuthCredentials(agent, state, false); err == nil {
				t.Fatal("expected push error")
			}
			reloaded, err := loadLocalEnrollmentState(agent)
			if err != nil {
				t.Fatal(err)
			}
			if reloaded.CodexOAuthSyncedLastRefresh != originalStamp {
				t.Fatalf("stamp advanced to %q", reloaded.CodexOAuthSyncedLastRefresh)
			}
		})
	}
}

func TestSyncCredentialsPrintsRowsAndRefusesOtherAgents(t *testing.T) {
	silenceCodexKeychain(t)
	home := testenv.SetTempHome(t)
	codexDir := filepath.Join(home, ".codex")
	t.Setenv("CODEX_HOME", codexDir)
	agent := codexSyncAgent(t, home)
	access := codexTestJWT(t, map[string]interface{}{"exp": 1893456000})
	const refresh = "refresh-must-not-print"
	writeCodexAuthFile(t, codexDir, access, refresh, "acct-example", "2026-09-18T11:43:27.789Z")
	saveCodexSyncState(t, agent, "2020-01-01T00:00:00Z", 1)

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
			_ = json.NewEncoder(w).Encode(managedAgentListResponse{
				Items: []managedAgentSummary{{
					ID:                "agent-codex-1",
					DisplayName:       "Codex CLI",
					SessionSourceType: "codex",
					SessionSourceID:   runtimePrincipalIDForAgent(agent),
				}},
			})
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
			_ = json.NewEncoder(w).Encode([]aiModelResponse{
				codexSyncModel("model-alpha", "Example Alpha", "secret-shared", "agent-codex-1"),
				codexSyncModel("model-beta", "Example Beta", "secret-shared", "agent-codex-1"),
			})
		case r.Method == http.MethodPut:
			_, _ = w.Write([]byte(`{}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	restore := setCodexSyncFlags(t, server.URL)
	defer restore()

	cmd := &cobra.Command{}
	var out bytes.Buffer
	cmd.SetOut(&out)
	if err := runAgentsSyncCredentials(cmd, []string{"Codex CLI"}); err != nil {
		t.Fatal(err)
	}
	text := out.String()
	if !strings.Contains(text, "Example Alpha") || !strings.Contains(text, "Example Beta") {
		t.Fatalf("output missing updated rows: %s", text)
	}
	if strings.Contains(text, access) || strings.Contains(text, refresh) {
		t.Fatalf("output printed token material: %s", text)
	}
	if strings.Count(text, "\n") != 1 {
		t.Fatalf("expected one output line, got %q", text)
	}

	refused := &cobra.Command{}
	err := runAgentsSyncCredentials(refused, []string{"Claude Code"})
	if err == nil {
		t.Fatal("expected non-zero refusal")
	}
	if err.Error() != "sync-credentials only supports Codex CLI" || strings.Contains(err.Error(), "\n") {
		t.Fatalf("refusal = %q", err.Error())
	}
}

func TestCodexOAuthSyncPrefersNewerKeychainBundle(t *testing.T) {
	home := testenv.SetTempHome(t)
	codexDir := filepath.Join(home, ".codex")
	t.Setenv("CODEX_HOME", codexDir)
	agent := codexSyncAgent(t, home)
	const fileRefresh = "2026-01-01T00:00:00Z"
	authPath := writeCodexAuthFile(
		t,
		codexDir,
		codexTestJWT(t, map[string]interface{}{"exp": 1700000000}),
		"file-refresh",
		"acct-file",
		fileRefresh,
	)
	info, err := os.Stat(authPath)
	if err != nil {
		t.Fatal(err)
	}
	saveCodexSyncState(t, agent, fileRefresh, info.ModTime().UnixNano())

	keychainAccess := codexTestJWT(t, map[string]interface{}{"exp": 1893456000})
	prev := readCodexKeychainOAuthForSync
	readCodexKeychainOAuthForSync = func() (*codexOAuthCredential, string) {
		cred := parseCodexOAuthCredentialBlob([]byte(fmt.Sprintf(
			`{"tokens":{"access_token":%q,"refresh_token":"keychain-refresh","account_id":"acct-keychain"},"last_refresh":"2026-09-18T11:43:27.789Z"}`,
			keychainAccess,
		)), time.Now().UTC().Add(time.Hour).UnixMilli())
		return cred, "2026-09-18T11:43:27.789Z"
	}
	t.Cleanup(func() { readCodexKeychainOAuthForSync = prev })

	var raw json.RawMessage
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
			_ = json.NewEncoder(w).Encode(managedAgentListResponse{
				Items: []managedAgentSummary{{
					ID:                "agent-codex-1",
					SessionSourceType: "codex",
					SessionSourceID:   runtimePrincipalIDForAgent(agent),
				}},
			})
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
			_ = json.NewEncoder(w).Encode([]aiModelResponse{
				codexSyncModel("model-alpha", "Example Alpha", "secret-shared", "agent-codex-1"),
			})
		case r.Method == http.MethodPut:
			var body struct {
				CredentialPayload json.RawMessage `json:"credential_payload"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			raw = append(json.RawMessage(nil), body.CredentialPayload...)
			_, _ = w.Write([]byte(`{}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	restore := setCodexSyncFlags(t, server.URL)
	defer restore()

	state, err := loadLocalEnrollmentState(agent)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := syncCodexOAuthCredentials(agent, state, false); err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(raw, []byte(keychainAccess)) {
		t.Fatalf("PUT did not use the keychain bundle: %s", raw)
	}
	if bytes.Contains(raw, []byte("file-refresh")) {
		t.Fatalf("PUT used the file refresh token: %s", raw)
	}
}

func TestAnnotateCodexOAuth401Summary(t *testing.T) {
	codex := AgentConfig{Name: "Codex CLI"}
	claude := AgentConfig{Name: "Claude Code"}
	summary := "openai refresh failed (status=401, code=invalid_refresh_token)"
	got := annotateCodexOAuth401Summary(codex, openaiCodexOAuthCredentialType, summary)
	if !strings.Contains(got, "sync-credentials") || !strings.Contains(got, summary) {
		t.Fatalf("annotated = %q", got)
	}
	if again := annotateCodexOAuth401Summary(codex, openaiCodexOAuthCredentialType, got); again != got {
		t.Fatalf("hint duplicated: %q", again)
	}
	if annotateCodexOAuth401Summary(claude, openaiCodexOAuthCredentialType, summary) != summary {
		t.Fatal("non-Codex summary was rewritten")
	}
	if annotateCodexOAuth401Summary(codex, openaiCodexOAuthCredentialType, "healthy") != "healthy" {
		t.Fatal("healthy summary was rewritten")
	}
	summary401 := "openai refresh failed (status=401, code=invalid_refresh_token)"
	oauthShown := displayedValidationValue(codex, map[string]interface{}{
		"model_credential_type": openaiCodexOAuthCredentialType,
	}, "model_summary", summary401)
	if text, _ := oauthShown.(string); !strings.Contains(text, "sync-credentials") {
		t.Fatalf("oauth validate line = %q", oauthShown)
	}
	apiKeyShown := displayedValidationValue(codex, map[string]interface{}{
		"model_credential_type": "api_key",
	}, "error", summary401)
	if apiKeyShown != summary401 {
		t.Fatalf("api_key validate line = %q", apiKeyShown)
	}
}

func TestCodexFileBundleNewerTreatsEmptyMarkerAsMtime(t *testing.T) {
	const stamp = "2026-09-18T11:43:27.789Z"
	if !codexFileBundleNewer(stamp, 100, "", 200) {
		t.Fatal("empty last_refresh with a newer mtime should be pushed")
	}
	if codexFileBundleNewer(stamp, 200, "", 100) {
		t.Fatal("empty last_refresh with an older mtime should not be pushed")
	}
	if codexFileBundleNewer(stamp, 100, "", 100) {
		t.Fatal("empty last_refresh with the same mtime should not be pushed")
	}
}

func TestSaveLocalEnrollmentStateReplacesAtomically(t *testing.T) {
	home := testenv.SetTempHome(t)
	agent := codexSyncAgent(t, home)
	state := &localEnrollmentState{
		AgentName:                   agent.Name,
		ConfigPath:                  agent.ConfigPath,
		CodexOAuthSyncedLastRefresh: "first",
	}
	if err := saveLocalEnrollmentState(state); err != nil {
		t.Fatal(err)
	}
	state.CodexOAuthSyncedLastRefresh = "second"
	if err := saveLocalEnrollmentState(state); err != nil {
		t.Fatal(err)
	}
	loaded, err := loadLocalEnrollmentState(agent)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.CodexOAuthSyncedLastRefresh != "second" {
		t.Fatalf("reloaded stamp = %q", loaded.CodexOAuthSyncedLastRefresh)
	}
	path, err := localEnrollmentStatePath(agent.Name, agent.ConfigPath)
	if err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	// Windows reports no Unix permission bits, so Stat comes back as 0666
	// even after Chmod 0600.
	if runtime.GOOS != "windows" && info.Mode().Perm() != 0o600 {
		t.Fatalf("mode = %o", info.Mode().Perm())
	}
	matches, err := filepath.Glob(filepath.Join(filepath.Dir(path), ".enrollment-*.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(matches) != 0 {
		t.Fatalf("leftover temp files: %v", matches)
	}
}

func TestCodexOAuthSyncEmptyMarkerUsesMtime(t *testing.T) {
	silenceCodexKeychain(t)
	home := testenv.SetTempHome(t)
	codexDir := filepath.Join(home, ".codex")
	t.Setenv("CODEX_HOME", codexDir)
	agent := codexSyncAgent(t, home)
	access := codexTestJWT(t, map[string]interface{}{"exp": 1893456000})
	document := map[string]interface{}{
		"tokens": map[string]interface{}{
			"access_token":  access,
			"refresh_token": "refresh-example-1",
			"account_id":    "acct-example",
		},
	}
	data, err := json.MarshalIndent(document, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(codexDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(codexDir, "auth.json"), data, 0o600); err != nil {
		t.Fatal(err)
	}
	saveCodexSyncState(t, agent, "2026-09-18T11:43:27.789Z", 1)

	puts := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
			_ = json.NewEncoder(w).Encode(managedAgentListResponse{
				Items: []managedAgentSummary{{
					ID:                "agent-codex-1",
					SessionSourceType: "codex",
					SessionSourceID:   runtimePrincipalIDForAgent(agent),
				}},
			})
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
			_ = json.NewEncoder(w).Encode([]aiModelResponse{
				codexSyncModel("model-alpha", "Example Alpha", "secret-shared", "agent-codex-1"),
			})
		case r.Method == http.MethodPut:
			puts++
			_, _ = w.Write([]byte(`{}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	restore := setCodexSyncFlags(t, server.URL)
	defer restore()

	state, err := loadLocalEnrollmentState(agent)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := syncCodexOAuthCredentials(agent, state, false); err != nil {
		t.Fatal(err)
	}
	if puts != 1 {
		t.Fatalf("PUTs = %d, want 1 for an empty last_refresh and a newer mtime", puts)
	}
}

func TestCodexOAuthSyncPartialPutReportsUpdatedRows(t *testing.T) {
	silenceCodexKeychain(t)
	home := testenv.SetTempHome(t)
	codexDir := filepath.Join(home, ".codex")
	t.Setenv("CODEX_HOME", codexDir)
	agent := codexSyncAgent(t, home)
	writeCodexAuthFile(
		t,
		codexDir,
		codexTestJWT(t, map[string]interface{}{"exp": 1893456000}),
		"refresh-must-not-print",
		"acct-example",
		"2026-09-18T11:43:27.789Z",
	)
	const originalStamp = "2020-01-01T00:00:00Z"
	saveCodexSyncState(t, agent, originalStamp, 1)

	puts := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
			_ = json.NewEncoder(w).Encode(managedAgentListResponse{
				Items: []managedAgentSummary{{
					ID:                "agent-codex-1",
					SessionSourceType: "codex",
					SessionSourceID:   runtimePrincipalIDForAgent(agent),
				}},
			})
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
			_ = json.NewEncoder(w).Encode([]aiModelResponse{
				codexSyncModel("model-alpha", "Example Alpha", "secret-a", "agent-codex-1"),
				codexSyncModel("model-beta", "Example Beta", "secret-a", "agent-codex-1"),
				codexSyncModel("model-gamma", "Example Gamma", "secret-b", "agent-codex-1"),
			})
		case r.Method == http.MethodPut:
			puts++
			if puts == 1 {
				_, _ = w.Write([]byte(`{}`))
				return
			}
			http.Error(w, "unavailable", http.StatusInternalServerError)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	restore := setCodexSyncFlags(t, server.URL)
	defer restore()

	cmd := &cobra.Command{}
	var out bytes.Buffer
	cmd.SetOut(&out)
	err := runAgentsSyncCredentials(cmd, []string{"Codex CLI"})
	if err == nil {
		t.Fatal("expected the second PUT to fail")
	}
	text := out.String()
	if !strings.Contains(text, "Example Alpha") || !strings.Contains(text, "Example Beta") {
		t.Fatalf("partial success was not printed: %s", text)
	}
	if strings.Contains(text, "Example Gamma") {
		t.Fatalf("failed group was reported as updated: %s", text)
	}
	if strings.Contains(text, "refresh-must-not-print") {
		t.Fatalf("output printed token material: %s", text)
	}
	reloaded, loadErr := loadLocalEnrollmentState(agent)
	if loadErr != nil {
		t.Fatal(loadErr)
	}
	if reloaded.CodexOAuthSyncedLastRefresh != originalStamp {
		t.Fatalf("stamp advanced to %q", reloaded.CodexOAuthSyncedLastRefresh)
	}
	if strings.TrimSpace(reloaded.CodexOAuthSyncLastAttempt) == "" {
		t.Fatal("failed push did not record an attempt")
	}
}

func TestCodexOAuthSyncBackoffAndAttemptTimeout(t *testing.T) {
	silenceCodexKeychain(t)
	home := testenv.SetTempHome(t)
	codexDir := filepath.Join(home, ".codex")
	t.Setenv("CODEX_HOME", codexDir)
	agent := codexSyncAgent(t, home)
	writeCodexAuthFile(
		t,
		codexDir,
		codexTestJWT(t, map[string]interface{}{"exp": 1893456000}),
		"refresh-example-1",
		"acct-example",
		"2026-09-18T11:43:27.789Z",
	)
	saveCodexSyncState(t, agent, "2020-01-01T00:00:00Z", 1)
	state, err := loadLocalEnrollmentState(agent)
	if err != nil {
		t.Fatal(err)
	}
	state.CodexOAuthSyncLastAttempt = time.Now().UTC().Format(time.RFC3339Nano)
	if err := saveLocalEnrollmentState(state); err != nil {
		t.Fatal(err)
	}

	calls := 0
	prevClient := newCodexOAuthSyncClient
	newCodexOAuthSyncClient = func() (*api.Client, error) {
		calls++
		return nil, fmt.Errorf("opened")
	}
	t.Cleanup(func() { newCodexOAuthSyncClient = prevClient })

	fresh, err := loadLocalEnrollmentState(agent)
	if err != nil {
		t.Fatal(err)
	}
	outcome, err := syncCodexOAuthCredentials(agent, fresh, false)
	if err != nil {
		t.Fatal(err)
	}
	if !outcome.Unchanged || calls != 0 {
		t.Fatalf("backoff outcome=%+v calls=%d", outcome, calls)
	}

	forced, err := syncCodexOAuthCredentials(agent, fresh, true)
	if err == nil || calls != 1 {
		t.Fatalf("force err=%v calls=%d outcome=%+v", err, calls, forced)
	}

	fresh.CodexOAuthSyncLastAttempt = time.Now().Add(-2 * time.Minute).UTC().Format(time.RFC3339Nano)
	if err := saveLocalEnrollmentState(fresh); err != nil {
		t.Fatal(err)
	}
	retry, err := loadLocalEnrollmentState(agent)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := syncCodexOAuthCredentials(agent, retry, false); err == nil || calls != 2 {
		t.Fatalf("expired backoff err=%v calls=%d", err, calls)
	}

	newCodexOAuthSyncClient = prevClient
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
			_ = json.NewEncoder(w).Encode(managedAgentListResponse{
				Items: []managedAgentSummary{{
					ID:                "agent-codex-1",
					SessionSourceType: "codex",
					SessionSourceID:   runtimePrincipalIDForAgent(agent),
				}},
			})
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
			_ = json.NewEncoder(w).Encode([]aiModelResponse{
				codexSyncModel("model-alpha", "Example Alpha", "secret-shared", "agent-codex-1"),
			})
		case r.Method == http.MethodPut:
			select {
			case <-r.Context().Done():
			case <-time.After(20 * time.Second):
			}
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	restore := setCodexSyncFlags(t, server.URL)
	defer restore()
	hung, err := loadLocalEnrollmentState(agent)
	if err != nil {
		t.Fatal(err)
	}
	hung.CodexOAuthSyncLastAttempt = ""
	started := time.Now()
	if _, err := syncCodexOAuthCredentials(agent, hung, false); err == nil {
		t.Fatal("expected the hung PUT to fail")
	}
	if elapsed := time.Since(started); elapsed > 8*time.Second {
		t.Fatalf("hung sync took %s, want under 8s", elapsed)
	}
}

func TestCodexOAuthSyncFailureDoesNotRecreateRemovedEnrollment(t *testing.T) {
	silenceCodexKeychain(t)
	home := testenv.SetTempHome(t)
	codexDir := filepath.Join(home, ".codex")
	t.Setenv("CODEX_HOME", codexDir)
	agent := codexSyncAgent(t, home)
	writeCodexAuthFile(
		t,
		codexDir,
		codexTestJWT(t, map[string]interface{}{"exp": 1893456000}),
		"refresh-example-1",
		"acct-example",
		"2026-09-18T11:43:27.789Z",
	)
	saveCodexSyncState(t, agent, "2020-01-01T00:00:00Z", 1)
	statePath, err := localEnrollmentStatePath(agent.Name, agent.ConfigPath)
	if err != nil {
		t.Fatal(err)
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := removeLocalEnrollmentState(agent); err != nil {
			t.Errorf("remove enrollment: %v", err)
		}
		http.Error(w, "unavailable", http.StatusInternalServerError)
	}))
	defer server.Close()
	restore := setCodexSyncFlags(t, server.URL)
	defer restore()

	state, err := loadLocalEnrollmentState(agent)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := syncCodexOAuthCredentials(agent, state, false); err == nil {
		t.Fatal("expected push error")
	}
	if _, err := os.Stat(statePath); !os.IsNotExist(err) {
		t.Fatalf("failure save recreated enrollment state: %v", err)
	}
}

func codexSyncModel(id, name, secretID, agentID string) aiModelResponse {
	return aiModelResponse{
		ID:                  id,
		Name:                name,
		CredentialType:      openaiCodexOAuthCredentialType,
		CredentialsSecretID: secretID,
		MetaData:            map[string]interface{}{"managed_agent_id": agentID},
	}
}

func putPaths(puts []codexSyncPut) []string {
	paths := make([]string, 0, len(puts))
	for _, put := range puts {
		paths = append(paths, put.Path)
	}
	return paths
}

func setCodexSyncFlags(t *testing.T, serverURL string) func() {
	t.Helper()
	oldURL := FlagURL
	oldToken := FlagToken
	FlagURL = serverURL
	FlagToken = "test-token"
	return func() {
		FlagURL = oldURL
		FlagToken = oldToken
	}
}
