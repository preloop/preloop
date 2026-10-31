package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
	"github.com/spf13/cobra"
)

const (
	claudeSyncLocalAccess   = "sk-ant-oat01-local-must-not-print"
	claudeSyncLocalRefresh  = "sk-ant-ort01-local-must-not-print"
	claudeSyncServerAccess  = "sk-ant-oat01-server-must-not-print"
	claudeSyncServerRefresh = "sk-ant-ort01-server-must-not-print"
	claudeSyncLocalExpires  = int64(1893456000000)
	claudeSyncServerExpires = int64(1893500000000)
)

type claudeSyncFixture struct {
	t        *testing.T
	home     string
	credPath string
	agent    AgentConfig
	server   *httptest.Server

	mu           sync.Mutex
	requests     []string
	puts         []map[string]interface{}
	marker       map[string]interface{}
	listStatus   int
	exportsTaken int
	splitSecrets bool
}

func newClaudeSyncFixture(t *testing.T) *claudeSyncFixture {
	t.Helper()
	home := testenv.SetTempHome(t)
	prevKeychain := readClaudeKeychainBlobForSync
	readClaudeKeychainBlobForSync = func() (string, error) { return "", nil }
	prevOffboard := readClaudeOffboardKeychain
	readClaudeOffboardKeychain = func() (string, error) { return "", nil }
	t.Cleanup(func() {
		readClaudeKeychainBlobForSync = prevKeychain
		readClaudeOffboardKeychain = prevOffboard
	})
	configPath := filepath.Join(home, ".claude", "settings.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(configPath, []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	f := &claudeSyncFixture{
		t:          t,
		home:       home,
		credPath:   filepath.Join(home, ".claude", ".credentials.json"),
		agent:      AgentConfig{Name: "Claude Code", ConfigPath: configPath},
		listStatus: http.StatusOK,
	}
	f.marker = map[string]interface{}{
		"credential_type":    anthropicClaudeCodeOAuthCredentialType,
		"expires":            claudeSyncLocalExpires,
		"last_refresh":       "2026-01-01T00:00:00Z",
		"credentials_status": "active",
	}
	f.writeLocal(claudeSyncLocalAccess, claudeSyncLocalRefresh, claudeSyncLocalExpires)
	if err := saveLocalEnrollmentState(&localEnrollmentState{
		AgentName:  f.agent.Name,
		ConfigPath: f.agent.ConfigPath,
	}); err != nil {
		t.Fatal(err)
	}
	f.server = httptest.NewServer(http.HandlerFunc(f.handle))
	t.Cleanup(f.server.Close)
	t.Cleanup(setCodexSyncFlags(t, f.server.URL))
	return f
}

func (f *claudeSyncFixture) writeLocal(access, refresh string, expires int64) {
	f.t.Helper()
	document := map[string]interface{}{
		"claudeAiOauth": map[string]interface{}{
			"accessToken":      access,
			"refreshToken":     refresh,
			"expiresAt":        expires,
			"scopes":           []string{"user:inference"},
			"subscriptionType": "max",
		},
	}
	data, _ := json.Marshal(document)
	if err := os.WriteFile(f.credPath, data, 0o600); err != nil {
		f.t.Fatal(err)
	}
	// Move mtime forward so the cheap stat path sees a rewrite.
	future := time.Now().Add(time.Duration(len(f.requests)+1) * time.Second)
	_ = os.Chtimes(f.credPath, future, future)
}

func (f *claudeSyncFixture) opusSecret() string {
	if f.splitSecrets {
		return "secret-claude-split"
	}
	return "secret-claude"
}

func (f *claudeSyncFixture) stampSynced(expires int64) {
	f.t.Helper()
	state := f.state()
	state.ClaudeOAuthSyncedLocalExpiresMS = expires
	state.ClaudeOAuthSyncedServerExpiresMS = expires
	if err := saveLocalEnrollmentState(state); err != nil {
		f.t.Fatal(err)
	}
}

func (f *claudeSyncFixture) state() *localEnrollmentState {
	f.t.Helper()
	state, err := loadLocalEnrollmentState(f.agent)
	if err != nil {
		f.t.Fatal(err)
	}
	return state
}

func (f *claudeSyncFixture) requestLog() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.requests...)
}

func (f *claudeSyncFixture) handle(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.requests = append(f.requests, r.Method+" "+r.URL.Path)
	w.Header().Set("Content-Type", "application/json")
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
		_ = json.NewEncoder(w).Encode(managedAgentListResponse{Items: []managedAgentSummary{{
			ID:                "agent-claude-1",
			DisplayName:       "Claude Code",
			SessionSourceType: runtimeSessionSourceTypeForAgent("Claude Code"),
			SessionSourceID:   runtimePrincipalIDForAgent(f.agent),
			LifecycleState:    "active",
		}}})
	case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
		if f.listStatus != http.StatusOK {
			w.WriteHeader(f.listStatus)
			_, _ = w.Write([]byte(`{"detail":"boom"}`))
			return
		}
		codex := codexSyncModel("model-codex", "Codex row", "secret-codex", "agent-claude-1")
		_ = json.NewEncoder(w).Encode([]aiModelResponse{
			{
				ID: "model-sonnet", Name: "Example Sonnet", CredentialType: anthropicClaudeCodeOAuthCredentialType,
				CredentialsSecretID: "secret-claude", MetaData: map[string]interface{}{"managed_agent_id": "agent-claude-1"},
			},
			{
				ID: "model-opus", Name: "Example Opus", CredentialType: anthropicClaudeCodeOAuthCredentialType,
				CredentialsSecretID: f.opusSecret(), MetaData: map[string]interface{}{"managed_agent_id": "agent-claude-1"},
			},
			codex,
		})
	case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/credentials/marker"):
		_ = json.NewEncoder(w).Encode(f.marker)
	case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/credentials/export"):
		f.exportsTaken++
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"credential_type": anthropicClaudeCodeOAuthCredentialType,
			"access":          claudeSyncServerAccess,
			"refresh":         claudeSyncServerRefresh,
			"expires":         f.marker["expires"],
		})
	case r.Method == http.MethodPut && strings.HasPrefix(r.URL.Path, "/api/v1/ai-models/"):
		var body map[string]interface{}
		_ = json.NewDecoder(r.Body).Decode(&body)
		body["_path"] = r.URL.Path
		f.puts = append(f.puts, body)
		_ = json.NewEncoder(w).Encode(aiModelResponse{ID: strings.TrimPrefix(r.URL.Path, "/api/v1/ai-models/")})
	default:
		http.Error(w, "unexpected", http.StatusNotFound)
	}
}

func TestClaudeLocalBundleParseAndCompare(t *testing.T) {
	cred := parseClaudeOAuthCredentialBlob(
		`{"claudeAiOauth":{"accessToken":"sk-ant-oat01-x","refreshToken":"r","expiresAt":1893456000000,"scopes":["a"],"subscriptionType":"max"}}`,
		0,
	)
	if cred == nil || cred.ExpiresAtMS != 1893456000000 || cred.RefreshToken != "r" {
		t.Fatalf("parse = %+v", cred)
	}
	cases := []struct {
		local, synced int64
		want          bool
	}{
		{100, 0, true},
		{100, 100, false},
		{101, 100, true},
		{99, 100, false},
	}
	for _, tc := range cases {
		if got := claudeLocalBundleNewer(tc.local, tc.synced); got != tc.want {
			t.Fatalf("claudeLocalBundleNewer(%d,%d)=%v", tc.local, tc.synced, got)
		}
	}
	marker := codexOAuthServerMarker{Expires: 200}
	if !claudeServerMarkerNewer(marker, &localEnrollmentState{ClaudeOAuthSyncedServerExpiresMS: 150}, 300) {
		t.Fatal("stamped server expiry must be the reference")
	}
	if claudeServerMarkerNewer(marker, &localEnrollmentState{}, 300) {
		t.Fatal("without a stamp the local expiry is the reference")
	}
}

func TestNewestClaudeOAuthMarkerSkipsDeadAndForeignRows(t *testing.T) {
	best := newestClaudeOAuthMarker([]codexOAuthServerMarker{
		{ModelID: "dead", CredentialType: anthropicClaudeCodeOAuthCredentialType, Expires: 900, CredentialsStatus: "error"},
		{ModelID: "codex", CredentialType: openaiCodexOAuthCredentialType, Expires: 800},
		{ModelID: "old", CredentialType: anthropicClaudeCodeOAuthCredentialType, Expires: 100},
		{ModelID: "live", CredentialType: anthropicClaudeCodeOAuthCredentialType, Expires: 500},
	})
	if best == nil || best.ModelID != "live" {
		t.Fatalf("best = %+v", best)
	}
}

func TestClaudeOAuthSyncPushesNewerLocalLoginOncePerSecret(t *testing.T) {
	f := newClaudeSyncFixture(t)
	outcome, err := syncClaudeOAuthCredentials(f.agent, f.state(), false)
	if err != nil {
		t.Fatalf("sync: %v", err)
	}
	if outcome.Direction != claudeOAuthSyncDirectionPush || len(outcome.Updated) != 2 {
		t.Fatalf("outcome = %+v", outcome)
	}
	if len(f.puts) != 1 {
		t.Fatalf("expected one PUT for the shared secret, got %d", len(f.puts))
	}
	put := f.puts[0]
	if put["credential_type"] != anthropicClaudeCodeOAuthCredentialType || put["_path"] != "/api/v1/ai-models/model-sonnet" {
		t.Fatalf("put = %v", put)
	}
	payload, _ := put["credential_payload"].(map[string]interface{})
	if payload["refresh"] != claudeSyncLocalRefresh || int64(payload["expires"].(float64)) != claudeSyncLocalExpires {
		t.Fatalf("payload did not carry the local login")
	}
	state := f.state()
	if state.ClaudeOAuthSyncedLocalExpiresMS != claudeSyncLocalExpires ||
		state.ClaudeOAuthSyncedServerExpiresMS != claudeSyncLocalExpires ||
		state.ClaudeOAuthSyncedFileMtimeNS == 0 || state.ClaudeOAuthServerCheckedAt == "" {
		t.Fatalf("stamps not recorded: %+v", state)
	}
	if text := formatClaudeOAuthSyncOutcome(outcome); strings.Contains(text, claudeSyncLocalRefresh) ||
		strings.Contains(text, claudeSyncLocalAccess) || !strings.Contains(text, "Example Opus") {
		t.Fatalf("format = %q", text)
	}

	before := len(f.requestLog())
	again, err := syncClaudeOAuthCredentials(f.agent, f.state(), false)
	if err != nil || !again.Unchanged {
		t.Fatalf("second sync = %+v, %v", again, err)
	}
	if after := len(f.requestLog()); after != before {
		t.Fatalf("unchanged login with no marker check due made %d requests", after-before)
	}
}

func TestClaudeOAuthSyncPullsNewerServerLoginPreservingFields(t *testing.T) {
	f := newClaudeSyncFixture(t)
	state := f.state()
	state.ClaudeOAuthSyncedLocalExpiresMS = claudeSyncLocalExpires
	state.ClaudeOAuthSyncedServerExpiresMS = claudeSyncLocalExpires
	if err := saveLocalEnrollmentState(state); err != nil {
		t.Fatal(err)
	}
	f.marker["expires"] = claudeSyncServerExpires

	outcome, err := syncClaudeOAuthCredentials(f.agent, f.state(), false)
	if err != nil {
		t.Fatalf("sync: %v", err)
	}
	wantPath, _ := filepath.EvalSymlinks(f.credPath)
	if outcome.Direction != claudeOAuthSyncDirectionPull || outcome.Destination != wantPath {
		t.Fatalf("outcome = %+v", outcome)
	}
	if len(f.puts) != 0 {
		t.Fatal("a pull must not push")
	}
	data, _ := os.ReadFile(f.credPath)
	var document map[string]map[string]interface{}
	_ = json.Unmarshal(data, &document)
	oauth := document["claudeAiOauth"]
	if oauth["accessToken"] != claudeSyncServerAccess || oauth["refreshToken"] != claudeSyncServerRefresh ||
		int64(oauth["expiresAt"].(float64)) != claudeSyncServerExpires {
		t.Fatalf("local login not replaced")
	}
	if oauth["subscriptionType"] != "max" || oauth["scopes"] == nil {
		t.Fatalf("pull dropped fields Preloop does not hold: %v", oauth)
	}
	after := f.state()
	if after.ClaudeOAuthSyncedServerExpiresMS != claudeSyncServerExpires ||
		after.ClaudeOAuthSyncedLocalExpiresMS != claudeSyncServerExpires {
		t.Fatalf("stamps not advanced: %+v", after)
	}
	if text := formatClaudeOAuthSyncOutcome(outcome); strings.Contains(text, claudeSyncServerRefresh) {
		t.Fatalf("format printed token material: %q", text)
	}
}

func TestClaudeOAuthSyncNeverPullsWithoutLocalLogin(t *testing.T) {
	f := newClaudeSyncFixture(t)
	if err := os.Remove(f.credPath); err != nil {
		t.Fatal(err)
	}
	f.marker["expires"] = claudeSyncServerExpires
	outcome, err := syncClaudeOAuthCredentials(f.agent, f.state(), false)
	if err != nil || !outcome.Unchanged {
		t.Fatalf("outcome = %+v, %v", outcome, err)
	}
	if len(f.requestLog()) != 0 {
		t.Fatalf("no local login must mean no API call: %v", f.requestLog())
	}
	if _, err := os.Stat(f.credPath); !os.IsNotExist(err) {
		t.Fatal("a pull wrote a login onto a single-holder host")
	}
}

func TestClaudeOAuthSyncSkipsErroredServerCopy(t *testing.T) {
	f := newClaudeSyncFixture(t)
	state := f.state()
	state.ClaudeOAuthSyncedLocalExpiresMS = claudeSyncLocalExpires
	state.ClaudeOAuthSyncedServerExpiresMS = claudeSyncLocalExpires
	_ = saveLocalEnrollmentState(state)
	f.marker["expires"] = claudeSyncServerExpires
	f.marker["credentials_status"] = "error"
	outcome, err := syncClaudeOAuthCredentials(f.agent, f.state(), false)
	if err != nil || outcome.Direction != "" || f.exportsTaken != 0 {
		t.Fatalf("errored copy must never overwrite the local login: %+v %v", outcome, err)
	}
}

func TestClaudeOAuthSyncFailureBacksOffAndKeepsStamp(t *testing.T) {
	f := newClaudeSyncFixture(t)
	f.listStatus = http.StatusInternalServerError
	if _, err := syncClaudeOAuthCredentials(f.agent, f.state(), false); err == nil {
		t.Fatal("expected a push failure")
	}
	state := f.state()
	if state.ClaudeOAuthSyncLastAttempt == "" || state.ClaudeOAuthSyncedLocalExpiresMS != 0 {
		t.Fatalf("failure must set back-off and leave the stamp: %+v", state)
	}
	before := len(f.requestLog())
	if _, err := syncClaudeOAuthCredentials(f.agent, f.state(), false); err != nil {
		t.Fatalf("back-off pass returned %v", err)
	}
	if len(f.requestLog()) != before {
		t.Fatal("back-off pass made an API call")
	}
}

func TestClaudeOAuthHookSyncNeverWritesStdout(t *testing.T) {
	f := newClaudeSyncFixture(t)
	f.listStatus = http.StatusInternalServerError
	logged := 0
	prevLog := logClaudeOAuthSyncFailure
	logClaudeOAuthSyncFailure = func(err error) {
		if strings.Contains(err.Error(), claudeSyncLocalRefresh) {
			t.Errorf("logged token material")
		}
		logged++
	}
	t.Cleanup(func() { logClaudeOAuthSyncFailure = prevLog })
	out := captureStdout(t, func() error { maybeSyncClaudeOAuthFromHook(); return nil })
	if out != "" || logged != 1 {
		t.Fatalf("stdout=%q logged=%d", out, logged)
	}
}

func stubSubscriptionHookSyncs(t *testing.T) (*int, *int) {
	t.Helper()
	codex, claude := 0, 0
	prevCodex, prevClaude := syncCodexOAuthFromHook, syncClaudeOAuthFromHook
	syncCodexOAuthFromHook = func() { codex++ }
	syncClaudeOAuthFromHook = func() { claude++ }
	t.Cleanup(func() {
		syncCodexOAuthFromHook = prevCodex
		syncClaudeOAuthFromHook = prevClaude
	})
	return &codex, &claude
}

func TestSessionHookRunsSubscriptionSyncForBothSources(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	codex, claude := stubSubscriptionHookSyncs(t)
	for _, tc := range []struct {
		source, event string
	}{
		{permissionSourceClaudeCode, "SessionStart"},
		{permissionSourceClaudeCode, "UserPromptSubmit"},
		{permissionSourceCodexCLI, "SessionStart"},
	} {
		raw, _ := json.Marshal(map[string]interface{}{"hook_event_name": tc.event, "session_id": "ext-sync"})
		var stdout bytes.Buffer
		agentsSessionHookCmd.SetIn(bytes.NewReader(raw))
		agentsSessionHookCmd.SetOut(&stdout)
		agentsSessionHookCmd.SetErr(&bytes.Buffer{})
		if err := agentsSessionHookCmd.Flags().Set("source", tc.source); err != nil {
			t.Fatal(err)
		}
		if err := runAgentsSessionHook(agentsSessionHookCmd, nil); err != nil {
			t.Fatalf("session hook returned %v", err)
		}
		if stdout.Len() != 0 {
			t.Fatalf("session hook stdout = %q", stdout.String())
		}
	}
	_ = agentsSessionHookCmd.Flags().Set("source", permissionSourceClaudeCode)
	if *claude != 2 || *codex != 1 {
		t.Fatalf("claude=%d codex=%d", *claude, *codex)
	}
}

func TestSubscriptionHookSyncSkipsFlowCredentialRuns(t *testing.T) {
	codex, claude := stubSubscriptionHookSyncs(t)
	t.Setenv("PRELOOP_FLOW_CREDENTIAL_REQUIRED", "1")
	maybeSyncSubscriptionOAuthFromHook(permissionSourceClaudeCode)
	maybeSyncSubscriptionOAuthFromHook(permissionSourceCodexCLI)
	if *codex != 0 || *claude != 0 {
		t.Fatalf("flow run synced: claude=%d codex=%d", *claude, *codex)
	}
	t.Setenv("PRELOOP_FLOW_CREDENTIAL_REQUIRED", "")
	maybeSyncSubscriptionOAuthFromHook(permissionSourceClaudeCode)
	maybeSyncSubscriptionOAuthFromHook(permissionSourceCodexCLI)
	maybeSyncSubscriptionOAuthFromHook(permissionSourceCursor)
	if *codex != 1 || *claude != 1 {
		t.Fatalf("dispatch: claude=%d codex=%d", *claude, *codex)
	}
}

func TestSyncCredentialsDispatchesClaudeCode(t *testing.T) {
	f := newClaudeSyncFixture(t)
	if err := os.WriteFile(filepath.Join(f.home, ".claude.json"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	cmd := &cobra.Command{}
	cmd.SetOut(&out)
	if err := runAgentsSyncCredentials(cmd, []string{"Claude Code"}); err != nil {
		t.Fatalf("sync-credentials Claude Code: %v", err)
	}
	text := out.String()
	if !strings.Contains(text, "Pushed the local Claude Code login") || strings.Count(text, "\n") != 1 ||
		strings.Contains(text, claudeSyncLocalRefresh) {
		t.Fatalf("output = %q", text)
	}
	if len(f.puts) != 1 {
		t.Fatalf("puts = %d", len(f.puts))
	}
	err := runAgentsSyncCredentials(&cobra.Command{}, []string{"Cursor"})
	if err == nil || !strings.Contains(err.Error(), "Codex CLI and Claude Code") {
		t.Fatalf("refusal = %v", err)
	}
}

func TestClaudeOAuthSyncConflictLaterExpiryWins(t *testing.T) {
	t.Run("server wins", func(t *testing.T) {
		f := newClaudeSyncFixture(t)
		f.stampSynced(claudeSyncLocalExpires - 1000)
		f.marker["expires"] = claudeSyncServerExpires
		outcome, err := syncClaudeOAuthCredentials(f.agent, f.state(), false)
		if err != nil || outcome.Direction != claudeOAuthSyncDirectionPull || !outcome.Conflict || len(f.puts) != 0 {
			t.Fatalf("outcome = %+v, %v, puts=%d", outcome, err, len(f.puts))
		}
	})
	t.Run("local wins", func(t *testing.T) {
		f := newClaudeSyncFixture(t)
		f.stampSynced(claudeSyncLocalExpires - 2000)
		f.marker["expires"] = claudeSyncLocalExpires - 1000
		outcome, err := syncClaudeOAuthCredentials(f.agent, f.state(), false)
		if err != nil || outcome.Direction != claudeOAuthSyncDirectionPush || !outcome.Conflict || f.exportsTaken != 0 {
			t.Fatalf("outcome = %+v, %v, exports=%d", outcome, err, f.exportsTaken)
		}
	})
}

func claudeKeychainBlob(access, refresh string, expires int64) string {
	data, _ := json.Marshal(map[string]interface{}{"claudeAiOauth": map[string]interface{}{
		"accessToken": access, "refreshToken": refresh, "expiresAt": expires,
		"scopes": []string{"user:inference"}, "subscriptionType": "max",
	}})
	return string(data)
}

func useFakeClaudeKeychain(t *testing.T, blob *string) {
	t.Helper()
	prevSync, prevRead, prevWrite := readClaudeKeychainBlobForSync, readClaudeOffboardKeychain, writeClaudeOffboardKeychain
	readClaudeKeychainBlobForSync = func() (string, error) { return *blob, nil }
	readClaudeOffboardKeychain = func() (string, error) { return *blob, nil }
	writeClaudeOffboardKeychain = func(next string) error { *blob = next; return nil }
	t.Cleanup(func() {
		readClaudeKeychainBlobForSync, readClaudeOffboardKeychain, writeClaudeOffboardKeychain = prevSync, prevRead, prevWrite
	})
}

func TestClaudeOAuthSyncKeychainPushAndPull(t *testing.T) {
	f := newClaudeSyncFixture(t)
	_ = os.Remove(f.credPath)
	blob := claudeKeychainBlob(claudeSyncLocalAccess, claudeSyncLocalRefresh, claudeSyncLocalExpires)
	useFakeClaudeKeychain(t, &blob)

	outcome, err := syncClaudeOAuthCredentials(f.agent, f.state(), false)
	if err != nil || outcome.Direction != claudeOAuthSyncDirectionPush || len(f.puts) != 1 {
		t.Fatalf("keychain push = %+v, %v", outcome, err)
	}
	payload, _ := f.puts[0]["credential_payload"].(map[string]interface{})
	if payload["refresh"] != claudeSyncLocalRefresh {
		t.Fatal("push did not carry the Keychain login")
	}

	state := f.state()
	state.ClaudeOAuthServerCheckedAt = ""
	_ = saveLocalEnrollmentState(state)
	f.marker["expires"] = claudeSyncServerExpires
	outcome, err = syncClaudeOAuthCredentials(f.agent, f.state(), false)
	if err != nil || outcome.Direction != claudeOAuthSyncDirectionPull || outcome.Destination != "the macOS Keychain" {
		t.Fatalf("keychain pull = %+v, %v", outcome, err)
	}
	cred := parseClaudeOAuthCredentialBlob(blob, 0)
	if cred == nil || cred.RefreshToken != claudeSyncServerRefresh || cred.ExpiresAtMS != claudeSyncServerExpires {
		t.Fatal("Keychain login not replaced")
	}
	if !strings.Contains(blob, "subscriptionType") {
		t.Fatal("pull dropped Keychain fields")
	}
	if _, err := os.Stat(f.credPath); !os.IsNotExist(err) {
		t.Fatal("a Keychain pull wrote a credentials file")
	}
}

func TestClaudeOAuthSyncRefusesSplitSecrets(t *testing.T) {
	f := newClaudeSyncFixture(t)
	f.splitSecrets = true
	_, err := syncClaudeOAuthCredentials(f.agent, f.state(), false)
	if err == nil || !strings.Contains(err.Error(), "reconnect") {
		t.Fatalf("split secrets = %v", err)
	}
	if len(f.puts) != 0 || f.exportsTaken != 0 {
		t.Fatal("a split enrollment was synced")
	}
}

func TestClaudeOAuthLocalReadFailureHonoursBackoff(t *testing.T) {
	f := newClaudeSyncFixture(t)
	reads := 0
	prev := readClaudeKeychainBlobForSync
	readClaudeKeychainBlobForSync = func() (string, error) {
		reads++
		return "", errors.New("keychain locked")
	}
	t.Cleanup(func() { readClaudeKeychainBlobForSync = prev })
	if _, err := syncClaudeOAuthCredentials(f.agent, f.state(), false); err == nil {
		t.Fatal("expected a read failure")
	}
	if _, err := syncClaudeOAuthCredentials(f.agent, f.state(), false); err != nil {
		t.Fatalf("back-off pass returned %v", err)
	}
	if reads != 1 || len(f.requestLog()) != 0 {
		t.Fatalf("reads=%d requests=%v", reads, f.requestLog())
	}
}

func TestSessionSubscriptionSyncHonoursDeadline(t *testing.T) {
	release := make(chan struct{})
	t.Cleanup(func() { close(release) })
	prev := syncClaudeOAuthFromHook
	syncClaudeOAuthFromHook = func() { <-release }
	t.Cleanup(func() { syncClaudeOAuthFromHook = prev })
	begin := time.Now()
	runSessionSubscriptionSync(permissionSourceClaudeCode, time.Now().Add(-(sessionSubscriptionSyncBudget - 1500*time.Millisecond)))
	if elapsed := time.Since(begin); elapsed > 3*time.Second {
		t.Fatalf("sync ignored the remaining budget: %s", elapsed)
	}
	begin = time.Now()
	runSessionSubscriptionSync(permissionSourceClaudeCode, time.Now().Add(-sessionSubscriptionSyncBudget))
	if time.Since(begin) > 100*time.Millisecond {
		t.Fatal("an exhausted budget still ran the sync")
	}
}

func TestClaudeOAuthSyncRecoversAfterSplitIsMerged(t *testing.T) {
	f := newClaudeSyncFixture(t)
	f.stampSynced(claudeSyncLocalExpires)
	f.splitSecrets = true
	state := f.state()
	state.ClaudeOAuthSyncModelIDs = []string{"model-sonnet", "model-opus"}
	_ = saveLocalEnrollmentState(state)
	if _, err := syncClaudeOAuthCredentials(f.agent, f.state(), false); err == nil {
		t.Fatal("expected a split refusal")
	}
	// Reconnect merged the rows onto one secret; the back-off has expired.
	f.splitSecrets = false
	state = f.state()
	state.ClaudeOAuthSyncLastAttempt = ""
	state.ClaudeOAuthServerCheckedAt = ""
	_ = saveLocalEnrollmentState(state)
	outcome, err := syncClaudeOAuthCredentials(f.agent, f.state(), false)
	if err != nil || !outcome.Unchanged {
		t.Fatalf("merged enrollment still refused: %+v, %v", outcome, err)
	}
	after := f.state()
	if len(after.ClaudeOAuthSyncModelIDs) != 1 || after.ClaudeOAuthServerCheckedAt == "" {
		t.Fatalf("cached ids not refreshed: %+v", after.ClaudeOAuthSyncModelIDs)
	}
}
