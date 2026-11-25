package cmd

import (
	"bytes"
	"strings"
	"testing"
)

func TestParseHarnessVersionHarnessOutputs(t *testing.T) {
	for _, tc := range []struct{ name, output, want string }{
		{"codex", "codex-cli 0.171.0\n", "0.171.0"},
		{"opencode", "1.18.32\n", "1.18.32"},
		{"gemini", "0.24.1\n", "0.24.1"},
		{"claude", "2.1.296 (Claude Code)\n", "2.1.296"},
		{"prerelease", "codex-cli 0.172.0-alpha.3\n", "0.172.0-alpha.3"},
		{"no version", "usage: codex\n", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := parseHarnessVersion(tc.output); got != tc.want {
				t.Fatalf("parseHarnessVersion(%q) = %q, want %q", tc.output, got, tc.want)
			}
		})
	}
}

func TestBuildHarnessInventoryExtendedHarnesses(t *testing.T) {
	const (
		codexLoginStep    = "run `codex login` as the runner user"
		codexApprovalStep = "run `preloop agents onboard \"Codex CLI\" --approvals` as the runner user"
	)
	for _, tc := range []struct {
		name          string
		harness       string
		agent         AgentConfig
		installed     bool
		approved      bool
		env           map[string]string
		handwritten   []hostExecProfile
		wantSupport   string
		wantProfile   string
		wantStep      string
		wantLogin     string
		wantGoverance string
	}{
		{
			name:          "codex ready gets generated profile",
			harness:       "codex_cli",
			agent:         AgentConfig{Name: "Codex CLI", RuntimeState: "present", AuthState: "ready", IsOnboarded: true, OnboardingState: "fully_onboarded"},
			installed:     true,
			approved:      true,
			wantSupport:   "flows_only",
			wantProfile:   "codex",
			wantLogin:     "signed_in",
			wantGoverance: "governed",
		},
		{
			name:          "codex without approval hook names the onboarding step",
			harness:       "codex_cli",
			agent:         AgentConfig{Name: "Codex CLI", RuntimeState: "present", AuthState: "ready", IsOnboarded: true, OnboardingState: "fully_onboarded"},
			installed:     true,
			wantSupport:   "presence_only",
			wantStep:      codexApprovalStep,
			wantLogin:     "signed_in",
			wantGoverance: "partial",
		},
		{
			name:          "codex signed out names the login step first",
			harness:       "codex_cli",
			agent:         AgentConfig{Name: "Codex CLI", RuntimeState: "present", AuthState: "not_logged_in"},
			installed:     true,
			wantSupport:   "presence_only",
			wantStep:      codexLoginStep,
			wantLogin:     "signed_out",
			wantGoverance: "ungoverned",
		},
		{
			name:          "codex api key variable counts as signed in",
			harness:       "codex_cli",
			agent:         AgentConfig{Name: "Codex CLI", RuntimeState: "present", AuthState: "unknown"},
			installed:     true,
			approved:      true,
			env:           map[string]string{"CODEX_API_KEY": "x"},
			wantSupport:   "flows_only",
			wantProfile:   "codex",
			wantLogin:     "signed_in",
			wantGoverance: "governed",
		},
		{
			name:          "codex discovered but not on PATH",
			harness:       "codex_cli",
			agent:         AgentConfig{Name: "Codex CLI", RuntimeState: "present", AuthState: "ready"},
			approved:      true,
			wantSupport:   "presence_only",
			wantStep:      "install Codex CLI on the runner user's PATH, then `preloop runner restart`",
			wantLogin:     "signed_in",
			wantGoverance: "governed",
		},
		{
			name:          "handwritten codex profile keeps flows and suppresses generated",
			harness:       "codex_cli",
			agent:         AgentConfig{Name: "Codex CLI", RuntimeState: "present", AuthState: "ready"},
			installed:     true,
			handwritten:   []hostExecProfile{{Name: "my-codex", Executable: "codex"}},
			wantSupport:   "flows_only",
			wantStep:      codexApprovalStep,
			wantLogin:     "signed_in",
			wantGoverance: "ungoverned",
		},
		{
			name:          "opencode ready gets generated profile",
			harness:       "opencode",
			agent:         AgentConfig{Name: "OpenCode", RuntimeState: "present", AuthState: "ready", IsOnboarded: true, OnboardingState: "fully_onboarded"},
			installed:     true,
			approved:      true,
			wantSupport:   "flows_only",
			wantProfile:   "opencode",
			wantLogin:     "signed_in",
			wantGoverance: "governed",
		},
		{
			name:          "opencode without plugin names the onboarding step",
			harness:       "opencode",
			agent:         AgentConfig{Name: "OpenCode", RuntimeState: "present", AuthState: "ready"},
			installed:     true,
			wantSupport:   "presence_only",
			wantStep:      "run `preloop agents onboard \"OpenCode\" --approvals` as the runner user",
			wantLogin:     "signed_in",
			wantGoverance: "ungoverned",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			exe := harnessExecutables[tc.harness]
			host := fakeHost{
				home:        t.TempDir(),
				env:         tc.env,
				executables: map[string]bool{exe: tc.installed},
				agents:      []AgentConfig{tc.agent},
				handwritten: tc.handwritten,
				hints:       &harnessRuntimeHints{},
			}
			d := host.deps("darwin")
			d.Version = func(string) string { return "0.171.0" }
			d.CodexHook = func() bool { return tc.approved }
			d.OpenCodeHook = func() bool { return tc.approved }
			entry := entryFor(t, buildHarnessInventory(d), tc.harness)
			profile := ""
			if entry.GeneratedProfile != nil {
				profile = *entry.GeneratedProfile
			}
			if entry.SupportLevel != tc.wantSupport || profile != tc.wantProfile || entry.SetupStep != tc.wantStep || entry.LoginState != tc.wantLogin || entry.Governance != tc.wantGoverance {
				t.Fatalf("entry = %+v (profile %q)", entry, profile)
			}
			if tc.installed && entry.Version != "0.171.0" {
				t.Fatalf("version = %q", entry.Version)
			}
			if entry.SessionMode != "none" {
				t.Fatalf("session mode = %q", entry.SessionMode)
			}
		})
	}
}

func TestCodexInventoryModelsAndGeneratedProfile(t *testing.T) {
	hints := &harnessRuntimeHints{}
	hints.noteModel("codex_cli", "gpt-6.2-astra")
	host := fakeHost{
		home:        t.TempDir(),
		executables: map[string]bool{"codex": true},
		agents:      []AgentConfig{{Name: "Codex CLI", RuntimeState: "present", AuthState: "ready"}},
		hints:       hints,
	}
	d := host.deps("darwin")
	d.CodexHook = func() bool { return true }
	d.CodexModel = func() string { return "gpt-6.1" }
	inv := buildHarnessInventory(d)
	entry := entryFor(t, inv, "codex_cli")
	if len(entry.Models) != 2 || entry.Models[0] != (harnessModel{ID: "gpt-6.2-astra", Source: "observed"}) || entry.Models[1] != (harnessModel{ID: "gpt-6.1", Source: "configured"}) {
		t.Fatalf("models = %+v", entry.Models)
	}
	var codex *hostExecProfile
	for _, profile := range generatedHostExecProfiles(inv) {
		if profile.Name == "codex" {
			codex = &profile
		}
	}
	if codex == nil || codex.Executable != "codex" || codex.ModelMap["gpt-6.2-astra"] != "gpt-6.2-astra" {
		t.Fatalf("generated profiles = %+v", generatedHostExecProfiles(inv))
	}
}

func TestCodexUndiscoveredUsesAuthFileLogin(t *testing.T) {
	host := fakeHost{home: t.TempDir(), executables: map[string]bool{"codex": true}, hints: &harnessRuntimeHints{}}
	d := host.deps("darwin")
	d.CodexHook = func() bool { return true }
	d.AuthFileLogin = func(name string) string {
		if name != "Codex CLI" {
			t.Fatalf("auth file check for %q", name)
		}
		return harnessLoginSignedIn
	}
	entry := entryFor(t, buildHarnessInventory(d), "codex_cli")
	if entry.LoginState != "signed_in" || entry.LoginSource != "stored" || entry.SupportLevel != "flows_only" || entry.GeneratedProfile == nil {
		t.Fatalf("codex = %+v", entry)
	}
}

func TestDiscoveredGeminiReportsVersion(t *testing.T) {
	host := fakeHost{
		home:        t.TempDir(),
		executables: map[string]bool{"gemini": true},
		agents:      []AgentConfig{{Name: "Gemini CLI", RuntimeState: "present"}},
	}
	d := host.deps("linux")
	d.Version = func(string) string { return "0.24.1" }
	entry := entryFor(t, buildHarnessInventory(d), "gemini_cli")
	if entry.Version != "0.24.1" || entry.SupportLevel != "presence_only" {
		t.Fatalf("gemini = %+v", entry)
	}
}

func TestPrintHarnessInventoryShowsSetupStep(t *testing.T) {
	var out bytes.Buffer
	printHarnessInventory(&out, harnessInventory{GeneratedAt: "now", Entries: []harnessInventoryEntry{{
		DisplayName: "Codex CLI", Version: "0.171.0", Enabled: true, LoginState: "signed_in", LoginSource: "cli_status",
		Governance: "partial", SupportLevel: "presence_only", Billing: "seat",
		SetupStep: "run `preloop agents onboard \"Codex CLI\" --approvals` as the runner user",
	}}})
	if !strings.Contains(out.String(), "Codex CLI (0.171.0, enabled)") || !strings.Contains(out.String(), "  next step: run `preloop agents onboard") {
		t.Fatalf("output = %s", out.String())
	}
}

func TestCodexSuccessfulRunNotesRequestedModel(t *testing.T) {
	saved := runnerHarnessHints
	runnerHarnessHints = &harnessRuntimeHints{}
	t.Cleanup(func() { runnerHarnessHints = saved })
	buffer := &runnerLogBuffer{native: true, harness: hostExecHarnessCodex, requestedModel: "gpt-6.2-astra"}
	buffer.codexCapture = codexCapture{SessionID: "0199a213-81c0-7800-8aa1-bbab2a035a53", Results: 1}
	if _, err := nativeRunnerResult(buffer, nil); err != nil {
		t.Fatal(err)
	}
	if _, observed := runnerHarnessHints.snapshot(hostExecHarnessCodex); len(observed) != 1 || observed[0] != "gpt-6.2-astra" {
		t.Fatalf("observed = %v", observed)
	}
}
