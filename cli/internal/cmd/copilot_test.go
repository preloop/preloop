package cmd

import (
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestBuildCopilotProviderEnvOpenAI(t *testing.T) {
	env, err := buildCopilotProviderEnv(
		"https://preloop.example.com",
		"agt_durable_token",
		"openai/gpt-5",
		"",
	)
	if err != nil {
		t.Fatalf("buildCopilotProviderEnv: %v", err)
	}
	if got := env[copilotEnvProviderType]; got != "openai" {
		t.Fatalf("COPILOT_PROVIDER_TYPE = %q, want openai", got)
	}
	if got := env[copilotEnvProviderURL]; got != "https://preloop.example.com/openai/v1" {
		t.Fatalf("COPILOT_PROVIDER_BASE_URL = %q", got)
	}
	if got := env[copilotEnvProviderKey]; got != "agt_durable_token" {
		t.Fatalf("COPILOT_PROVIDER_API_KEY = %q", got)
	}
	if got := env[copilotEnvModel]; got != "openai/gpt-5" {
		t.Fatalf("COPILOT_MODEL = %q", got)
	}
}

func TestBuildCopilotProviderEnvAnthropicFromAlias(t *testing.T) {
	env, err := buildCopilotProviderEnv(
		"https://preloop.example.com/",
		"agt_durable_token",
		"preloop/anthropic/claude-sonnet-4-5",
		"",
	)
	if err != nil {
		t.Fatalf("buildCopilotProviderEnv: %v", err)
	}
	if got := env[copilotEnvProviderType]; got != "anthropic" {
		t.Fatalf("COPILOT_PROVIDER_TYPE = %q, want anthropic", got)
	}
	if got := env[copilotEnvProviderURL]; got != "https://preloop.example.com/anthropic" {
		t.Fatalf("COPILOT_PROVIDER_BASE_URL = %q", got)
	}
	if got := env[copilotEnvModel]; got != "preloop/anthropic/claude-sonnet-4-5" {
		t.Fatalf("COPILOT_MODEL = %q", got)
	}
}

func TestBuildCopilotProviderEnvAnthropicFromFlag(t *testing.T) {
	env, err := buildCopilotProviderEnv(
		"https://preloop.example.com",
		"agt_durable_token",
		"openai/gpt-5", // alias alone would be openai; flag wins
		"anthropic",
	)
	if err != nil {
		t.Fatalf("buildCopilotProviderEnv: %v", err)
	}
	if got := env[copilotEnvProviderType]; got != "anthropic" {
		t.Fatalf("COPILOT_PROVIDER_TYPE = %q, want anthropic", got)
	}
	if got := env[copilotEnvProviderURL]; got != "https://preloop.example.com/anthropic" {
		t.Fatalf("COPILOT_PROVIDER_BASE_URL = %q", got)
	}
}

func TestBuildCopilotProviderEnvMissingKeyAndModel(t *testing.T) {
	_, err := buildCopilotProviderEnv("https://preloop.example.com", "", "openai/gpt-5", "")
	if !errors.Is(err, errCopilotCredentialMissing) {
		t.Fatalf("expected errCopilotCredentialMissing, got %v", err)
	}
	_, err = buildCopilotProviderEnv("https://preloop.example.com", "agt_x", "", "")
	if !errors.Is(err, errCopilotModelMissing) {
		t.Fatalf("expected errCopilotModelMissing, got %v", err)
	}
}

func TestBuildCopilotProviderEnvInvalidProvider(t *testing.T) {
	_, err := buildCopilotProviderEnv(
		"https://preloop.example.com",
		"agt_x",
		"openai/gpt-5",
		"azure",
	)
	if err == nil || !strings.Contains(err.Error(), "--provider") {
		t.Fatalf("expected --provider error, got %v", err)
	}
}

func TestParseCopilotArgs(t *testing.T) {
	opts, err := parseCopilotArgs([]string{
		"--model", "openai/gpt-5",
		"--provider=anthropic",
		"--",
		"--help",
		"extra",
	})
	if err != nil {
		t.Fatalf("parseCopilotArgs: %v", err)
	}
	if opts.model != "openai/gpt-5" || opts.provider != "anthropic" {
		t.Fatalf("opts = %+v", opts)
	}
	if strings.Join(opts.args, " ") != "--help extra" {
		t.Fatalf("passthrough = %q", opts.args)
	}
}

func TestPrepareCopilotLaunchMissingBinary(t *testing.T) {
	testenv.SetTempHome(t)
	t.Setenv("PATH", t.TempDir())

	prevLookup := lookupEnrolledCopilotModelAlias
	prevCred := resolveCopilotCredential
	prevURL := resolveCopilotBaseURL
	t.Cleanup(func() {
		lookupEnrolledCopilotModelAlias = prevLookup
		resolveCopilotCredential = prevCred
		resolveCopilotBaseURL = prevURL
	})
	lookupEnrolledCopilotModelAlias = func() (string, error) { return "", nil }
	resolveCopilotCredential = func() (string, error) { return "agt_x", nil }
	resolveCopilotBaseURL = func() (string, error) { return "https://preloop.example.com", nil }

	_, err := prepareCopilotLaunch(copilotOptions{model: "openai/gpt-5"})
	if !errors.Is(err, errCopilotBinaryMissing) {
		t.Fatalf("expected errCopilotBinaryMissing, got %v", err)
	}
	if !strings.Contains(err.Error(), copilotInstallHint) {
		t.Fatalf("error should name install hint, got %v", err)
	}
}

func TestPrepareCopilotLaunchMissingCredential(t *testing.T) {
	testenv.SetTempHome(t)
	binDir := installFakeCopilot(t)

	prevLookup := lookupEnrolledCopilotModelAlias
	prevCred := resolveCopilotCredential
	prevURL := resolveCopilotBaseURL
	t.Cleanup(func() {
		lookupEnrolledCopilotModelAlias = prevLookup
		resolveCopilotCredential = prevCred
		resolveCopilotBaseURL = prevURL
	})
	lookupEnrolledCopilotModelAlias = func() (string, error) { return "", nil }
	resolveCopilotCredential = func() (string, error) { return "", nil }
	resolveCopilotBaseURL = func() (string, error) { return "https://preloop.example.com", nil }

	_, err := prepareCopilotLaunch(copilotOptions{model: "openai/gpt-5"})
	if !errors.Is(err, errCopilotCredentialMissing) {
		t.Fatalf("expected errCopilotCredentialMissing, got %v", err)
	}
	_ = binDir
}

func TestPrepareCopilotLaunchMissingModel(t *testing.T) {
	testenv.SetTempHome(t)
	_ = installFakeCopilot(t)

	prevLookup := lookupEnrolledCopilotModelAlias
	prevCred := resolveCopilotCredential
	prevURL := resolveCopilotBaseURL
	t.Cleanup(func() {
		lookupEnrolledCopilotModelAlias = prevLookup
		resolveCopilotCredential = prevCred
		resolveCopilotBaseURL = prevURL
	})
	lookupEnrolledCopilotModelAlias = func() (string, error) { return "", nil }
	resolveCopilotCredential = func() (string, error) { return "agt_x", nil }
	resolveCopilotBaseURL = func() (string, error) { return "https://preloop.example.com", nil }

	_, err := prepareCopilotLaunch(copilotOptions{})
	if !errors.Is(err, errCopilotModelMissing) {
		t.Fatalf("expected errCopilotModelMissing, got %v", err)
	}
}

func TestPrepareCopilotLaunchBuildsEnvWithoutStartingProcess(t *testing.T) {
	testenv.SetTempHome(t)
	binDir := installFakeCopilot(t)

	prevLookup := lookupEnrolledCopilotModelAlias
	prevCred := resolveCopilotCredential
	prevURL := resolveCopilotBaseURL
	t.Cleanup(func() {
		lookupEnrolledCopilotModelAlias = prevLookup
		resolveCopilotCredential = prevCred
		resolveCopilotBaseURL = prevURL
	})
	lookupEnrolledCopilotModelAlias = func() (string, error) {
		return "anthropic/claude-opus-4-6", nil
	}
	resolveCopilotCredential = func() (string, error) { return "agt_enrolled", nil }
	resolveCopilotBaseURL = func() (string, error) {
		return "https://gateway.example.com", nil
	}

	launch, err := prepareCopilotLaunch(copilotOptions{args: []string{"-p", "hi"}})
	if err != nil {
		t.Fatalf("prepareCopilotLaunch: %v", err)
	}
	wantName := "copilot"
	if runtime.GOOS == "windows" {
		// exec.LookPath appends a PATHEXT suffix; the stub is copilot.exe.
		wantName = "copilot.exe"
	}
	wantBin := filepath.Join(binDir, wantName)
	if launch.Bin != wantBin {
		t.Fatalf("bin = %q, want %q", launch.Bin, wantBin)
	}
	if strings.Join(launch.Args, " ") != "-p hi" {
		t.Fatalf("args = %q", launch.Args)
	}
	if launch.Env[copilotEnvProviderType] != "anthropic" {
		t.Fatalf("type = %q", launch.Env[copilotEnvProviderType])
	}
	if launch.Env[copilotEnvProviderURL] != "https://gateway.example.com/anthropic" {
		t.Fatalf("url = %q", launch.Env[copilotEnvProviderURL])
	}
	if launch.Env[copilotEnvProviderKey] != "agt_enrolled" {
		t.Fatalf("key = %q", launch.Env[copilotEnvProviderKey])
	}
	if launch.Env[copilotEnvModel] != "anthropic/claude-opus-4-6" {
		t.Fatalf("model = %q", launch.Env[copilotEnvModel])
	}
}

func TestMergeCopilotEnvOverridesExisting(t *testing.T) {
	merged := mergeCopilotEnv(
		[]string{
			"PATH=/bin",
			"COPILOT_PROVIDER_TYPE=openai",
			"COPILOT_MODEL=old",
			"OTHER=1",
		},
		map[string]string{
			copilotEnvProviderType: "anthropic",
			copilotEnvProviderURL:  "https://preloop.example.com/anthropic",
			copilotEnvProviderKey:  "agt_x",
			copilotEnvModel:        "anthropic/claude-sonnet-4-5",
		},
	)
	joined := strings.Join(merged, "\n")
	if strings.Count(joined, "COPILOT_PROVIDER_TYPE=") != 1 {
		t.Fatalf("expected one PROVIDER_TYPE, got:\n%s", joined)
	}
	if !strings.Contains(joined, "COPILOT_PROVIDER_TYPE=anthropic") {
		t.Fatalf("missing anthropic type:\n%s", joined)
	}
	if strings.Contains(joined, "COPILOT_MODEL=old") {
		t.Fatalf("old model should be replaced:\n%s", joined)
	}
	if !strings.Contains(joined, "OTHER=1") || !strings.Contains(joined, "PATH=/bin") {
		t.Fatalf("unrelated env lost:\n%s", joined)
	}
}

func TestSelectCopilotAPIKeyPrefersExplicitToken(t *testing.T) {
	got := selectCopilotAPIKey("flag-token", "hook-token", "login-token")
	if got != "flag-token" {
		t.Fatalf("explicit token = %q", got)
	}
	got = selectCopilotAPIKey("", "hook-token", "login-token")
	if got != "hook-token" {
		t.Fatalf("hook token = %q", got)
	}
	got = selectCopilotAPIKey("  ", "", "login-token")
	if got != "login-token" {
		t.Fatalf("login token = %q", got)
	}
}

func TestIsCopilotCLIManagedAgent(t *testing.T) {
	if !isCopilotCLIManagedAgent(managedAgentSummary{DisplayName: "Copilot CLI"}) {
		t.Fatal("exact name should match")
	}
	if isCopilotCLIManagedAgent(managedAgentSummary{DisplayName: "VSCode / Copilot"}) {
		t.Fatal("IDE Copilot must not match the CLI launcher")
	}
}

// installFakeCopilot puts a non-executed stub named "copilot" on PATH so
// findCopilot succeeds. Tests must not start a real Copilot process.
func installFakeCopilot(t *testing.T) string {
	t.Helper()
	binDir := filepath.Join(t.TempDir(), "bin")
	if err := os.MkdirAll(binDir, 0o755); err != nil {
		t.Fatal(err)
	}
	name := "copilot"
	if runtime.GOOS == "windows" {
		// LookPath only matches PATHEXT (.exe, .bat, .cmd) on Windows.
		name = "copilot.exe"
	}
	path := filepath.Join(binDir, name)
	// Not a runnable script on purpose: prepareCopilotLaunch must succeed
	// without exec'ing this file.
	if err := os.WriteFile(path, []byte("#!/bin/sh\necho should-not-run\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", binDir)
	return binDir
}
