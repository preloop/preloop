package cmd

import (
	"bytes"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/config"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestResolveConfiguredAPIURLUsesEnvVariable(t *testing.T) {
	tempHome := t.TempDir()
	testenv.SetHome(t, tempHome)
	t.Setenv("PRELOOP_URL", "http://example.test/api/")

	originalFlagURL := FlagURL
	originalFlagToken := FlagToken
	FlagURL = ""
	FlagToken = ""
	t.Cleanup(func() {
		FlagURL = originalFlagURL
		FlagToken = originalFlagToken
	})

	baseURL, err := resolveConfiguredAPIURL()
	if err != nil {
		t.Fatalf("resolveConfiguredAPIURL returned error: %v", err)
	}
	if baseURL != "http://example.test/api" {
		t.Fatalf("expected PRELOOP_URL to be used, got %q", baseURL)
	}
}

func TestShouldUseHeadlessOAuthDetectsSSH(t *testing.T) {
	restore := snapshotLoginFlags()
	defer restore()

	t.Setenv("SSH_CONNECTION", "client host 123 22")

	if !shouldUseHeadlessOAuth() {
		t.Fatal("expected SSH sessions to use headless OAuth")
	}
}

func TestShouldUseHeadlessOAuthHonorsLoopbackFlag(t *testing.T) {
	restore := snapshotLoginFlags()
	defer restore()

	t.Setenv("SSH_CONNECTION", "client host 123 22")
	loginLoopback = true

	if shouldUseHeadlessOAuth() {
		t.Fatal("expected --loopback to override SSH headless detection")
	}
}

func TestRootIncludesLoginAlias(t *testing.T) {
	for _, command := range rootCmd.Commands() {
		if command.Name() == "login" {
			return
		}
	}

	t.Fatal("expected root command to include a login alias")
}

func TestRootIncludesSignupCommand(t *testing.T) {
	for _, command := range rootCmd.Commands() {
		if command.Name() == "signup" {
			return
		}
	}

	t.Fatal("expected root command to include a signup command")
}

func TestAuthIncludesSignupSubcommand(t *testing.T) {
	for _, command := range authCmd.Commands() {
		if command.Name() == "signup" {
			return
		}
	}

	t.Fatal("expected 'preloop auth' to include a signup subcommand")
}

func TestBuildAuthorizationURLAddsSignupFlagWhenSignupRequested(t *testing.T) {
	originalSignup := signupRequested
	t.Cleanup(func() { signupRequested = originalSignup })

	signupRequested = false
	loginURL := buildAuthorizationURL("https://example.test", "http://127.0.0.1:1234/cb", "abc")
	if strings.Contains(loginURL, "signup=1") {
		t.Fatalf("expected login URL not to contain signup=1, got %q", loginURL)
	}

	signupRequested = true
	signupURL := buildAuthorizationURL("https://example.test", "http://127.0.0.1:1234/cb", "abc")
	if !strings.Contains(signupURL, "signup=1") {
		t.Fatalf("expected signup URL to contain signup=1, got %q", signupURL)
	}
}

func TestBuildPostAuthRedirectURL(t *testing.T) {
	cases := map[string]string{
		"":                   "",
		"https://preloop.ai": "https://preloop.ai/console/agents?cli=connected",
		// Trailing slashes should be normalized.
		"https://preloop.ai/": "https://preloop.ai/console/agents?cli=connected",
	}
	for input, expected := range cases {
		if got := buildPostAuthRedirectURL(input); got != expected {
			t.Fatalf("buildPostAuthRedirectURL(%q) = %q, want %q", input, got, expected)
		}
	}
}

func TestHandleOAuthCallbackRedirectsToConsole(t *testing.T) {
	codeChan := make(chan string, 1)
	errChan := make(chan error, 1)

	req := httptest.NewRequest("GET", "/callback?code=the-code&state=expected-state", nil)
	rec := httptest.NewRecorder()

	handleOAuthCallback(rec, req, "expected-state", "https://preloop.ai/console/agents?cli=connected", codeChan, errChan)

	if rec.Code != http.StatusFound {
		t.Fatalf("expected 302 redirect, got %d", rec.Code)
	}
	location := rec.Header().Get("Location")
	if location != "https://preloop.ai/console/agents?cli=connected" {
		t.Fatalf("unexpected Location header: %q", location)
	}
	select {
	case got := <-codeChan:
		if got != "the-code" {
			t.Fatalf("expected callback to forward code, got %q", got)
		}
	default:
		t.Fatal("expected authorization code to be sent on the channel")
	}
}

func TestRunAuthStatusRefreshesStoredLoginBeforeFetchingUser(t *testing.T) {
	tempHome := t.TempDir()
	testenv.SetHome(t, tempHome)

	restore := snapshotLoginFlags()
	defer restore()

	requestedUserInfo := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/oauth/token":
			if err := r.ParseForm(); err != nil {
				t.Fatalf("failed to parse refresh form: %v", err)
			}
			if r.Form.Get("grant_type") != "refresh_token" {
				t.Fatalf("expected refresh_token grant, got %q", r.Form.Get("grant_type"))
			}
			if r.Form.Get("refresh_token") != "refresh-token" {
				t.Fatalf("expected stored refresh token, got %q", r.Form.Get("refresh_token"))
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"access_token":"fresh-token","refresh_token":"rotated-refresh"}`))
		case userInfoPath:
			requestedUserInfo = true
			if got := r.Header.Get("Authorization"); got != "Bearer fresh-token" {
				t.Fatalf("expected refreshed bearer token, got %q", got)
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"id":"u1","email":"user@example.com","name":"Test User","organization":"Acme"}`))
		default:
			t.Fatalf("unexpected path %q", r.URL.Path)
		}
	}))
	defer server.Close()

	if err := config.Save(&config.Config{
		AccessToken:  "expired-token",
		RefreshToken: "refresh-token",
		APIURL:       server.URL,
	}); err != nil {
		t.Fatalf("failed to save config: %v", err)
	}

	output := captureStdout(t, func() error {
		return runAuthStatus(authStatusCmd, nil)
	})

	if !requestedUserInfo {
		t.Fatal("expected auth status to fetch user info after refresh")
	}
	if !strings.Contains(output, "Authenticated") || !strings.Contains(output, "Test User") {
		t.Fatalf("expected authenticated output after refresh, got %q", output)
	}
}

func TestRunAuthStatusPrintsUnderlyingError(t *testing.T) {
	tempHome := t.TempDir()
	testenv.SetHome(t, tempHome)

	restore := snapshotLoginFlags()
	defer restore()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != userInfoPath {
			t.Fatalf("unexpected path %q", r.URL.Path)
		}
		w.WriteHeader(http.StatusBadGateway)
		_, _ = w.Write([]byte(`{"detail":"upstream unavailable"}`))
	}))
	defer server.Close()

	if err := config.Save(&config.Config{
		AccessToken: "access-token",
		APIURL:      server.URL,
	}); err != nil {
		t.Fatalf("failed to save config: %v", err)
	}

	output := captureStdout(t, func() error {
		return runAuthStatus(authStatusCmd, nil)
	})

	if !strings.Contains(output, "upstream unavailable") {
		t.Fatalf("expected auth status to print underlying error, got %q", output)
	}
}

func snapshotLoginFlags() func() {
	originalLoginToken := loginToken
	originalLoginHeadless := loginHeadless
	originalLoginLoopback := loginLoopback
	originalLoginCode := loginCode
	originalLoginForce := loginForce

	return func() {
		loginToken = originalLoginToken
		loginHeadless = originalLoginHeadless
		loginLoopback = originalLoginLoopback
		loginCode = originalLoginCode
		loginForce = originalLoginForce
	}
}

func captureStdout(t *testing.T, fn func() error) string {
	t.Helper()

	oldStdout := os.Stdout
	readPipe, writePipe, err := os.Pipe()
	if err != nil {
		t.Fatalf("failed to create stdout pipe: %v", err)
	}
	os.Stdout = writePipe
	defer func() {
		os.Stdout = oldStdout
	}()

	runErr := fn()
	_ = writePipe.Close()
	if runErr != nil {
		t.Fatalf("unexpected error: %v", runErr)
	}

	var output bytes.Buffer
	if _, err := io.Copy(&output, readPipe); err != nil {
		t.Fatalf("failed to read stdout: %v", err)
	}
	return output.String()
}

func TestMain(m *testing.M) {
	// Prevent tests from reading the developer's real config. This is the
	// package-wide backstop for any test that forgets to redirect the home
	// directory itself; testenv.SetProcessHome covers Windows too, where
	// setting HOME alone would leave lookups pointing at the real profile.
	tempHome, err := os.MkdirTemp("", "preloop-cli-tests-*")
	if err != nil {
		panic(err)
	}
	restoreHome, err := testenv.SetProcessHome(tempHome)
	if err != nil {
		panic(err)
	}

	// GitHub CLI CI sets this; GitLab OSS did not. Successful login posts
	// /api/v1/events/batch unless telemetry is off, which fails hermetic
	// httptest servers that only expect the user-info path.
	if err := os.Setenv("PRELOOP_DISABLE_TELEMETRY", "true"); err != nil {
		panic(err)
	}

	// Scrub ambient agent/model env vars so upstream-resolution tests see a
	// hermetic environment. When the test suite itself runs inside a managed
	// agent session (e.g. Preloop-managed Claude Code exporting
	// ANTHROPIC_BASE_URL=https://.../anthropic), resolvers like
	// parseClaudeManagedGatewayUpstream read os.Getenv and correctly refuse
	// to treat the managed gateway as an upstream — failing tests that
	// expect detection. Tests that need these vars set them via t.Setenv.
	scrubPrefixes := []string{"ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_"}
	scrubExact := []string{
		"AWS_BEARER_TOKEN_BEDROCK",
		"GEMINI_API_KEY",
		"GOOGLE_API_KEY",
		"OPENAI_API_KEY",
		"CLAUDECODE",
	}
	for _, entry := range os.Environ() {
		key, _, ok := strings.Cut(entry, "=")
		if !ok {
			continue
		}
		for _, prefix := range scrubPrefixes {
			if strings.HasPrefix(key, prefix) {
				_ = os.Unsetenv(key)
				break
			}
		}
	}
	for _, key := range scrubExact {
		_ = os.Unsetenv(key)
	}

	code := m.Run()

	restoreHome()
	_ = os.RemoveAll(tempHome)
	os.Exit(code)
}

// A valid existing login must short-circuit `preloop login` (the install
// script re-runs it on every install); --force must re-authenticate.
func TestRunAuthLoginSkipsWhenAlreadyAuthenticated(t *testing.T) {
	tempHome := t.TempDir()
	testenv.SetHome(t, tempHome)

	restore := snapshotLoginFlags()
	defer restore()
	loginToken = ""
	loginForce = false

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != userInfoPath {
			t.Fatalf("unexpected path %q (login should not start OAuth)", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"id":"u1","email":"user@example.com","name":"Test User","organization":"Acme"}`))
	}))
	defer server.Close()

	if err := config.Save(&config.Config{
		AccessToken: "valid-token",
		APIURL:      server.URL,
	}); err != nil {
		t.Fatalf("failed to save config: %v", err)
	}

	output := captureStdout(t, func() error {
		return runAuthLogin(authLoginCmd, nil)
	})
	if !strings.Contains(output, "Already logged in") {
		t.Fatalf("expected already-logged-in short circuit, got %q", output)
	}
	if !strings.Contains(output, "Test User") || !strings.Contains(output, "user@example.com") {
		t.Fatalf("expected identity in output, got %q", output)
	}
	if !strings.Contains(output, "--force") {
		t.Fatalf("expected --force hint, got %q", output)
	}
}

// An invalid/unverifiable stored login must NOT short-circuit: the login flow
// should proceed (here: headless OAuth against an unreachable server errors,
// proving we got past the pre-check).
func TestRunAuthLoginProceedsWhenStoredLoginInvalid(t *testing.T) {
	tempHome := t.TempDir()
	testenv.SetHome(t, tempHome)

	restore := snapshotLoginFlags()
	defer restore()
	loginToken = ""
	loginForce = false
	loginHeadless = true

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"detail":"invalid token"}`))
	}))
	server.Close() // immediately unreachable

	if err := config.Save(&config.Config{
		AccessToken: "stale-token",
		APIURL:      server.URL,
	}); err != nil {
		t.Fatalf("failed to save config: %v", err)
	}

	var loginErr error
	output := captureStdout(t, func() error {
		loginErr = runAuthLogin(authLoginCmd, nil)
		return nil
	})
	if strings.Contains(output, "Already logged in") {
		t.Fatalf("stale login must not short-circuit, got %q", output)
	}
	if loginErr == nil {
		t.Fatal("expected headless OAuth against a dead server to fail (proves pre-check passed through)")
	}
}

// --token login must ignore the pre-check entirely (unattended/CI flows).
func TestRunAuthLoginTokenBypassesPreCheck(t *testing.T) {
	tempHome := t.TempDir()
	testenv.SetHome(t, tempHome)

	restore := snapshotLoginFlags()
	defer restore()
	loginForce = false

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != userInfoPath {
			t.Fatalf("unexpected path %q", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"id":"u2","email":"new@example.com","name":"New User"}`))
	}))
	defer server.Close()

	if err := config.Save(&config.Config{
		AccessToken: "old-token",
		APIURL:      server.URL,
	}); err != nil {
		t.Fatalf("failed to save config: %v", err)
	}
	loginToken = "new-token"

	output := captureStdout(t, func() error {
		return runAuthLogin(authLoginCmd, nil)
	})
	if strings.Contains(output, "Already logged in") {
		t.Fatalf("--token must bypass the pre-check, got %q", output)
	}
	if !strings.Contains(output, "Authenticated successfully") {
		t.Fatalf("expected token login to complete, got %q", output)
	}

	cfg, err := config.Load()
	if err != nil {
		t.Fatalf("config load: %v", err)
	}
	if cfg.AccessToken != "new-token" {
		t.Fatalf("expected new token saved, got %q", cfg.AccessToken)
	}
}

// --force must skip the pre-check and start a real login even when the stored
// session is perfectly valid (switching accounts).
func TestRunAuthLoginForceIgnoresExistingSession(t *testing.T) {
	tempHome := t.TempDir()
	testenv.SetHome(t, tempHome)

	restore := snapshotLoginFlags()
	defer restore()
	loginToken = ""
	loginForce = true
	loginHeadless = true

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"id":"u1","email":"user@example.com","name":"Test User"}`))
	}))
	server.Close() // OAuth must fail; we only care that it was attempted

	if err := config.Save(&config.Config{
		AccessToken: "valid-token",
		APIURL:      server.URL,
	}); err != nil {
		t.Fatalf("failed to save config: %v", err)
	}

	var loginErr error
	output := captureStdout(t, func() error {
		loginErr = runAuthLogin(authLoginCmd, nil)
		return nil
	})
	if strings.Contains(output, "Already logged in") {
		t.Fatalf("--force must not short-circuit, got %q", output)
	}
	if loginErr == nil {
		t.Fatal("expected --force to attempt OAuth against the dead server")
	}
}

// The re-auth hint must name the command the user actually ran, so that
// `preloop auth login` does not tell them to run `preloop login --force`.
func TestPrintAlreadyLoggedInNamesTheInvokedCommand(t *testing.T) {
	for _, tt := range []struct {
		name     string
		cmd      *cobra.Command
		identity UserInfo
		wants    []string
		notWant  string
	}{
		{
			name:     "login",
			cmd:      loginCmd,
			identity: UserInfo{Name: "Test User", Email: "user@example.com", Organization: "Acme"},
			wants:    []string{"Test User (user@example.com)", "Org:     Acme", "'preloop login --force'"},
		},
		{
			name:     "auth login alias",
			cmd:      authLoginCmd,
			identity: UserInfo{Name: "Test User", Email: "user@example.com"},
			wants:    []string{"'preloop auth login --force'"},
			notWant:  "Org:",
		},
		{
			name:     "signup alias",
			cmd:      signupCmd,
			identity: UserInfo{Name: "Test User", Email: "user@example.com"},
			wants:    []string{"'preloop signup --force'"},
		},
		{
			// Accounts without a display name still get an unambiguous line.
			name:     "email only",
			cmd:      loginCmd,
			identity: UserInfo{Email: "user@example.com"},
			wants:    []string{"User:    user@example.com"},
			notWant:  "()",
		},
	} {
		t.Run(tt.name, func(t *testing.T) {
			var buf bytes.Buffer
			identity := tt.identity
			printAlreadyLoggedIn(&buf, tt.cmd, &identity)
			for _, want := range tt.wants {
				if !strings.Contains(buf.String(), want) {
					t.Fatalf("expected %q in %q", want, buf.String())
				}
			}
			if tt.notWant != "" && strings.Contains(buf.String(), tt.notWant) {
				t.Fatalf("did not expect %q in %q", tt.notWant, buf.String())
			}
		})
	}
}
