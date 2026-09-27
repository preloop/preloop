package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/testenv"
)

var gatedGroups = []string{"subaccounts", "share", "tags", "access"}

// withFeatures makes the server report features for the test.
func withFeatures(t *testing.T, features map[string]any) {
	t.Helper()
	previous := featuresFetcher
	featuresFetcher = func() (map[string]any, error) { return features, nil }
	resetCapabilityCache()
	t.Cleanup(func() {
		featuresFetcher = previous
		resetCapabilityCache()
		for _, cmd := range rootCmd.Commands() {
			if _, gated := cmd.Annotations[capabilityAnnotation]; gated {
				cmd.Hidden = true
			}
		}
	})
}

// runRoot runs the root command with args and returns stdout and the error.
func runRoot(t *testing.T, args ...string) (string, error) {
	t.Helper()
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	var out, errOut bytes.Buffer
	rootCmd.SetOut(&out)
	rootCmd.SetErr(&errOut)
	rootCmd.SetArgs(args)
	t.Cleanup(func() {
		rootCmd.SetArgs(nil)
		rootCmd.SetOut(nil)
		rootCmd.SetErr(nil)
		FlagProfile, FlagAccount, FlagToken, FlagURL = "", "", "", ""
		for _, name := range []string{"profile", "account", "token", "url"} {
			if flag := rootCmd.PersistentFlags().Lookup(name); flag != nil {
				flag.Changed = false
			}
		}
		config.Select("", "")
	})
	err := rootCmd.Execute()
	return out.String(), err
}

// availableCommands returns the command names listed by root help.
func availableCommands(help string) map[string]bool {
	names := map[string]bool{}
	_, section, ok := strings.Cut(help, "Available Commands:")
	if !ok {
		return names
	}
	section, _, _ = strings.Cut(section, "\n\n")
	for _, line := range strings.Split(section, "\n") {
		if fields := strings.Fields(line); len(fields) > 0 {
			names[fields[0]] = true
		}
	}
	return names
}

func TestGatedCommandsHiddenFromHelpWhenCapabilityOff(t *testing.T) {
	testenv.SetTempHome(t)
	withFeatures(t, map[string]any{"multi_account": false})

	help, err := runRoot(t, "--help")
	if err != nil {
		t.Fatal(err)
	}
	listed := availableCommands(help)
	if !listed["accounts"] || !listed["auth"] {
		t.Fatalf("help did not list the ungated commands:\n%s", help)
	}
	for _, name := range gatedGroups {
		if listed[name] {
			t.Errorf("%q listed in help with its capability off", name)
		}
	}
}

func TestGatedCommandsShownPerCapability(t *testing.T) {
	testenv.SetTempHome(t)
	withFeatures(t, map[string]any{"account_hierarchy": true})

	help, err := runRoot(t, "--help")
	if err != nil {
		t.Fatal(err)
	}
	listed := availableCommands(help)
	for name, want := range map[string]bool{"subaccounts": true, "share": true, "tags": false, "access": false} {
		if listed[name] != want {
			t.Errorf("%q listed = %v with only account_hierarchy on, want %v", name, listed[name], want)
		}
	}

	withFeatures(t, map[string]any{"abac_rules": true})
	help, err = runRoot(t, "--help")
	if err != nil {
		t.Fatal(err)
	}
	listed = availableCommands(help)
	for name, want := range map[string]bool{"subaccounts": false, "share": false, "tags": true, "access": true} {
		if listed[name] != want {
			t.Errorf("%q listed = %v with only abac_rules on, want %v", name, listed[name], want)
		}
	}
}

func TestGatedCommandRefusedWhenCapabilityOff(t *testing.T) {
	testenv.SetTempHome(t)
	var hits int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()
	withFeatures(t, map[string]any{})

	_, err := runRoot(t, "--url", server.URL, "--token", "t", "subaccounts", "list")
	if err == nil || !strings.Contains(err.Error(), "not available on this server") {
		t.Fatalf("err = %v, want a capability-off error", err)
	}
	if hits != 0 {
		t.Fatalf("the command reached the server %d times with its capability off", hits)
	}
}

// fakeAccountsServer serves memberships and the account switch.
type fakeAccountsServer struct {
	mu         sync.Mutex
	switchAuth []string
	subAuth    []string
}

func (f *fakeAccountsServer) handler(t *testing.T) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		switch r.URL.Path {
		case "/api/v1/me/memberships":
			_ = json.NewEncoder(w).Encode([]map[string]any{
				{"account_id": "acc-root", "account_name": "Root", "slug": "root", "is_current": true},
				{"account_id": "acc-acme", "account_name": "Acme", "slug": "acme", "parent_account_id": "acc-root"},
			})
		case "/api/v1/auth/switch-account":
			f.switchAuth = append(f.switchAuth, r.Header.Get("Authorization"))
			var body map[string]string
			_ = json.NewDecoder(r.Body).Decode(&body)
			if body["account_id"] != "acc-acme" {
				t.Errorf("switch body = %v", body)
			}
			_ = json.NewEncoder(w).Encode(map[string]string{
				"access_token": "acme-access", "refresh_token": "acme-refresh",
			})
		case "/api/v1/accounts/acc-acme/subaccounts":
			f.subAuth = append(f.subAuth, r.Header.Get("Authorization"))
			_ = json.NewEncoder(w).Encode(map[string]any{"items": []any{}})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	})
}

func writeProfileConfig(t *testing.T, home, serverURL string) string {
	t.Helper()
	dir := filepath.Join(home, config.ConfigDir)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, config.ConfigFile)
	body := "access_token: default-access\nrefresh_token: \"\"\napi_url: https://unused.invalid\n" +
		"profiles:\n  work:\n    api_url: " + serverURL + "\n    access_token: work-access\n"
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestAccountsSwitchPersistsPerProfile(t *testing.T) {
	home := testenv.SetTempHome(t)
	t.Setenv(config.EnvProfile, "")
	t.Setenv(config.EnvAccount, "")
	t.Setenv(config.EnvToken, "")
	t.Setenv(config.EnvURL, "")
	fake := &fakeAccountsServer{}
	server := httptest.NewServer(fake.handler(t))
	defer server.Close()
	writeProfileConfig(t, home, server.URL)

	out, err := runRoot(t, "--profile", "work", "accounts", "switch", "acme")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out, "Switched to Acme (acme)") {
		t.Fatalf("output = %q", out)
	}
	if len(fake.switchAuth) != 1 || fake.switchAuth[0] != "Bearer work-access" {
		t.Fatalf("switch used %v, want the work profile's session", fake.switchAuth)
	}

	// The work profile now uses acme's pair; the default profile does not.
	config.Select("work", "")
	work, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	if work.Account != "acme" || work.AccessToken != "acme-access" || work.RefreshToken != "acme-refresh" || work.AccountID != "acc-acme" {
		t.Fatalf("work profile after switch = %+v", work)
	}
	config.Select("", "")
	def, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	if def.Account != "" || def.AccessToken != "default-access" {
		t.Fatalf("default profile changed by the switch: %+v", def)
	}

	// No request after the switch carries the old token.
	withFeatures(t, map[string]any{"account_hierarchy": true})
	if _, err := runRoot(t, "--profile", "work", "subaccounts", "list"); err != nil {
		t.Fatal(err)
	}
	if len(fake.subAuth) != 1 || fake.subAuth[0] != "Bearer acme-access" {
		t.Fatalf("request after the switch used %v, want only the acme token", fake.subAuth)
	}

	current, err := runRoot(t, "--profile", "work", "accounts", "current")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(current, "Profile: work") || !strings.Contains(current, "Account: Acme (acme)") {
		t.Fatalf("accounts current = %q", current)
	}
}

func TestAccountFlagWithoutStoredSessionIsRefused(t *testing.T) {
	home := testenv.SetTempHome(t)
	t.Setenv(config.EnvProfile, "")
	t.Setenv(config.EnvAccount, "")
	t.Setenv(config.EnvToken, "")
	t.Setenv(config.EnvURL, "")
	var auth []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth = append(auth, r.Header.Get("Authorization"))
		_ = json.NewEncoder(w).Encode([]any{})
	}))
	defer server.Close()
	writeProfileConfig(t, home, server.URL)

	_, err := runRoot(t, "--profile", "work", "--account", "other", "accounts", "list")
	if err == nil || !strings.Contains(err.Error(), "accounts switch other") {
		t.Fatalf("err = %v, want a pointer to accounts switch", err)
	}
	if len(auth) != 0 {
		t.Fatalf("a request went out with %v for an account without a session", auth)
	}
}

func TestSubaccountNotFoundDoesNotLeak(t *testing.T) {
	home := testenv.SetTempHome(t)
	t.Setenv(config.EnvProfile, "")
	t.Setenv(config.EnvAccount, "")
	t.Setenv(config.EnvToken, "")
	t.Setenv(config.EnvURL, "")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/auth/users/me" {
			_ = json.NewEncoder(w).Encode(map[string]string{"account_id": "acc-root"})
			return
		}
		// A sibling account's subaccount id: the server answers 404.
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"detail":"Not found"}`))
	}))
	defer server.Close()
	writeProfileConfig(t, home, server.URL)
	withFeatures(t, map[string]any{"account_hierarchy": true})

	_, err := runRoot(t, "--profile", "work", "subaccounts", "rename", "sibling-sub", "x")
	if err == nil || err.Error() != "subaccount sibling-sub not found in this account" {
		t.Fatalf("err = %v", err)
	}
}

func TestParseTagPairs(t *testing.T) {
	tags, err := parseTagPairs([]string{"env=prod", "team/owner=platform"})
	if err != nil || tags["env"] != "prod" || tags["team/owner"] != "platform" {
		t.Fatalf("tags = %v, err = %v", tags, err)
	}
	for _, bad := range []string{"noequals", "=v", "Env=prod", "k=" + strings.Repeat("x", 129)} {
		if _, err := parseTagPairs([]string{bad}); err == nil {
			t.Errorf("parseTagPairs(%q) accepted", bad)
		}
	}
}

func TestAuthStatusShowsProfileAndAccount(t *testing.T) {
	home := testenv.SetTempHome(t)
	t.Setenv(config.EnvProfile, "")
	t.Setenv(config.EnvAccount, "")
	t.Setenv(config.EnvToken, "")
	t.Setenv(config.EnvURL, "")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == userInfoPath && r.Header.Get("Authorization") == "Bearer acme-access" {
			_ = json.NewEncoder(w).Encode(map[string]string{"name": "Ada", "email": "ada@example.com"})
			return
		}
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer server.Close()
	path := filepath.Join(home, config.ConfigDir, config.ConfigFile)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	body := "current_profile: work\nprofiles:\n  work:\n    api_url: " + server.URL +
		"\n    current_account: acme\n    accounts:\n      acme:\n        access_token: acme-access\n        account_id: acc-acme\n        name: Acme\n"
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	config.Select("", "")
	t.Cleanup(func() { config.Select("", "") })

	out := captureCommandStdout(t, func() error { return runAuthStatus(authStatusCmd, nil) })
	for _, want := range []string{"User:    Ada", "Profile: work", "Account: Acme (acme)"} {
		if !strings.Contains(out, want) {
			t.Errorf("auth status missing %q:\n%s", want, out)
		}
	}
}

// tagServer serves one resource's tags at version v1 and records writes.
type tagServer struct {
	mu       sync.Mutex
	puts     []map[string]any
	conflict bool
	shares   []map[string]any
}

func (s *tagServer) handler(t *testing.T) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s.mu.Lock()
		defer s.mu.Unlock()
		switch {
		case r.URL.Path == "/api/v1/auth/users/me":
			_ = json.NewEncoder(w).Encode(map[string]string{"account_id": "acc-root"})
		case r.URL.Path == "/api/v1/tags/ai_model/m1" && r.Method == http.MethodGet:
			_ = json.NewEncoder(w).Encode(map[string]any{
				"tags":          map[string]string{"env": "prod"},
				"governed_keys": []string{"customer"},
				"version":       "v1",
			})
		case r.URL.Path == "/api/v1/tags/ai_model/m1" && r.Method == http.MethodPut:
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			s.puts = append(s.puts, body)
			if s.conflict {
				w.WriteHeader(http.StatusConflict)
				_, _ = w.Write([]byte(`{"detail":"version mismatch"}`))
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"tags": body["tags"], "version": "v2"})
		case r.URL.Path == "/api/v1/accounts/acc-root/shares" && r.Method == http.MethodPost:
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			s.shares = append(s.shares, body)
			_ = json.NewEncoder(w).Encode(map[string]any{"id": "sh-1"})
		default:
			t.Errorf("unexpected %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	})
}

func setupTagServer(t *testing.T, fake *tagServer) {
	t.Helper()
	home := testenv.SetTempHome(t)
	for _, env := range []string{config.EnvProfile, config.EnvAccount, config.EnvToken, config.EnvURL} {
		t.Setenv(env, "")
	}
	server := httptest.NewServer(fake.handler(t))
	t.Cleanup(server.Close)
	writeProfileConfig(t, home, server.URL)
	withFeatures(t, map[string]any{"account_hierarchy": true, "abac_rules": true})
}

func TestTagsSetSendsTheVersionItRead(t *testing.T) {
	fake := &tagServer{}
	setupTagServer(t, fake)

	if _, err := runRoot(t, "--profile", "work", "tags", "set", "ai_model", "m1", "tier=gold"); err != nil {
		t.Fatal(err)
	}
	if len(fake.puts) != 1 {
		t.Fatalf("puts = %v", fake.puts)
	}
	if fake.puts[0]["version"] != "v1" {
		t.Fatalf("version sent = %v", fake.puts[0]["version"])
	}
	tags := fake.puts[0]["tags"].(map[string]any)
	if tags["env"] != "prod" || tags["tier"] != "gold" {
		t.Fatalf("tags sent = %v", tags)
	}
}

func TestTagsSetReportsAConcurrentChange(t *testing.T) {
	fake := &tagServer{conflict: true}
	setupTagServer(t, fake)

	_, err := runRoot(t, "--profile", "work", "tags", "set", "ai_model", "m1", "tier=gold")
	if err == nil || !strings.Contains(err.Error(), "changed while this command ran") {
		t.Fatalf("err = %v", err)
	}
}

func TestShareAddSendsNoID(t *testing.T) {
	fake := &tagServer{}
	setupTagServer(t, fake)

	if _, err := runRoot(t, "--profile", "work", "share", "add", "ai_model", "m1"); err != nil {
		t.Fatal(err)
	}
	if len(fake.shares) != 1 {
		t.Fatalf("shares = %v", fake.shares)
	}
	if _, ok := fake.shares[0]["id"]; ok {
		t.Fatalf("POST /shares carried an id: %v", fake.shares[0])
	}
	if fake.shares[0]["resource_type"] != "ai_model" || fake.shares[0]["resource_id"] != "m1" {
		t.Fatalf("body = %v", fake.shares[0])
	}
}
