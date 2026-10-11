package cmd

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const fakeToken = "ghp_FAKE_TOKEN_VALUE_must_never_leak_1234567890"

type fakeHost struct {
	home        string
	env         map[string]string
	executables map[string]bool
	agents      []AgentConfig
	handwritten []hostExecProfile
	harnesses   map[string]runnerHarnessConfig
	hints       *harnessRuntimeHints
}

func (f fakeHost) deps(goos string) harnessInventoryDeps {
	return harnessInventoryDeps{
		Home: f.home,
		GOOS: goos,
		HasEnv: func(name string) bool {
			_, ok := f.env[name]
			return ok
		},
		HostEnv:  func(name string) string { return f.env[name] },
		Discover: func() []AgentConfig { return f.agents },
		Resolve: func(name string) (string, error) {
			if f.executables[name] {
				return filepath.Join(f.home, "bin", name), nil
			}
			return "", errors.New("not found")
		},
		Version:     func(string) string { return "1.0.95" },
		CursorLogin: func(string) string { return harnessLoginSignedOut },
		Handwritten: f.handwritten,
		Harnesses:   f.harnesses,
		Now:         func() time.Time { return time.Date(2026, 10, 16, 9, 0, 0, 0, time.UTC) },
		Hints:       f.hints,
	}
}

func writeCopilotConfig(t *testing.T, dir, body string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "config.json"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func entryFor(t *testing.T, inv harnessInventory, harness string) harnessInventoryEntry {
	t.Helper()
	for _, entry := range inv.Entries {
		if entry.Harness == harness {
			return entry
		}
	}
	t.Fatalf("no %s entry in %+v", harness, inv.Entries)
	return harnessInventoryEntry{}
}

func TestBuildHarnessInventoryCopilotStoredLoginAcrossOSes(t *testing.T) {
	for _, goos := range []string{"linux", "darwin", "windows"} {
		t.Run(goos, func(t *testing.T) {
			home := t.TempDir()
			writeCopilotConfig(t, filepath.Join(home, ".copilot"), `{
  "model": "claude-sonnet-4.6",
  "last_logged_in_user": {"host": "https://github.com", "login": "jane-doe"},
  "logged_in_users": [{"host": "https://github.com", "login": "jane-doe"}]
}`)
			host := fakeHost{
				home:        home,
				env:         map[string]string{},
				executables: map[string]bool{"copilot": true},
				agents: []AgentConfig{
					{Name: "Claude Desktop", RuntimeState: "present"},
					{Name: "Codex CLI", RuntimeState: "missing"},
				},
			}
			inv := buildHarnessInventory(host.deps(goos))
			copilot := entryFor(t, inv, "copilot_cli")
			if copilot.LoginState != "signed_in" || copilot.LoginSource != "stored" {
				t.Fatalf("login = %s/%s", copilot.LoginState, copilot.LoginSource)
			}
			if copilot.AccountHost != "github.com" || copilot.Version != "1.0.95" {
				t.Fatalf("host/version = %q/%q", copilot.AccountHost, copilot.Version)
			}
			if copilot.SupportLevel != "flows_and_sessions" || copilot.SessionMode != "resume" || copilot.Billing != "seat" {
				t.Fatalf("unexpected copilot entry %+v", copilot)
			}
			if copilot.Governance != "ungoverned" {
				t.Fatalf("governance = %s", copilot.Governance)
			}
			if copilot.GeneratedProfile == nil || *copilot.GeneratedProfile != "copilot" {
				t.Fatalf("generated profile = %v", copilot.GeneratedProfile)
			}
			sources := map[string]string{}
			for _, model := range copilot.Models {
				sources[model.ID] = model.Source
			}
			if sources["auto"] != "static" || sources["claude-sonnet-4.6"] != "static" {
				t.Fatalf("models = %+v", copilot.Models)
			}
			desktop := entryFor(t, inv, "claude_desktop")
			if desktop.SupportLevel != "presence_only" || desktop.LoginState != "not_applicable" {
				t.Fatalf("desktop = %+v", desktop)
			}
			for _, entry := range inv.Entries {
				if entry.Harness == "codex_cli" {
					t.Fatal("a missing runtime must not be reported")
				}
			}
			raw, _ := json.Marshal(inv)
			for _, leak := range []string{"jane-doe", home, "bin"} {
				if strings.Contains(string(raw), leak) {
					t.Fatalf("inventory leaks %q: %s", leak, raw)
				}
			}
			want, _ := harnessInventoryHash(inv.Entries)
			if inv.Hash != want || inv.GeneratedAt != "2026-10-16T09:00:00Z" {
				t.Fatalf("hash/time = %s %s", inv.Hash, inv.GeneratedAt)
			}
		})
	}
}

func TestBuildHarnessInventoryConfiguredModelIsMarkedConfigured(t *testing.T) {
	home := t.TempDir()
	writeCopilotConfig(t, filepath.Join(home, ".copilot"), `{"model": "gpt-5.1-codex"}`)
	host := fakeHost{home: home, env: map[string]string{}, executables: map[string]bool{"copilot": true}}
	copilot := entryFor(t, buildHarnessInventory(host.deps("linux")), "copilot_cli")
	last := copilot.Models[len(copilot.Models)-1]
	if last.ID != "gpt-5.1-codex" || last.Source != "configured" {
		t.Fatalf("models = %+v", copilot.Models)
	}
	if copilot.LoginState != "unknown" || copilot.AccountHost != "" {
		t.Fatalf("no login record must stay unknown: %+v", copilot)
	}
}

func TestBuildHarnessInventoryCopilotHomeOverride(t *testing.T) {
	home := t.TempDir()
	custom := filepath.Join(t.TempDir(), "copilot-home")
	writeCopilotConfig(t, custom, `{"logged_in_users": [{"host": "https://acme.ghe.com"}]}`)
	host := fakeHost{home: home, env: map[string]string{"COPILOT_HOME": custom}, executables: map[string]bool{"copilot": true}}
	copilot := entryFor(t, buildHarnessInventory(host.deps("windows")), "copilot_cli")
	if copilot.LoginSource != "stored" || copilot.AccountHost != "acme.ghe.com" {
		t.Fatalf("copilot = %+v", copilot)
	}
}

func TestBuildHarnessInventoryEnvTokenReportsNameOnly(t *testing.T) {
	for _, name := range copilotTokenEnvNames {
		t.Run(name, func(t *testing.T) {
			host := fakeHost{
				home:        t.TempDir(),
				env:         map[string]string{name: fakeToken, "COPILOT_GH_HOST": "https://Acme.GHE.com/"},
				executables: map[string]bool{"copilot": true},
			}
			inv := buildHarnessInventory(host.deps("linux"))
			copilot := entryFor(t, inv, "copilot_cli")
			if copilot.LoginState != "signed_in" || copilot.LoginSource != "env" {
				t.Fatalf("login = %s/%s", copilot.LoginState, copilot.LoginSource)
			}
			if copilot.AccountHost != "acme.ghe.com" {
				t.Fatalf("account_host = %q", copilot.AccountHost)
			}
			raw, _ := json.Marshal(inv)
			if strings.Contains(string(raw), fakeToken) || strings.Contains(string(raw), name) {
				t.Fatalf("token or variable name leaked: %s", raw)
			}
		})
	}
}

func TestBuildHarnessInventoryHostOnlyRejectsCredentialsInURL(t *testing.T) {
	if got := hostOnly("https://user:" + fakeToken + "@github.com"); got != "" {
		t.Fatalf("hostOnly kept a URL with userinfo: %q", got)
	}
	if got := hostOnly("acme.ghe.com:443"); got != "acme.ghe.com" {
		t.Fatalf("hostOnly = %q", got)
	}
}

func TestBuildHarnessInventorySignedOutHintWins(t *testing.T) {
	home := t.TempDir()
	writeCopilotConfig(t, filepath.Join(home, ".copilot"), `{"logged_in_users": [{"host": "https://github.com"}]}`)
	hints := &harnessRuntimeHints{}
	hints.markSignedOut("copilot_cli", true)
	hints.noteModel("copilot_cli", "gpt-5.3")
	hints.noteModel("copilot_cli", "bad model id with spaces")
	host := fakeHost{home: home, env: map[string]string{}, executables: map[string]bool{"copilot": true}, hints: hints}
	copilot := entryFor(t, buildHarnessInventory(host.deps("darwin")), "copilot_cli")
	if copilot.LoginState != "signed_out" {
		t.Fatalf("login_state = %s", copilot.LoginState)
	}
	last := copilot.Models[len(copilot.Models)-1]
	if last.ID != "gpt-5.3" || last.Source != "observed" {
		t.Fatalf("models = %+v", copilot.Models)
	}
}

func TestBuildHarnessInventoryHandwrittenCopilotProfileSuppressesGenerated(t *testing.T) {
	handwritten, err := normalizeHostExecProfile(hostExecProfile{Name: "copilot-review", Executable: "copilot"})
	if err != nil {
		t.Fatal(err)
	}
	host := fakeHost{
		home:        t.TempDir(),
		env:         map[string]string{},
		executables: map[string]bool{"copilot": true, "cursor-agent": true},
		handwritten: []hostExecProfile{handwritten},
	}
	inv := buildHarnessInventory(host.deps("linux"))
	if entryFor(t, inv, "copilot_cli").GeneratedProfile != nil {
		t.Fatal("hand-written copilot profile must win over the generated one")
	}
	cursor := entryFor(t, inv, "cursor_cli")
	if cursor.GeneratedProfile == nil || *cursor.GeneratedProfile != "cursor" {
		t.Fatalf("cursor generated profile = %v", cursor.GeneratedProfile)
	}
	if cursor.LoginState != "signed_out" || cursor.LoginSource != "cli_status" || cursor.SupportLevel != "flows_only" {
		t.Fatalf("cursor = %+v", cursor)
	}
	generated := generatedHostExecProfiles(inv)
	if len(generated) != 1 || generated[0].Name != "cursor" || generated[0].Executable != "cursor-agent" {
		t.Fatalf("generated = %+v", generated)
	}
	if generated[0].ModelMap["auto"] != "auto" || generated[0].AllowCheckout || generated[0].ForceWrites {
		t.Fatalf("generated defaults = %+v", generated[0])
	}
}

func TestBuildHarnessInventoryHandwrittenNameCollisionSuppressesGenerated(t *testing.T) {
	handwritten, err := normalizeHostExecProfile(hostExecProfile{Name: "Cursor", Executable: "copilot"})
	if err != nil {
		t.Fatal(err)
	}
	host := fakeHost{home: t.TempDir(), env: map[string]string{}, executables: map[string]bool{"cursor-agent": true}, handwritten: []hostExecProfile{handwritten}}
	if entryFor(t, buildHarnessInventory(host.deps("linux")), "cursor_cli").GeneratedProfile != nil {
		t.Fatal("a hand-written profile named cursor must win")
	}
}

func TestBuildHarnessInventoryInvalidProfileFileGeneratesNothing(t *testing.T) {
	host := fakeHost{home: t.TempDir(), env: map[string]string{}, executables: map[string]bool{"copilot": true}}
	deps := host.deps("linux")
	deps.HandwrittenErr = true
	if entryFor(t, buildHarnessInventory(deps), "copilot_cli").GeneratedProfile != nil {
		t.Fatal("an invalid profile file must not be bypassed by a generated profile")
	}
}

func TestBuildHarnessInventoryDisabledHarnessReportedButNotGenerated(t *testing.T) {
	off := false
	host := fakeHost{
		home:        t.TempDir(),
		env:         map[string]string{},
		executables: map[string]bool{"copilot": true},
		harnesses:   map[string]runnerHarnessConfig{"copilot_cli": {Enabled: &off, SessionsEnabled: true}},
	}
	inv := buildHarnessInventory(host.deps("linux"))
	copilot := entryFor(t, inv, "copilot_cli")
	if copilot.Enabled || copilot.GeneratedProfile != nil {
		t.Fatalf("disabled copilot = %+v", copilot)
	}
	if !copilot.SessionsEnabled {
		t.Fatal("sessions_enabled comes from the host config")
	}
	if len(generatedHostExecProfiles(inv)) != 0 {
		t.Fatal("a disabled harness must not get a generated profile")
	}
}

func TestBuildHarnessInventoryGovernanceFromCopilotHooks(t *testing.T) {
	home := t.TempDir()
	hooks := filepath.Join(home, ".copilot", "hooks")
	if err := os.MkdirAll(hooks, 0o700); err != nil {
		t.Fatal(err)
	}
	write := func(body string) {
		if err := os.WriteFile(filepath.Join(hooks, copilotPreloopHooksFileName), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	host := fakeHost{home: home, env: map[string]string{}, executables: map[string]bool{"copilot": true}}
	write(`{"hooks": {"postToolUse": [{"bash": "preloop usage-hook"}]}}`)
	if got := entryFor(t, buildHarnessInventory(host.deps("linux")), "copilot_cli").Governance; got != "partial" {
		t.Fatalf("usage hooks only = %s", got)
	}
	write(`{"hooks": {"preToolUse": [{"bash": "preloop agents permission-hook --client copilot"}]}}`)
	if got := entryFor(t, buildHarnessInventory(host.deps("linux")), "copilot_cli").Governance; got != "governed" {
		t.Fatalf("approval hook = %s", got)
	}
	if got := entryFor(t, buildHarnessInventory(host.deps("windows")), "copilot_cli").Governance; got != "partial" {
		t.Fatalf("a bash hook never runs on Windows, got %s", got)
	}
}

func TestDiscoveredHarnessMapping(t *testing.T) {
	host := fakeHost{
		home: t.TempDir(),
		env:  map[string]string{},
		agents: []AgentConfig{
			{Name: "Claude Code", RuntimeState: "present", AuthState: "ready", IsOnboarded: true, OnboardingState: "fully_onboarded"},
			{Name: "OpenCode", RuntimeState: "unknown", AuthState: "not_logged_in", IsOnboarded: true, OnboardingState: "mcp_proxy_only"},
			{Name: "Windsurf", RuntimeState: "present"},
		},
	}
	inv := buildHarnessInventory(host.deps("linux"))
	if len(inv.Entries) != 2 {
		t.Fatalf("entries = %+v", inv.Entries)
	}
	claude := entryFor(t, inv, "claude_code")
	if claude.Governance != "governed" || claude.Billing != "metered" || claude.SupportLevel != "flows_only" || claude.LoginState != "signed_in" {
		t.Fatalf("claude = %+v", claude)
	}
	opencode := entryFor(t, inv, "opencode")
	if opencode.Governance != "partial" || opencode.LoginState != "signed_out" {
		t.Fatalf("opencode = %+v", opencode)
	}
}

func TestCursorLoginFromStatus(t *testing.T) {
	cases := map[string]string{
		"Not logged in":                  "signed_out",
		"✓ Logged in as jane@example.com": "signed_in",
		"something else":                 "unknown",
	}
	for output, want := range cases {
		if got := cursorLoginFromStatus(output, true); got != want {
			t.Fatalf("%q -> %s, want %s", output, got, want)
		}
	}
	if parseHarnessVersion("GitHub Copilot CLI 1.0.95.\n") != "1.0.95" {
		t.Fatal("version parse")
	}
}
