package telemetry

import (
	"os"
	"strings"
)

// Environment variables that disable all adoption telemetry from the CLI:
// the daily version check-in POST, conversion events, and local usage
// counters. They mirror the server-side opt-out honored by
// backend/preloop/services/instance_service.py so one variable silences
// both halves of the product (e.g. in test rigs and CI).
const (
	// DisableTelemetryEnv is the primary opt-out variable.
	DisableTelemetryEnv = "PRELOOP_DISABLE_TELEMETRY"

	// DisableVersionCheckEnv is accepted as a legacy alias for backwards
	// compatibility with documentation (same as the backend).
	DisableVersionCheckEnv = "DISABLE_VERSION_CHECK"
)

// Disabled reports whether telemetry is disabled via environment variables.
//
// Note: when telemetry is disabled the CLI performs no version check at all,
// so local update notifications are suppressed too — the notification is a
// by-product of the check-in response and there is no offline source for it.
// This is deliberate: the variable exists for scripted/test runs that must
// leave zero footprint in adoption data.
func Disabled() bool {
	return envTruthy(os.Getenv(DisableTelemetryEnv)) ||
		envTruthy(os.Getenv(DisableVersionCheckEnv))
}

// envTruthy matches the backend's truthy parsing spirit: true/1/t/yes,
// case-insensitive, surrounding whitespace ignored.
func envTruthy(value string) bool {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "true", "1", "t", "yes":
		return true
	}
	return false
}
