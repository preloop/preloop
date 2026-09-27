package cmd

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// copilotLaunchCapture is what the fake copilot binary saw.
type copilotLaunchCapture struct {
	args []string
	env  map[string]string
}

// runCopilotViaRoot executes `preloop <argv...>` through the real root
// command with a fake copilot binary on PATH, and returns what it received.
// os.Args is set as well so the launcher sees the same raw argv cobra does.
func runCopilotViaRoot(t *testing.T, argv ...string) (copilotLaunchCapture, error) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("fake copilot is a shell script")
	}
	testenv.SetTempHome(t)
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")

	binDir := t.TempDir()
	out := filepath.Join(t.TempDir(), "capture")
	script := "#!/bin/sh\n" +
		"for a in \"$@\"; do printf '%s\\n' \"$a\"; done > \"$COPILOT_FAKE_OUT.args\"\n" +
		"env | grep -E '^COPILOT_(PROVIDER_|MODEL=)' > \"$COPILOT_FAKE_OUT.env\"\n"
	if err := os.WriteFile(filepath.Join(binDir, copilotCommand), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+"/usr/bin:/bin")
	t.Setenv("COPILOT_FAKE_OUT", out)

	prevOSArgs := os.Args
	prevToken, prevURL, prevVerbose := FlagToken, FlagURL, verbose
	t.Cleanup(func() {
		os.Args = prevOSArgs
		FlagToken, FlagURL, verbose = prevToken, prevURL, prevVerbose
		rootCmd.SetArgs(nil)
	})
	FlagToken, FlagURL = "", ""
	os.Args = append([]string{"preloop"}, argv...)
	rootCmd.SetArgs(argv)

	err := Execute()

	capture := copilotLaunchCapture{env: map[string]string{}}
	if raw, readErr := os.ReadFile(out + ".args"); readErr == nil {
		if s := strings.TrimSuffix(string(raw), "\n"); s != "" {
			capture.args = strings.Split(s, "\n")
		}
	}
	if raw, readErr := os.ReadFile(out + ".env"); readErr == nil {
		for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
			if k, v, ok := strings.Cut(line, "="); ok {
				capture.env[k] = v
			}
		}
	}
	return capture, err
}

func TestCopilotLauncherHonorsGlobalFlagsBeforeSubcommand(t *testing.T) {
	got, err := runCopilotViaRoot(t,
		"--url", "https://flag.example.com", "--token", "flag-token",
		"copilot", "--model", "openai/gpt-5", "-p", "hi",
	)
	if err != nil {
		t.Fatalf("launch failed: %v", err)
	}
	if got.env[copilotEnvProviderKey] != "flag-token" {
		t.Fatalf("%s = %q, want the --token value", copilotEnvProviderKey, got.env[copilotEnvProviderKey])
	}
	if got.env[copilotEnvProviderURL] != "https://flag.example.com/openai/v1" {
		t.Fatalf("%s = %q, want the --url gateway", copilotEnvProviderURL, got.env[copilotEnvProviderURL])
	}
	if strings.Join(got.args, " ") != "-p hi" {
		t.Fatalf("copilot argv = %q, want only the args after 'copilot'", got.args)
	}
}

func TestCopilotLauncherDoesNotLeakTokenIntoCopilotArgv(t *testing.T) {
	t.Setenv("PRELOOP_TOKEN", "env-token")
	got, err := runCopilotViaRoot(t,
		"--token=flag-token", "-v", "--url=https://flag.example.com/",
		"copilot", "--model", "anthropic/claude-sonnet-4-5",
	)
	if err != nil {
		t.Fatalf("launch failed: %v", err)
	}
	for _, arg := range got.args {
		if strings.Contains(arg, "flag-token") || strings.HasPrefix(arg, "--url") || arg == "-v" {
			t.Fatalf("Preloop global flag forwarded to copilot argv: %q", got.args)
		}
	}
	if got.env[copilotEnvProviderKey] != "flag-token" {
		t.Fatalf("explicit --token should win over PRELOOP_TOKEN, got %q", got.env[copilotEnvProviderKey])
	}
	if got.env[copilotEnvProviderType] != copilotProviderAnthropic ||
		got.env[copilotEnvProviderURL] != "https://flag.example.com/anthropic" {
		t.Fatalf("anthropic env = %v", got.env)
	}
}

func TestCopilotLauncherPassesFlagsAfterSubcommandThrough(t *testing.T) {
	t.Setenv("PRELOOP_TOKEN", "env-token")
	t.Setenv("PRELOOP_URL", "https://env.example.com")
	got, err := runCopilotViaRoot(t,
		"copilot", "--model", "openai/gpt-5", "--url", "copilot-own-value", "copilot",
	)
	if err != nil {
		t.Fatalf("launch failed: %v", err)
	}
	if strings.Join(got.args, " ") != "--url copilot-own-value copilot" {
		t.Fatalf("copilot argv = %q, want args after 'copilot' untouched", got.args)
	}
	if got.env[copilotEnvProviderKey] != "env-token" ||
		got.env[copilotEnvProviderURL] != "https://env.example.com/openai/v1" {
		t.Fatalf("env = %v", got.env)
	}
}

func TestCopilotLauncherRejectsUnknownGlobalFlagWithoutLaunching(t *testing.T) {
	t.Setenv("PRELOOP_TOKEN", "env-token")
	got, err := runCopilotViaRoot(t, "--not-a-preloop-flag", "copilot", "--model", "openai/gpt-5")
	if err == nil {
		t.Fatal("expected an unknown flag error")
	}
	if len(got.env) != 0 || got.args != nil {
		t.Fatalf("copilot must not start on a flag error, captured %v %q", got.env, got.args)
	}
}

func TestSplitLeadingRootFlags(t *testing.T) {
	tests := []struct {
		name        string
		raw         []string
		wantLeading []string
		wantRest    []string
		wantOK      bool
	}{
		{
			name:        "flags with separate values",
			raw:         []string{"--url", "u", "--token", "t", "copilot", "-p", "hi"},
			wantLeading: []string{"--url", "u", "--token", "t"},
			wantRest:    []string{"-p", "hi"},
			wantOK:      true,
		},
		{
			name:        "flag value equal to the command name",
			raw:         []string{"--token", "copilot", "copilot", "x"},
			wantLeading: []string{"--token", "copilot"},
			wantRest:    []string{"x"},
			wantOK:      true,
		},
		{
			name:        "bool shorthand does not consume the command name",
			raw:         []string{"-v", "copilot"},
			wantLeading: []string{"-v"},
			wantRest:    []string{},
			wantOK:      true,
		},
		{
			name:        "no leading flags",
			raw:         []string{"copilot", "--url", "x"},
			wantLeading: []string{},
			wantRest:    []string{"--url", "x"},
			wantOK:      true,
		},
		{
			name:   "double dash before the command",
			raw:    []string{"--", "copilot"},
			wantOK: false,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var args []string
			if tt.wantOK {
				args = append(append([]string{}, tt.wantLeading...), tt.wantRest...)
			}
			leading, rest, ok := splitLeadingRootFlags(copilotCmd, tt.raw, args)
			if ok != tt.wantOK {
				t.Fatalf("ok = %v, want %v", ok, tt.wantOK)
			}
			if !ok {
				return
			}
			if strings.Join(leading, "|") != strings.Join(tt.wantLeading, "|") ||
				strings.Join(rest, "|") != strings.Join(tt.wantRest, "|") {
				t.Fatalf("got leading=%q rest=%q, want %q %q", leading, rest, tt.wantLeading, tt.wantRest)
			}
		})
	}
}

func TestSplitLeadingRootFlagsFallsBackWhenArgvDoesNotMatch(t *testing.T) {
	// A caller that set args directly (os.Args is something else) keeps the
	// args cobra passed, untouched.
	_, _, ok := splitLeadingRootFlags(copilotCmd, []string{"--token", "t", "copilot"}, []string{"--model", "m"})
	if ok {
		t.Fatal("expected no split when raw argv does not reproduce args")
	}
}

func TestApplyLeadingRootFlagsHelpBeforeSubcommand(t *testing.T) {
	prev := os.Args
	t.Cleanup(func() { os.Args = prev })
	os.Args = []string{"preloop", "--help", "copilot"}
	got, err := applyLeadingRootFlags(copilotCmd, []string{"--help"})
	if err != nil || strings.Join(got, " ") != "--help" {
		t.Fatalf("got %q, %v; want [--help], nil", got, err)
	}
}
