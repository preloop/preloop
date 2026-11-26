package cmd

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func healthTestServer(t *testing.T, status int, body string, seen *string) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*seen = r.URL.Path + "?" + r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
}

func setupHealthEnv(t *testing.T, serverURL string) {
	t.Helper()
	home := testenv.SetTempHome(t)
	t.Setenv(config.EnvProfile, "")
	t.Setenv(config.EnvAccount, "")
	t.Setenv(config.EnvToken, "")
	t.Setenv(config.EnvURL, "")
	writeProfileConfig(t, home, serverURL)
}

func TestAccountsHealthPrintsSignals(t *testing.T) {
	body := `{"account_id":"00000000-0000-0000-0000-00000000000a",
"since":"2026-01-01T00:00:00Z","until":"2026-01-02T00:00:00Z",
"failed_executions":[{"category":"model_auth","count":3,
"first_execution_id":"00000000-0000-0000-0000-0000000000e1"}],
"hollow_successes":{"zero_tool_calls":{"count":2,"sample_execution_ids":[]},
"zero_model_requests":{"count":1,"sample_execution_ids":[]}},
"route_problems":[{"method":"GET","route_template":"/api/v1/flows/{flow_id}",
"status_class":4,"count":9,"first_at":"x","last_at":"y"}],
"error_responses":[],
"empty_after_create":[{"method":"GET","route_template":"/api/v1/projects","count":1,
"first_at":"x","last_at":"y"}],
"gateway_upstream_errors":[{"provider":"openai","error_class":"upstream_overloaded",
"count":4,"first_at":"x","last_at":"y"}]}`
	var seen string
	server := healthTestServer(t, http.StatusOK, body, &seen)
	defer server.Close()
	setupHealthEnv(t, server.URL)

	out, err := runRoot(t, "--profile", "work", "accounts", "health", "00000000-0000-0000-0000-00000000000a", "--since", "6h")
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if !strings.HasPrefix(seen, "/api/v1/operator/accounts/00000000-0000-0000-0000-00000000000a/health?since=") {
		t.Fatalf("request = %s", seen)
	}
	for _, want := range []string{"model_auth", "GET /api/v1/flows/{flow_id}", "4xx", "upstream_overloaded", "zero model requests"} {
		if !strings.Contains(out, want) {
			t.Fatalf("output lacks %q:\n%s", want, out)
		}
	}
}

func TestAccountsHealthRefusedForNonOperators(t *testing.T) {
	var seen string
	server := healthTestServer(t, http.StatusForbidden, `{"detail":"Platform operator access required"}`, &seen)
	defer server.Close()
	setupHealthEnv(t, server.URL)

	_, err := runRoot(t, "--profile", "work", "accounts", "health", "00000000-0000-0000-0000-00000000000a")
	if err == nil || !strings.Contains(err.Error(), "platform operators only") {
		t.Fatalf("err = %v", err)
	}
}

func TestAccountsHealthLegacyServer(t *testing.T) {
	var seen string
	server := healthTestServer(t, http.StatusNotFound, `{"detail":"Not Found"}`, &seen)
	defer server.Close()
	setupHealthEnv(t, server.URL)
	_, err := runRoot(t, "--profile", "work", "accounts", "health", "00000000-0000-0000-0000-00000000000a")
	if err == nil || !strings.Contains(err.Error(), "upgrade the server") {
		t.Fatalf("expected compatibility guidance, got %v", err)
	}
}

func TestAccountsHealthSendsUntilAndCapsTheWindow(t *testing.T) {
	var seen string
	server := healthTestServer(t, http.StatusOK, `{"account_id":"a","hollow_successes":{}}`, &seen)
	defer server.Close()
	setupHealthEnv(t, server.URL)

	if _, err := runRoot(t, "--profile", "work", "accounts", "health", "a", "--since", "743h"); err != nil {
		t.Fatalf("err = %v", err)
	}
	if !strings.Contains(seen, "until=") {
		t.Fatalf("request lacks until: %s", seen)
	}
	_, err := runRoot(t, "--profile", "work", "accounts", "health", "a", "--since", "744h")
	if err == nil || !strings.Contains(err.Error(), "743h") {
		t.Fatalf("err = %v", err)
	}
	accountsHealthSince = 24 * time.Hour
}
