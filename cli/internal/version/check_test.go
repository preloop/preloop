package version

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/telemetry"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestTokenFingerprint(t *testing.T) {
	if got := tokenFingerprint(""); got != "" {
		t.Errorf("empty token must fingerprint to empty, got %q", got)
	}
	fp := tokenFingerprint("secret-token")
	if len(fp) != 16 {
		t.Errorf("expected 16 hex chars, got %d (%q)", len(fp), fp)
	}
	if fp == "secret-token"[:min(16, len("secret-token"))] {
		t.Error("fingerprint must not echo the token")
	}
	if tokenFingerprint("secret-token") != fp {
		t.Error("fingerprint must be deterministic")
	}
	if tokenFingerprint("other-token") == fp {
		t.Error("distinct tokens must fingerprint differently")
	}
}

func TestSameOrigin(t *testing.T) {
	cases := []struct {
		a, b string
		want bool
	}{
		{"https://preloop.ai", "https://preloop.ai", true},
		{"https://preloop.ai/", "https://preloop.ai", true},
		{"https://staging.preloop.ai", "https://preloop.ai", false},
		{"http://preloop.ai", "https://preloop.ai", false},
		{"https://preloop.ai:8443", "https://preloop.ai", false},
		{"", "https://preloop.ai", false},
	}
	for _, c := range cases {
		if got := sameOrigin(c.a, c.b); got != c.want {
			t.Errorf("sameOrigin(%q, %q) = %v, want %v", c.a, c.b, got, c.want)
		}
	}
}

func TestFetchVersionInfoPostsRichPayload(t *testing.T) {
	var gotPath, gotAuth string
	var gotBody map[string]any

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotAuth = r.Header.Get("Authorization")
		_ = json.NewDecoder(r.Body).Decode(&gotBody)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"latest_version": "9.9.9",
			"min_version":    "0.1.0",
			"download_url":   "https://preloop.ai/install/cli",
		})
	}))
	defer server.Close()

	oldOrigin := VersionCheckOrigin
	VersionCheckOrigin = server.URL
	defer func() { VersionCheckOrigin = oldOrigin }()

	// Isolate config dir so the test never touches the real ~/.preloop.
	testenv.SetHome(t, t.TempDir())

	info, err := fetchVersionInfo()
	if err != nil {
		t.Fatalf("fetchVersionInfo: %v", err)
	}
	if info.LatestVersion != "9.9.9" {
		t.Errorf("latest_version = %q", info.LatestVersion)
	}
	if gotPath != CliVersionCheckPath {
		t.Errorf("expected POST to %s, got %s", CliVersionCheckPath, gotPath)
	}
	// The payload must carry the install id + platform.
	if gotBody["client_id"] == nil || gotBody["client_id"] == "" {
		t.Error("payload missing client_id")
	}
	if gotBody["version"] != Version {
		t.Errorf("payload version = %v", gotBody["version"])
	}
	if gotBody["os"] == "" || gotBody["arch"] == "" {
		t.Error("payload missing os/arch")
	}
	// No config was written in the temp HOME → no token → no auth header,
	// and even with one, the origin differs from any configured APIURL.
	if gotAuth != "" {
		t.Errorf("unexpected Authorization header: %q", gotAuth)
	}
	// The raw token must never appear anywhere in the body.
	raw, _ := json.Marshal(gotBody)
	if strings.Contains(string(raw), "access_token") {
		t.Error("payload must not contain access_token")
	}
}

func TestFetchVersionInfoFallsBackToLegacyEndpoint(t *testing.T) {
	var paths []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.Method+" "+r.URL.Path)
		if r.URL.Path == CliVersionCheckPath {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"latest_version": "1.2.3",
		})
	}))
	defer server.Close()

	oldOrigin := VersionCheckOrigin
	VersionCheckOrigin = server.URL
	defer func() { VersionCheckOrigin = oldOrigin }()
	testenv.SetHome(t, t.TempDir())

	info, err := fetchVersionInfo()
	if err != nil {
		t.Fatalf("fetchVersionInfo with legacy fallback: %v", err)
	}
	if info.LatestVersion != "1.2.3" {
		t.Errorf("latest_version = %q", info.LatestVersion)
	}
	if len(paths) != 2 || !strings.HasSuffix(paths[1], LegacyVersionPath) {
		t.Errorf("expected rich attempt then legacy fallback, got %v", paths)
	}
}

func TestCheckForUpdateSkipsWhenTelemetryDisabled(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			requests++
			_ = json.NewEncoder(w).Encode(map[string]any{
				"latest_version": "9.9.9",
			})
		}))
	defer server.Close()

	oldOrigin := VersionCheckOrigin
	VersionCheckOrigin = server.URL
	defer func() { VersionCheckOrigin = oldOrigin }()

	// Fresh HOME: without the opt-out this would be a first run, which
	// checks in immediately — the strictest possible setting.
	testenv.SetHome(t, t.TempDir())
	t.Setenv(telemetry.DisableTelemetryEnv, "true")

	if err := CheckForUpdate(); err != nil {
		t.Fatalf("CheckForUpdate must be a silent no-op when disabled: %v", err)
	}
	if requests != 0 {
		t.Fatalf("expected no HTTP calls with telemetry disabled, got %d", requests)
	}
}

func TestForceCheckRefusesWhenTelemetryDisabled(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			requests++
		}))
	defer server.Close()

	oldOrigin := VersionCheckOrigin
	VersionCheckOrigin = server.URL
	defer func() { VersionCheckOrigin = oldOrigin }()

	testenv.SetHome(t, t.TempDir())
	t.Setenv(telemetry.DisableVersionCheckEnv, "yes")

	_, err := ForceCheck()
	if err == nil {
		t.Fatal("ForceCheck must return an error when telemetry is disabled")
	}
	if !strings.Contains(err.Error(), telemetry.DisableTelemetryEnv) {
		t.Errorf("error must name the env var %s, got: %v",
			telemetry.DisableTelemetryEnv, err)
	}
	if requests != 0 {
		t.Fatalf("expected no HTTP calls with telemetry disabled, got %d", requests)
	}
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
