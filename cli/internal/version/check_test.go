package version

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
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

	info, err := fetchVersionInfo("11111111-2222-4333-8444-555555555555", false)
	if err != nil {
		t.Fatalf("fetchVersionInfo: %v", err)
	}
	if info.LatestVersion != "9.9.9" {
		t.Errorf("latest_version = %q", info.LatestVersion)
	}
	if gotPath != CliVersionCheckPath {
		t.Errorf("expected POST to %s, got %s", CliVersionCheckPath, gotPath)
	}
	// The payload must carry the threaded install id + platform.
	if gotBody["client_id"] != "11111111-2222-4333-8444-555555555555" {
		t.Errorf("payload client_id = %v", gotBody["client_id"])
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

	info, err := fetchVersionInfo("", false)
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

func TestBuildCheckInPayloadFirstRunFlag(t *testing.T) {
	testenv.SetHome(t, t.TempDir())

	payload, _ := buildCheckInPayload("some-id", true)
	if payload.ClientID != "some-id" {
		t.Errorf("client_id = %q, want the threaded id", payload.ClientID)
	}
	if payload.Metadata["first_run"] != true {
		t.Errorf("first_run must be true when threaded in, got %v", payload.Metadata)
	}

	payload, _ = buildCheckInPayload("some-id", false)
	if _, present := payload.Metadata["first_run"]; present {
		t.Errorf("first_run must be absent on later runs, got %v", payload.Metadata)
	}
}

func TestCheckForUpdateThreadsFirstRunIntoPayload(t *testing.T) {
	// Regression: CheckForUpdate used to create the client id, then payload
	// construction re-derived it via a second GetOrCreateClientIDWithNew
	// call — which found the id already on disk and reported first_run=false
	// on the very first run. The flag must be threaded from the single call
	// that actually created the id.
	var bodies []map[string]any
	server := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			bodies = append(bodies, body)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"latest_version": Version, // no update prompt noise
			})
		}))
	defer server.Close()

	oldOrigin := VersionCheckOrigin
	VersionCheckOrigin = server.URL
	defer func() { VersionCheckOrigin = oldOrigin }()

	// Fresh HOME: no client id yet — this run is THE first run. Test runs
	// set PRELOOP_DISABLE_TELEMETRY=true globally (so tests never pollute
	// adoption data); neutralize it here scoped to this test, because the
	// check-in against the local httptest server IS the behavior under test.
	testenv.SetHome(t, t.TempDir())
	t.Setenv(telemetry.DisableTelemetryEnv, "")
	t.Setenv(telemetry.DisableVersionCheckEnv, "")

	if err := CheckForUpdate(); err != nil {
		t.Fatalf("first CheckForUpdate: %v", err)
	}
	if len(bodies) != 1 {
		t.Fatalf("expected 1 check-in, got %d", len(bodies))
	}
	metadata, _ := bodies[0]["metadata"].(map[string]any)
	if metadata == nil || metadata["first_run"] != true {
		t.Fatalf("very first check-in must carry first_run=true, got %v", bodies[0])
	}
	firstClientID, _ := bodies[0]["client_id"].(string)
	if firstClientID == "" {
		t.Fatal("first check-in must carry the freshly created client_id")
	}

	// Force a second check with the SAME install: drop only the last-check
	// timestamp, keeping the client id.
	lastCheckPath, err := getLastCheckPath()
	if err != nil {
		t.Fatalf("getLastCheckPath: %v", err)
	}
	if err := os.Remove(lastCheckPath); err != nil {
		t.Fatalf("removing last-check file: %v", err)
	}

	if err := CheckForUpdate(); err != nil {
		t.Fatalf("second CheckForUpdate: %v", err)
	}
	if len(bodies) != 2 {
		t.Fatalf("expected 2 check-ins, got %d", len(bodies))
	}
	if metadata, _ := bodies[1]["metadata"].(map[string]any); metadata != nil {
		if _, present := metadata["first_run"]; present {
			t.Errorf("second check-in must not carry first_run, got %v", metadata)
		}
	}
	if secondClientID, _ := bodies[1]["client_id"].(string); secondClientID != firstClientID {
		t.Errorf("client_id changed between runs: %q vs %q",
			firstClientID, secondClientID)
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
