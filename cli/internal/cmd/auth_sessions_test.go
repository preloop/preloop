package cmd

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/testenv"
	"github.com/preloop/preloop/cli/internal/version"
)

// setFlagURL points the global --url flag at a test server and restores it.
func setFlagURL(t *testing.T, value string) {
	t.Helper()
	original := FlagURL
	originalToken := FlagToken
	FlagURL = value
	FlagToken = ""
	t.Cleanup(func() {
		FlagURL = original
		FlagToken = originalToken
	})
}

func saveOAuthLogin(t *testing.T, apiURL, refreshToken string) {
	t.Helper()
	if err := config.Save(&config.Config{
		AccessToken:  "access-token",
		RefreshToken: refreshToken,
		APIURL:       apiURL,
	}); err != nil {
		t.Fatalf("failed to save config: %v", err)
	}
}

func TestRunAuthLogoutRevokesThisLoginOnTheServer(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	restore := snapshotLoginFlags()
	defer restore()
	logoutAll = false

	var revokedToken, hint string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/oauth/revoke" {
			t.Fatalf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		if err := r.ParseForm(); err != nil {
			t.Fatalf("failed to parse revoke form: %v", err)
		}
		revokedToken = r.Form.Get("token")
		hint = r.Form.Get("token_type_hint")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"revoked"}`))
	}))
	defer server.Close()
	setFlagURL(t, server.URL)
	saveOAuthLogin(t, server.URL, "refresh-token")

	output := captureStdout(t, func() error {
		return runAuthLogout(authLogoutCmd, nil)
	})

	if revokedToken != "refresh-token" || hint != "refresh_token" {
		t.Fatalf("expected the stored refresh token to be revoked, got %q (%q)", revokedToken, hint)
	}
	if config.IsAuthenticated() {
		t.Fatal("expected local credentials to be cleared")
	}
	if !strings.Contains(output, "Revoked this login on the server") {
		t.Fatalf("expected server revocation message, got %q", output)
	}
}

func TestRunAuthLogoutLegacyLoginPointsToAll(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	restore := snapshotLoginFlags()
	defer restore()
	logoutAll = false

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error":"unsupported_token_type","error_description":"no session"}`))
	}))
	defer server.Close()
	setFlagURL(t, server.URL)
	saveOAuthLogin(t, server.URL, "legacy-refresh-token")

	output := captureStdout(t, func() error {
		return runAuthLogout(authLogoutCmd, nil)
	})

	if config.IsAuthenticated() {
		t.Fatal("expected local credentials to be cleared")
	}
	if !strings.Contains(output, "cannot be revoked on its own") {
		t.Fatalf("expected legacy login warning, got %q", output)
	}
	if strings.Contains(output, "Revoked this login") {
		t.Fatalf("must not claim a revocation that did not happen, got %q", output)
	}
}

func TestRunAuthLogoutOfflineWarnsAndStillClears(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	restore := snapshotLoginFlags()
	defer restore()
	logoutAll = false
	setFlagURL(t, "http://127.0.0.1:1")
	saveOAuthLogin(t, "http://127.0.0.1:1", "refresh-token")

	output := captureStdout(t, func() error {
		return runAuthLogout(authLogoutCmd, nil)
	})

	if config.IsAuthenticated() {
		t.Fatal("expected local credentials to be cleared when offline")
	}
	if !strings.Contains(output, "Could not revoke this login on the server") {
		t.Fatalf("expected offline warning, got %q", output)
	}
}

func TestRunAuthLogoutWithAPIKeyDoesNotCallTheServer(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	restore := snapshotLoginFlags()
	defer restore()
	logoutAll = false

	called := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
	}))
	defer server.Close()
	setFlagURL(t, server.URL)
	saveOAuthLogin(t, server.URL, "")

	output := captureStdout(t, func() error {
		return runAuthLogout(authLogoutCmd, nil)
	})

	if called {
		t.Fatal("an API key login has no session to revoke; expected no request")
	}
	if !strings.Contains(output, "Successfully logged out") {
		t.Fatalf("expected logout confirmation, got %q", output)
	}
}

func TestExchangeCodeForTokensSendsDeviceName(t *testing.T) {
	var deviceName string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := r.ParseForm(); err != nil {
			t.Fatalf("failed to parse token form: %v", err)
		}
		deviceName = r.Form.Get("device_name")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"access_token":"a","refresh_token":"r"}`))
	}))
	defer server.Close()

	if _, err := exchangeCodeForTokens(server.URL, "code", "http://127.0.0.1/cb"); err != nil {
		t.Fatalf("exchangeCodeForTokens: %v", err)
	}
	if deviceName != version.DeviceName() {
		t.Fatalf("expected device_name %q, got %q", version.DeviceName(), deviceName)
	}
}

func TestRunAuthSessionsListMarksThisLogin(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/api/v1/auth/sessions/cli" {
			t.Fatalf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`[
			{"id":"11111111-1111-1111-1111-111111111111","created_at":"2026-09-01T10:00:00","last_seen_at":"2026-09-27T08:00:00+00:00","user_agent":"preloop-cli/0.17.0","hostname":"laptop","current":true},
			{"id":"22222222-2222-2222-2222-222222222222","created_at":"2026-09-02T10:00:00","last_seen_at":null,"user_agent":null,"hostname":null,"current":false}
		]`))
	}))
	defer server.Close()
	setFlagURL(t, server.URL)
	saveOAuthLogin(t, server.URL, "refresh-token")

	output := captureStdout(t, func() error {
		return runAuthSessionsList(authSessionsListCmd, nil)
	})

	for _, want := range []string{
		"11111111-1111-1111-1111-111111111111 (this login)",
		"laptop",
		"2026-09-27T08:00:00Z",
		"22222222-2222-2222-2222-222222222222",
	} {
		if !strings.Contains(output, want) {
			t.Fatalf("expected %q in %q", want, output)
		}
	}
	if strings.Count(output, "(this login)") != 1 {
		t.Fatalf("expected exactly one current login, got %q", output)
	}
}

func TestRunAuthSessionsRevokeDeletesOneSession(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	var deleted string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			t.Fatalf("expected DELETE, got %s", r.Method)
		}
		deleted = r.URL.Path
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	setFlagURL(t, server.URL)
	saveOAuthLogin(t, server.URL, "refresh-token")

	output := captureStdout(t, func() error {
		return runAuthSessionsRevoke(authSessionsRevokeCmd, []string{"22222222-2222-2222-2222-222222222222"})
	})

	if deleted != "/api/v1/auth/sessions/cli/22222222-2222-2222-2222-222222222222" {
		t.Fatalf("unexpected DELETE path %q", deleted)
	}
	if !strings.Contains(output, "Revoked CLI session") {
		t.Fatalf("expected confirmation, got %q", output)
	}
}

func TestRunAuthSessionsRevokeReportsUnknownSession(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"detail":"CLI session not found"}`))
	}))
	defer server.Close()
	setFlagURL(t, server.URL)
	saveOAuthLogin(t, server.URL, "")

	err := runAuthSessionsRevoke(authSessionsRevokeCmd, []string{"missing"})
	if err == nil || !strings.Contains(err.Error(), "no active CLI session missing") {
		t.Fatalf("expected not-found error, got %v", err)
	}
}
