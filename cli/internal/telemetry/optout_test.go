package telemetry

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// clearOptOutEnv removes both opt-out variables for the duration of a test
// (t.Setenv registers cleanup restoring the original values).
func clearOptOutEnv(t *testing.T) {
	t.Helper()
	t.Setenv(DisableTelemetryEnv, "")
	t.Setenv(DisableVersionCheckEnv, "")
}

func TestDisabledEnvParsing(t *testing.T) {
	cases := []struct {
		name  string
		env   string
		value string
		want  bool
	}{
		{"unset", "", "", false},
		{"true", DisableTelemetryEnv, "true", true},
		{"TRUE", DisableTelemetryEnv, "TRUE", true},
		{"True", DisableTelemetryEnv, "True", true},
		{"one", DisableTelemetryEnv, "1", true},
		{"t", DisableTelemetryEnv, "t", true},
		{"T", DisableTelemetryEnv, "T", true},
		{"yes", DisableTelemetryEnv, "yes", true},
		{"YES", DisableTelemetryEnv, "YES", true},
		{"whitespace-padded", DisableTelemetryEnv, "  true  ", true},
		{"false", DisableTelemetryEnv, "false", false},
		{"zero", DisableTelemetryEnv, "0", false},
		{"no", DisableTelemetryEnv, "no", false},
		{"empty", DisableTelemetryEnv, "", false},
		{"garbage", DisableTelemetryEnv, "yeah-nah", false},
		{"legacy-true", DisableVersionCheckEnv, "true", true},
		{"legacy-1", DisableVersionCheckEnv, "1", true},
		{"legacy-yes", DisableVersionCheckEnv, "YES", true},
		{"legacy-false", DisableVersionCheckEnv, "false", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			clearOptOutEnv(t)
			if c.env != "" {
				t.Setenv(c.env, c.value)
			}
			if got := Disabled(); got != c.want {
				t.Errorf("Disabled() with %s=%q = %v, want %v",
					c.env, c.value, got, c.want)
			}
		})
	}
}

func TestSendConversionSkippedWhenTelemetryDisabled(t *testing.T) {
	clearOptOutEnv(t)
	testenv.SetHome(t, t.TempDir())

	requests := 0
	server := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			requests++
		}))
	defer server.Close()

	t.Setenv(DisableTelemetryEnv, "true")
	SendConversion(server.URL, "token", "0.0.0-test", "Login")
	if requests != 0 {
		t.Fatalf("expected no HTTP calls with telemetry disabled, got %d", requests)
	}

	// Sanity: with the variable unset the same call does reach the server.
	t.Setenv(DisableTelemetryEnv, "")
	SendConversion(server.URL, "token", "0.0.0-test", "Login")
	if requests != 1 {
		t.Fatalf("expected exactly 1 HTTP call with telemetry enabled, got %d", requests)
	}
}

func TestIncrementSkippedWhenTelemetryDisabled(t *testing.T) {
	clearOptOutEnv(t)
	testenv.SetHome(t, t.TempDir())

	t.Setenv(DisableTelemetryEnv, "1")
	Increment("agents")
	if counters := Load(); len(counters) != 0 {
		t.Fatalf("expected no counters recorded with telemetry disabled, got %v", counters)
	}
}
