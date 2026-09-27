package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
	"github.com/spf13/cobra"
)

const (
	codexPullLocalExpSeconds   = int64(1893456000)
	codexPullServerExpSeconds  = int64(1893500000)
	codexPullLocalLastRefresh  = "2026-09-18T11:43:27.789Z"
	codexPullServerLastRefresh = "2026-09-27T08:35:17Z"
	codexPullLocalRefresh      = "refresh-local-must-not-print"
	codexPullServerRefresh     = "refresh-server-must-not-print"
	codexPullIDToken           = "id-token-example"
)

// codexPullFixture is a Codex enrollment with a Codex-shaped auth.json and
// a fake Preloop API that serves one rotation marker and one export.
type codexPullFixture struct {
	t           *testing.T
	home        string
	codexDir    string
	authPath    string
	agent       AgentConfig
	localAccess string
	server      *httptest.Server

	mu            sync.Mutex
	requests      []string
	marker        map[string]interface{}
	markerStatus  int
	export        map[string]interface{}
	exportStatus  int
	serverAccess  string
	putBodies     []json.RawMessage
	exportsServed int
}

func newCodexPullFixture(t *testing.T) *codexPullFixture {
	t.Helper()
	silenceCodexKeychain(t)
	home := testenv.SetTempHome(t)
	codexDir := filepath.Join(home, ".codex")
	t.Setenv("CODEX_HOME", codexDir)
	f := &codexPullFixture{
		t:            t,
		home:         home,
		codexDir:     codexDir,
		agent:        codexSyncAgent(t, home),
		localAccess:  codexTestJWT(t, map[string]interface{}{"exp": codexPullLocalExpSeconds}),
		serverAccess: codexTestJWT(t, map[string]interface{}{"exp": codexPullServerExpSeconds, "sub": "server"}),
		markerStatus: http.StatusOK,
		exportStatus: http.StatusOK,
	}
	f.authPath = f.writeAuth(codexPullLocalLastRefresh)
	f.marker = map[string]interface{}{
		"credential_type":    openaiCodexOAuthCredentialType,
		"expires":            codexPullServerExpSeconds * 1000,
		"last_refresh":       codexPullServerLastRefresh,
		"credentials_status": "active",
		"account_id":         "acct-example",
	}
	f.export = map[string]interface{}{
		"credential_type": openaiCodexOAuthCredentialType,
		"access":          f.serverAccess,
		"refresh":         codexPullServerRefresh,
		"expires":         codexPullServerExpSeconds * 1000,
		"account_id":      "acct-example",
		"last_refresh":    codexPullServerLastRefresh,
	}
	f.server = httptest.NewServer(http.HandlerFunc(f.handle))
	t.Cleanup(f.server.Close)
	restore := setCodexSyncFlags(t, f.server.URL)
	t.Cleanup(restore)
	return f
}

func (f *codexPullFixture) writeAuth(lastRefresh string) string {
	f.t.Helper()
	if err := os.MkdirAll(f.codexDir, 0o700); err != nil {
		f.t.Fatal(err)
	}
	document := map[string]interface{}{
		"auth_mode":      "chatgpt",
		"OPENAI_API_KEY": nil,
		"tokens": map[string]interface{}{
			"id_token":      codexPullIDToken,
			"access_token":  f.localAccess,
			"refresh_token": codexPullLocalRefresh,
			"account_id":    "acct-example",
		},
		"last_refresh": lastRefresh,
	}
	data, err := json.MarshalIndent(document, "", "  ")
	if err != nil {
		f.t.Fatal(err)
	}
	path := filepath.Join(f.codexDir, "auth.json")
	if err := os.WriteFile(path, data, 0o600); err != nil {
		f.t.Fatal(err)
	}
	return path
}

func (f *codexPullFixture) handle(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.requests = append(f.requests, r.Method+" "+r.URL.Path)
	w.Header().Set("Content-Type", "application/json")
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
		_ = json.NewEncoder(w).Encode(managedAgentListResponse{
			Items: []managedAgentSummary{{
				ID:                "agent-codex-1",
				DisplayName:       "Codex CLI",
				SessionSourceType: "codex",
				SessionSourceID:   runtimePrincipalIDForAgent(f.agent),
				LifecycleState:    "active",
			}},
		})
	case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
		_ = json.NewEncoder(w).Encode([]aiModelResponse{
			codexSyncModel("model-alpha", "Example Alpha", "secret-shared", "agent-codex-1"),
			codexSyncModel("model-beta", "Example Beta", "secret-shared", "agent-codex-1"),
		})
	case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models/model-alpha/credentials/marker":
		if f.markerStatus != http.StatusOK {
			http.Error(w, `{"detail":"unavailable"}`, f.markerStatus)
			return
		}
		_ = json.NewEncoder(w).Encode(f.marker)
	case r.Method == http.MethodPost && r.URL.Path == "/api/v1/ai-models/model-alpha/credentials/export":
		if f.exportStatus != http.StatusOK {
			http.Error(w, `{"detail":"unavailable"}`, f.exportStatus)
			return
		}
		f.exportsServed++
		_ = json.NewEncoder(w).Encode(f.export)
	case r.Method == http.MethodPut && strings.HasPrefix(r.URL.Path, "/api/v1/ai-models/"):
		var body struct {
			CredentialPayload json.RawMessage `json:"credential_payload"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		f.putBodies = append(f.putBodies, append(json.RawMessage(nil), body.CredentialPayload...))
		_, _ = w.Write([]byte(`{}`))
	default:
		http.NotFound(w, r)
	}
}

func (f *codexPullFixture) requestLog() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.requests...)
}

// saveState writes a synced enrollment: the local stamp matches auth.json
// and Preloop's expiry at that sync was serverExpiresMS.
func (f *codexPullFixture) saveState(stamp string, serverExpiresMS int64, checkedAt string) *localEnrollmentState {
	f.t.Helper()
	info, err := os.Stat(f.authPath)
	if err != nil {
		f.t.Fatal(err)
	}
	state := &localEnrollmentState{
		AgentName:                       f.agent.Name,
		ConfigPath:                      f.agent.ConfigPath,
		CodexOAuthSyncedLastRefresh:     stamp,
		CodexOAuthSyncedAuthMtimeNS:     info.ModTime().UnixNano(),
		CodexOAuthSyncedServerExpiresMS: serverExpiresMS,
		CodexOAuthServerCheckedAt:       checkedAt,
	}
	if err := saveLocalEnrollmentState(state); err != nil {
		f.t.Fatal(err)
	}
	loaded, err := loadLocalEnrollmentState(f.agent)
	if err != nil {
		f.t.Fatal(err)
	}
	return loaded
}

func (f *codexPullFixture) reload() *localEnrollmentState {
	f.t.Helper()
	state, err := loadLocalEnrollmentState(f.agent)
	if err != nil {
		f.t.Fatal(err)
	}
	return state
}

func readJSONDocument(t *testing.T, data []byte) map[string]interface{} {
	t.Helper()
	var document map[string]interface{}
	if err := json.Unmarshal(data, &document); err != nil {
		t.Fatalf("decode %s: %v", data, err)
	}
	return document
}

func assertCodexShapedPulledDocument(t *testing.T, document map[string]interface{}, serverAccess string) {
	t.Helper()
	tokens, _ := document["tokens"].(map[string]interface{})
	if tokens["access_token"] != serverAccess {
		t.Fatal("tokens.access_token was not replaced with Preloop's access token")
	}
	if tokens["refresh_token"] != codexPullServerRefresh {
		t.Fatal("tokens.refresh_token was not replaced with Preloop's refresh token")
	}
	if tokens["id_token"] != codexPullIDToken {
		t.Fatalf("tokens.id_token not preserved: %v", tokens["id_token"])
	}
	if tokens["account_id"] != "acct-example" {
		t.Fatalf("tokens.account_id = %v", tokens["account_id"])
	}
	if document["auth_mode"] != "chatgpt" {
		t.Fatalf("auth_mode = %v", document["auth_mode"])
	}
	if value, ok := document["OPENAI_API_KEY"]; !ok || value != nil {
		t.Fatalf("OPENAI_API_KEY not preserved: %v %v", value, ok)
	}
	if document["last_refresh"] != codexPullServerLastRefresh {
		t.Fatalf("last_refresh = %v", document["last_refresh"])
	}
}

func TestCodexOAuthPullWritesAuthJSONWhenServerMarkerIsNewer(t *testing.T) {
	f := newCodexPullFixture(t)
	state := f.saveState(codexPullLocalLastRefresh, codexPullLocalExpSeconds*1000, "")

	outcome, err := syncCodexOAuthCredentials(f.agent, state, false)
	if err != nil {
		t.Fatal(err)
	}
	if outcome.Direction != codexOAuthDirectionPull || outcome.Conflict {
		t.Fatalf("outcome = %+v, want a pull without conflict", outcome)
	}
	if f.exportsServed != 1 || len(f.putBodies) != 0 {
		t.Fatalf("exports=%d puts=%d, want 1 and 0", f.exportsServed, len(f.putBodies))
	}
	data, err := os.ReadFile(f.authPath)
	if err != nil {
		t.Fatal(err)
	}
	assertCodexShapedPulledDocument(t, readJSONDocument(t, data), f.serverAccess)
	info, err := os.Stat(f.authPath)
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" && info.Mode().Perm() != 0o600 {
		t.Fatalf("auth.json mode = %v, want 0600", info.Mode().Perm())
	}
	leftovers, _ := filepath.Glob(filepath.Join(f.codexDir, ".auth-*.json"))
	if len(leftovers) != 0 {
		t.Fatalf("temp files left behind: %v", leftovers)
	}

	reloaded := f.reload()
	if reloaded.CodexOAuthSyncedLastRefresh != codexPullServerLastRefresh {
		t.Fatalf("stamp = %q", reloaded.CodexOAuthSyncedLastRefresh)
	}
	if reloaded.CodexOAuthSyncedServerExpiresMS != codexPullServerExpSeconds*1000 {
		t.Fatalf("server stamp = %d", reloaded.CodexOAuthSyncedServerExpiresMS)
	}
	if reloaded.CodexOAuthSyncedAuthMtimeNS != info.ModTime().UnixNano() {
		t.Fatal("stamp did not record the rewritten auth.json mtime")
	}
	if len(reloaded.CodexOAuthSyncModelIDs) != 1 || reloaded.CodexOAuthSyncModelIDs[0] != "model-alpha" {
		t.Fatalf("cached model ids = %v", reloaded.CodexOAuthSyncModelIDs)
	}

	// The pulled login is now the stamp: the next hook call is the
	// no-change path and opens no client.
	before := len(f.requestLog())
	second, err := syncCodexOAuthCredentials(f.agent, reloaded, false)
	if err != nil {
		t.Fatal(err)
	}
	if !second.Unchanged || len(f.requestLog()) != before {
		t.Fatalf("second sync outcome=%+v requests=%v", second, f.requestLog()[before:])
	}
}

func TestCodexOAuthPullNoOpWhenServerMarkerEqual(t *testing.T) {
	f := newCodexPullFixture(t)
	f.marker["expires"] = codexPullLocalExpSeconds * 1000
	before, err := os.ReadFile(f.authPath)
	if err != nil {
		t.Fatal(err)
	}
	// No server stamp: state written before the pull path existed. The
	// local access-token expiry is the reference and it is equal.
	state := f.saveState(codexPullLocalLastRefresh, 0, "")

	outcome, err := syncCodexOAuthCredentials(f.agent, state, false)
	if err != nil {
		t.Fatal(err)
	}
	if !outcome.Unchanged || outcome.Direction != "" {
		t.Fatalf("outcome = %+v, want unchanged", outcome)
	}
	if f.exportsServed != 0 || len(f.putBodies) != 0 {
		t.Fatalf("exports=%d puts=%d, want none", f.exportsServed, len(f.putBodies))
	}
	after, err := os.ReadFile(f.authPath)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(before, after) {
		t.Fatal("auth.json changed on a no-op")
	}
	reloaded := f.reload()
	if reloaded.CodexOAuthSyncedServerExpiresMS != codexPullLocalExpSeconds*1000 {
		t.Fatalf("server stamp = %d, want the marker's expiry", reloaded.CodexOAuthSyncedServerExpiresMS)
	}
	if reloaded.CodexOAuthSyncedLastRefresh != codexPullLocalLastRefresh {
		t.Fatalf("local stamp moved to %q", reloaded.CodexOAuthSyncedLastRefresh)
	}
	if _, ok := parseCodexOAuthRefreshTime(reloaded.CodexOAuthServerCheckedAt); !ok {
		t.Fatal("marker check time not recorded")
	}

	// Within the interval the hook makes no request.
	requestsBefore := len(f.requestLog())
	if _, err := syncCodexOAuthCredentials(f.agent, reloaded, false); err != nil {
		t.Fatal(err)
	}
	if len(f.requestLog()) != requestsBefore {
		t.Fatalf("check within the interval made requests: %v", f.requestLog()[requestsBefore:])
	}

	// After the interval the cached id makes the check a single marker read.
	reloaded.CodexOAuthServerCheckedAt = time.Now().Add(-3 * time.Minute).UTC().Format(time.RFC3339Nano)
	if err := saveLocalEnrollmentState(reloaded); err != nil {
		t.Fatal(err)
	}
	if _, err := syncCodexOAuthCredentials(f.agent, f.reload(), false); err != nil {
		t.Fatal(err)
	}
	later := f.requestLog()[requestsBefore:]
	if len(later) != 1 || later[0] != "GET /api/v1/ai-models/model-alpha/credentials/marker" {
		t.Fatalf("due check requests = %v, want one marker read", later)
	}
}

func TestCodexOAuthPushStillWinsWhenLocalIsNewer(t *testing.T) {
	f := newCodexPullFixture(t)
	f.marker["expires"] = int64(1700000000000)
	// The stamp is older than auth.json's last_refresh, Preloop has not
	// rotated since the stamp.
	state := f.saveState("2026-01-01T00:00:00Z", 1700000000000, "")
	state.CodexOAuthSyncedAuthMtimeNS = 1
	if err := saveLocalEnrollmentState(state); err != nil {
		t.Fatal(err)
	}

	outcome, err := syncCodexOAuthCredentials(f.agent, f.reload(), false)
	if err != nil {
		t.Fatal(err)
	}
	if outcome.Direction != codexOAuthDirectionPush || outcome.Conflict {
		t.Fatalf("outcome = %+v, want a push without conflict", outcome)
	}
	if f.exportsServed != 0 || len(f.putBodies) != 1 {
		t.Fatalf("exports=%d puts=%d, want 0 and 1", f.exportsServed, len(f.putBodies))
	}
	if !bytes.Contains(f.putBodies[0], []byte(codexPullLocalRefresh)) {
		t.Fatal("PUT did not carry the local bundle")
	}
	reloaded := f.reload()
	if reloaded.CodexOAuthSyncedLastRefresh != codexPullLocalLastRefresh {
		t.Fatalf("stamp = %q", reloaded.CodexOAuthSyncedLastRefresh)
	}
	if reloaded.CodexOAuthSyncedServerExpiresMS != codexPullLocalExpSeconds*1000 {
		t.Fatalf("server stamp = %d, want the pushed expiry", reloaded.CodexOAuthSyncedServerExpiresMS)
	}
}

func TestCodexOAuthConflictLaterLastRefreshWins(t *testing.T) {
	for _, tc := range []struct {
		name          string
		serverRefresh string
		wantDirection string
	}{
		{name: "local later", serverRefresh: "2026-09-10T00:00:00Z", wantDirection: codexOAuthDirectionPush},
		{name: "preloop later", serverRefresh: "2026-09-27T10:00:00Z", wantDirection: codexOAuthDirectionPull},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newCodexPullFixture(t)
			f.marker["last_refresh"] = tc.serverRefresh
			f.export["last_refresh"] = tc.serverRefresh
			// Both sides moved: auth.json's last_refresh is after the stamp
			// and Preloop's expiry is past the recorded one.
			state := f.saveState("2026-09-01T00:00:00Z", 1800000000000, "")
			state.CodexOAuthSyncedAuthMtimeNS = 1
			if err := saveLocalEnrollmentState(state); err != nil {
				t.Fatal(err)
			}

			outcome, err := syncCodexOAuthCredentials(f.agent, f.reload(), false)
			if err != nil {
				t.Fatal(err)
			}
			if outcome.Direction != tc.wantDirection || !outcome.Conflict {
				t.Fatalf("outcome = %+v, want %s with conflict", outcome, tc.wantDirection)
			}
			data, err := os.ReadFile(f.authPath)
			if err != nil {
				t.Fatal(err)
			}
			tokens, _ := readJSONDocument(t, data)["tokens"].(map[string]interface{})
			switch tc.wantDirection {
			case codexOAuthDirectionPush:
				if len(f.putBodies) != 1 || f.exportsServed != 0 {
					t.Fatalf("puts=%d exports=%d", len(f.putBodies), f.exportsServed)
				}
				if tokens["refresh_token"] != codexPullLocalRefresh {
					t.Fatal("the losing Preloop copy must not touch auth.json")
				}
			case codexOAuthDirectionPull:
				if len(f.putBodies) != 0 || f.exportsServed != 1 {
					t.Fatalf("puts=%d exports=%d", len(f.putBodies), f.exportsServed)
				}
				if tokens["refresh_token"] != codexPullServerRefresh {
					t.Fatal("the losing local copy was not overwritten")
				}
			}
			line := formatCodexOAuthSyncOutcome(outcome)
			if !strings.Contains(line, "Both copies changed since the last sync") {
				t.Fatalf("conflict not reported: %s", line)
			}
		})
	}
}

func TestCodexOAuthPullWritesKeychainWhenCodexUsesIt(t *testing.T) {
	f := newCodexPullFixture(t)
	if err := os.Remove(f.authPath); err != nil {
		t.Fatal(err)
	}
	keychainBlob := fmt.Sprintf(
		`{"auth_mode":"chatgpt","OPENAI_API_KEY":null,"tokens":{"id_token":%q,"access_token":%q,"refresh_token":%q,"account_id":"acct-example"},"last_refresh":%q}`,
		codexPullIDToken, f.localAccess, codexPullLocalRefresh, codexPullLocalLastRefresh,
	)
	prevRead := readCodexKeychainOAuthForSync
	prevBlob := readCodexKeychainBlobForSync
	prevWrite := writeCodexKeychainBlobForSync
	readCodexKeychainOAuthForSync = func() (*codexOAuthCredential, string) {
		cred := parseCodexOAuthCredentialBlob([]byte(keychainBlob), time.Now().Add(time.Hour).UnixMilli())
		return cred, codexOAuthLastRefreshFromJSON([]byte(keychainBlob))
	}
	readCodexKeychainBlobForSync = func() string { return keychainBlob }
	var written []string
	writeCodexKeychainBlobForSync = func(blob string) error {
		written = append(written, blob)
		keychainBlob = blob
		return nil
	}
	t.Cleanup(func() {
		readCodexKeychainOAuthForSync = prevRead
		readCodexKeychainBlobForSync = prevBlob
		writeCodexKeychainBlobForSync = prevWrite
	})
	state := &localEnrollmentState{
		AgentName:                       f.agent.Name,
		ConfigPath:                      f.agent.ConfigPath,
		CodexOAuthSyncedLastRefresh:     codexPullLocalLastRefresh,
		CodexOAuthSyncedServerExpiresMS: codexPullLocalExpSeconds * 1000,
	}
	if err := saveLocalEnrollmentState(state); err != nil {
		t.Fatal(err)
	}

	outcome, err := syncCodexOAuthCredentials(f.agent, f.reload(), false)
	if err != nil {
		t.Fatal(err)
	}
	if outcome.Direction != codexOAuthDirectionPull || outcome.Destination != "the macOS Keychain" {
		t.Fatalf("outcome = %+v", outcome)
	}
	if len(written) != 1 {
		t.Fatalf("Keychain writes = %d, want 1", len(written))
	}
	assertCodexShapedPulledDocument(t, readJSONDocument(t, []byte(written[0])), f.serverAccess)
	if _, err := os.Stat(f.authPath); !os.IsNotExist(err) {
		t.Fatalf("a Keychain pull must not create auth.json: %v", err)
	}
	reloaded := f.reload()
	if reloaded.CodexOAuthSyncedLastRefresh != codexPullServerLastRefresh {
		t.Fatalf("stamp = %q", reloaded.CodexOAuthSyncedLastRefresh)
	}
	// The Keychain now matches the stamp, so the next call is a no-op.
	second, err := syncCodexOAuthCredentials(f.agent, reloaded, false)
	if err != nil || !second.Unchanged || len(written) != 1 {
		t.Fatalf("second outcome=%+v err=%v writes=%d", second, err, len(written))
	}
}

func TestCodexOAuthPullFailuresLeaveFileAndStampUntouched(t *testing.T) {
	for _, tc := range []struct {
		name     string
		setup    func(f *codexPullFixture)
		wantLogs int
	}{
		{
			name:     "export fails",
			setup:    func(f *codexPullFixture) { f.exportStatus = http.StatusBadGateway },
			wantLogs: 1,
		},
		{
			name:     "export has no refresh token",
			setup:    func(f *codexPullFixture) { delete(f.export, "refresh") },
			wantLogs: 1,
		},
		{
			name:     "marker read fails",
			setup:    func(f *codexPullFixture) { f.markerStatus = http.StatusInternalServerError },
			wantLogs: 1,
		},
		{
			name: "auth.json cannot be replaced",
			setup: func(f *codexPullFixture) {
				if runtime.GOOS == "windows" {
					f.t.Skip("directory permissions differ on Windows")
				}
				if err := os.Chmod(f.codexDir, 0o500); err != nil {
					f.t.Fatal(err)
				}
				f.t.Cleanup(func() { _ = os.Chmod(f.codexDir, 0o700) })
			},
			wantLogs: 1,
		},
		{
			name:     "Preloop copy is in error",
			setup:    func(f *codexPullFixture) { f.marker["credentials_status"] = "error" },
			wantLogs: 0,
		},
		{
			name:     "Preloop copy is another ChatGPT account",
			setup:    func(f *codexPullFixture) { f.marker["account_id"] = "acct-other" },
			wantLogs: 0,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newCodexPullFixture(t)
			f.saveState(codexPullLocalLastRefresh, codexPullLocalExpSeconds*1000, "")
			before, err := os.ReadFile(f.authPath)
			if err != nil {
				t.Fatal(err)
			}
			beforeInfo, err := os.Stat(f.authPath)
			if err != nil {
				t.Fatal(err)
			}
			tc.setup(f)

			logs := 0
			var logged []string
			prevLog := logCodexOAuthSyncFailure
			logCodexOAuthSyncFailure = func(err error) {
				if err != nil {
					logs++
					logged = append(logged, err.Error())
				}
			}
			t.Cleanup(func() { logCodexOAuthSyncFailure = prevLog })

			// The hook entry point: never returns an error, logs once.
			maybeSyncCodexOAuthFromPermissionHook()

			if logs != tc.wantLogs {
				t.Fatalf("log calls = %d, want %d: %v", logs, tc.wantLogs, logged)
			}
			for _, line := range logged {
				if strings.Contains(line, codexPullServerRefresh) || strings.Contains(line, f.serverAccess) ||
					strings.Contains(line, codexPullLocalRefresh) {
					t.Fatalf("log line carries token material: %s", line)
				}
			}
			after, err := os.ReadFile(f.authPath)
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(before, after) {
				t.Fatal("auth.json changed after a failed or refused pull")
			}
			afterInfo, err := os.Stat(f.authPath)
			if err != nil {
				t.Fatal(err)
			}
			if !afterInfo.ModTime().Equal(beforeInfo.ModTime()) {
				t.Fatal("auth.json mtime moved")
			}
			reloaded := f.reload()
			if reloaded.CodexOAuthSyncedLastRefresh != codexPullLocalLastRefresh ||
				reloaded.CodexOAuthSyncedServerExpiresMS != codexPullLocalExpSeconds*1000 ||
				reloaded.CodexOAuthSyncedAuthMtimeNS != beforeInfo.ModTime().UnixNano() {
				t.Fatalf("stamp moved: %+v", reloaded)
			}
			if tc.wantLogs > 0 && strings.TrimSpace(reloaded.CodexOAuthSyncLastAttempt) == "" {
				t.Fatal("a failed pull must start the retry backoff")
			}
			if len(f.putBodies) != 0 {
				t.Fatalf("a pull-side failure pushed %d bundles", len(f.putBodies))
			}
		})
	}
}

func TestCodexOAuthPullSkipsSingleHolderHost(t *testing.T) {
	f := newCodexPullFixture(t)
	f.saveState(codexPullLocalLastRefresh, codexPullLocalExpSeconds*1000, "")
	if err := os.Remove(f.authPath); err != nil {
		t.Fatal(err)
	}
	calls := 0
	prev := newCodexOAuthSyncClient
	newCodexOAuthSyncClient = func() (*api.Client, error) {
		calls++
		return nil, fmt.Errorf("client must not be opened")
	}
	t.Cleanup(func() { newCodexOAuthSyncClient = prev })

	outcome, err := syncCodexOAuthCredentials(f.agent, f.reload(), false)
	if err != nil || !outcome.Unchanged || calls != 0 {
		t.Fatalf("outcome=%+v err=%v calls=%d", outcome, err, calls)
	}
	if _, err := os.Stat(f.authPath); !os.IsNotExist(err) {
		t.Fatal("a host without a local login must not get one written back")
	}
}

func TestSyncCredentialsCommandReportsBothDirections(t *testing.T) {
	t.Run("pull", func(t *testing.T) {
		f := newCodexPullFixture(t)
		f.saveState(codexPullLocalLastRefresh, codexPullLocalExpSeconds*1000, time.Now().UTC().Format(time.RFC3339Nano))
		cmd := &cobra.Command{}
		var out bytes.Buffer
		cmd.SetOut(&out)
		if err := runAgentsSyncCredentials(cmd, []string{"Codex CLI"}); err != nil {
			t.Fatal(err)
		}
		text := out.String()
		if !strings.Contains(text, "Pulled Preloop's newer Codex login from Example Alpha (model-alpha) into "+resolveCodexAuthWritePath()) {
			t.Fatalf("pull output = %q", text)
		}
		assertNoCodexTokenMaterial(t, f, text)
		if strings.Count(text, "\n") != 1 {
			t.Fatalf("expected one output line, got %q", text)
		}
		if len(f.putBodies) != 0 {
			t.Fatal("a pull must not push the stale local bundle")
		}
	})
	t.Run("push", func(t *testing.T) {
		f := newCodexPullFixture(t)
		f.marker["expires"] = codexPullLocalExpSeconds * 1000
		f.saveState(codexPullLocalLastRefresh, codexPullLocalExpSeconds*1000, time.Now().UTC().Format(time.RFC3339Nano))
		cmd := &cobra.Command{}
		var out bytes.Buffer
		cmd.SetOut(&out)
		if err := runAgentsSyncCredentials(cmd, []string{"Codex CLI"}); err != nil {
			t.Fatal(err)
		}
		text := out.String()
		if !strings.HasPrefix(text, "Pushed the local Codex login to Preloop. Updated 2 model row(s): Example Alpha (model-alpha), Example Beta (model-beta)") {
			t.Fatalf("push output = %q", text)
		}
		assertNoCodexTokenMaterial(t, f, text)
		if f.exportsServed != 0 {
			t.Fatal("a push must not export")
		}
	})
}

func assertNoCodexTokenMaterial(t *testing.T, f *codexPullFixture, text string) {
	t.Helper()
	for _, secret := range []string{f.localAccess, f.serverAccess, codexPullLocalRefresh, codexPullServerRefresh, codexPullIDToken} {
		if strings.Contains(text, secret) {
			t.Fatalf("output printed token material: %s", text)
		}
	}
}

func TestMergeCodexAuthDocumentKeepsCodexFields(t *testing.T) {
	existing := []byte(`{"auth_mode":"chatgpt","OPENAI_API_KEY":null,"tokens":{"id_token":"id-example","access_token":"old","refresh_token":"old-r","account_id":"acct-example"},"last_refresh":"2026-01-01T00:00:00Z","extra":{"kept":true}}`)
	data, err := mergeCodexAuthDocument(existing, exportedModelCredential{
		Access:    "new",
		Refresh:   "new-r",
		AccountID: "acct-example",
	}, "2026-09-27T08:35:17Z")
	if err != nil {
		t.Fatal(err)
	}
	document := readJSONDocument(t, data)
	tokens, _ := document["tokens"].(map[string]interface{})
	if tokens["access_token"] != "new" || tokens["refresh_token"] != "new-r" || tokens["id_token"] != "id-example" {
		t.Fatalf("tokens = %v", tokens)
	}
	extra, _ := document["extra"].(map[string]interface{})
	if document["auth_mode"] != "chatgpt" || extra["kept"] != true || document["last_refresh"] != "2026-09-27T08:35:17Z" {
		t.Fatalf("document = %v", document)
	}
}
