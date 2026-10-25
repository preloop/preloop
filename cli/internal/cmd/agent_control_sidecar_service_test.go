package cmd

import (
	"bytes"
	"errors"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

func TestSidecarLaunchdCarriesPathAndLogs(t *testing.T) {
	env := agentControlSidecarServiceEnv{
		Path:    "/opt/homebrew/bin:/usr/bin",
		LogPath: "/Users/example/.preloop/logs/claude-sidecar.log",
		Home:    "/Users/example",
	}
	for _, spec := range []agentControlSidecarSpec{claudeAgentControlSidecarSpec(), codexAgentControlSidecarSpec()} {
		body := renderAgentControlSidecarLaunchd(spec, "/usr/local/bin/preloop", env)
		for _, want := range []string{
			"<key>EnvironmentVariables</key>\n  <dict><key>HOME</key><string>/Users/example</string><key>PATH</key><string>/opt/homebrew/bin:/usr/bin</string></dict>",
			"<key>StandardOutPath</key><string>/Users/example/.preloop/logs/claude-sidecar.log</string>",
			"<key>StandardErrorPath</key><string>/Users/example/.preloop/logs/claude-sidecar.log</string>",
			"<string>" + spec.Command + "</string><string>sidecar</string><string>run</string>",
			"<key>KeepAlive</key><true/>",
		} {
			if !strings.Contains(body, want) {
				t.Fatalf("%s launchd missing %q:\n%s", spec.Command, want, body)
			}
		}
	}
}

func TestSidecarLaunchdEscapesPath(t *testing.T) {
	body := renderAgentControlSidecarLaunchd(
		claudeAgentControlSidecarSpec(),
		"/usr/local/bin/preloop",
		agentControlSidecarServiceEnv{Path: "/a&b/bin"},
	)
	if !strings.Contains(body, "<string>/a&amp;b/bin</string>") {
		t.Fatalf("PATH not escaped:\n%s", body)
	}
}

func TestSidecarSystemdCarriesPathAndLogs(t *testing.T) {
	body := renderAgentControlSidecarSystemd(
		claudeAgentControlSidecarSpec(),
		"/usr/local/bin/preloop",
		agentControlSidecarServiceEnv{
			Path:    "/home/example/.nvm/versions/node/v22/bin:/usr/bin",
			LogPath: "/home/example/.preloop/logs/claude-sidecar.log",
			Home:    "/home/example",
		},
	)
	for _, want := range []string{
		`Environment="HOME=/home/example"`,
		`Environment="PATH=/home/example/.nvm/versions/node/v22/bin:/usr/bin"`,
		"StandardOutput=append:/home/example/.preloop/logs/claude-sidecar.log",
		"StandardError=append:/home/example/.preloop/logs/claude-sidecar.log",
		`ExecStart="/usr/local/bin/preloop" claude sidecar run`,
	} {
		if !strings.Contains(body, want) {
			t.Fatalf("systemd missing %q:\n%s", want, body)
		}
	}
}

func TestSidecarSystemdSkipsLogPathWithSpaces(t *testing.T) {
	body := renderAgentControlSidecarSystemd(
		claudeAgentControlSidecarSpec(),
		"/usr/local/bin/preloop",
		agentControlSidecarServiceEnv{LogPath: "/home/a b/.preloop/logs/x.log"},
	)
	if strings.Contains(body, "StandardOutput=") {
		t.Fatalf("unsafe log path written:\n%s", body)
	}
}

func TestSidecarServicePathPutsNodeFirstAndDedupes(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("service files are written on macOS and Linux only")
	}
	binDir := t.TempDir()
	node := filepath.Join(binDir, "node")
	if err := os.WriteFile(node, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", binDir)
	// Pin HOME so the plugin fallback locations under ~ cannot match.
	t.Setenv("HOME", t.TempDir())
	t.Setenv("NPM_CONFIG_PREFIX", t.TempDir())
	got := agentControlSidecarServicePath(
		claudeAgentControlSidecarSpec(),
		"/usr/bin"+string(os.PathListSeparator)+binDir+string(os.PathListSeparator)+"relative",
	)
	parts := filepath.SplitList(got)
	if len(parts) == 0 || parts[0] != binDir {
		t.Fatalf("node dir not first: %q", got)
	}
	count := 0
	for _, part := range parts {
		if part == binDir {
			count++
		}
		if part == "relative" {
			t.Fatalf("relative dir kept: %q", got)
		}
	}
	if count != 1 {
		t.Fatalf("node dir repeated: %q", got)
	}
	if !strings.Contains(got, "/usr/bin") {
		t.Fatalf("shell PATH dropped: %q", got)
	}
}

func TestSidecarServiceExitStatusParsing(t *testing.T) {
	spec := claudeAgentControlSidecarSpec()
	original := agentControlSidecarServiceManagerOutput
	t.Cleanup(func() { agentControlSidecarServiceManagerOutput = original })

	agentControlSidecarServiceManagerOutput = func(agentControlSidecarSpec) (string, error) {
		return "{\n\t\"LimitLoadToSessionType\" = \"Aqua\";\n\t\"Label\" = \"ai.preloop.claude-sidecar\";\n\t\"LastExitStatus\" = 256;\n};\n", nil
	}
	if got := agentControlSidecarServiceExitStatus(spec); got != "last exit status: 1" {
		t.Fatalf("launchd status = %q", got)
	}
	for raw, want := range map[string]string{
		"0":   "0",
		"9":   "terminated by signal 9",
		"11":  "terminated by signal 11",
		"512": "2",
		"-1":  "-1",
		"x":   "x",
	} {
		if got := launchdExitStatusText(raw); got != want {
			t.Fatalf("launchdExitStatusText(%q) = %q, want %q", raw, got, want)
		}
	}
	agentControlSidecarServiceManagerOutput = func(agentControlSidecarSpec) (string, error) {
		return "ExecMainStatus=1\nActiveState=failed\n", nil
	}
	if got := agentControlSidecarServiceExitStatus(spec); got != "last exit status: 1" {
		t.Fatalf("systemd status = %q", got)
	}
}

func TestSidecarStatusPrintsExitStatusWhenSocketDown(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("unix sockets")
	}
	t.Setenv("HOME", t.TempDir())
	original := agentControlSidecarServiceManagerOutput
	t.Cleanup(func() { agentControlSidecarServiceManagerOutput = original })
	agentControlSidecarServiceManagerOutput = func(agentControlSidecarSpec) (string, error) {
		return "\t\"LastExitStatus\" = 256;\n", nil
	}
	var out bytes.Buffer
	cmd := &cobra.Command{}
	cmd.SetOut(&out)
	if err := runAgentControlSidecarStatus(claudeAgentControlSidecarSpec(), cmd, nil); err != nil {
		t.Fatal(err)
	}
	text := out.String()
	if !strings.Contains(text, "socket: down") ||
		!strings.Contains(text, "last exit status: 1") ||
		!strings.Contains(text, filepath.Join(".preloop", "logs", "claude-sidecar.log")) {
		t.Fatalf("status output = %q", text)
	}
}

func TestSystemdQuoteEscapesSpecialCharacters(t *testing.T) {
	cases := map[string]string{
		"PATH=/usr/bin":      `"PATH=/usr/bin"`,
		`PATH=/a%b`:          `"PATH=/a%%b"`,
		`PATH=/a"b`:          `"PATH=/a\"b"`,
		`PATH=/a\b`:          `"PATH=/a\\b"`,
		"PATH=/a\nb":         `"PATH=/a b"`,
		`HOME=/home/x y%"\z`: `"HOME=/home/x y%%\"\\z"`,
	}
	for in, want := range cases {
		if got := systemdQuote(in); got != want {
			t.Fatalf("systemdQuote(%q) = %s, want %s", in, got, want)
		}
	}
}

func stubCodexSidecarInstaller(t *testing.T) *int {
	t.Helper()
	t.Setenv("HOME", t.TempDir())
	calls := 0
	orig := agentControlSidecarServiceInstaller
	t.Cleanup(func() { agentControlSidecarServiceInstaller = orig })
	agentControlSidecarServiceInstaller = func(spec agentControlSidecarSpec, out io.Writer) error {
		calls++
		path := agentControlSidecarServiceFile(spec)
		if path == "" {
			return errAgentControlSidecarServiceUnsupported
		}
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			return err
		}
		return os.WriteFile(path, []byte("unit"), 0o644)
	}
	origResolve := resolveCodexSidecarInvocation
	t.Cleanup(func() { resolveCodexSidecarInvocation = origResolve })
	resolveCodexSidecarInvocation = func(agentControlSidecarSpec) error { return nil }
	if err := os.MkdirAll(filepath.Dir(codexControlConfigPath()), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(codexControlConfigPath(), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	return &calls
}

func TestCodexOnboardEnablesSidecarService(t *testing.T) {
	if runtime.GOOS != "darwin" && runtime.GOOS != "linux" {
		t.Skip("launchd/systemd only")
	}
	calls := stubCodexSidecarInstaller(t)
	agent := AgentConfig{Name: "Codex CLI"}

	var out bytes.Buffer
	maybeEnableCodexSidecarOnOnboard(agent, managedEnrollmentOptions{}, nil, &out)
	if *calls != 1 {
		t.Fatalf("expected one install, got %d: %s", *calls, out.String())
	}
	if !strings.Contains(out.String(), "sidecar service enabled") {
		t.Fatalf("missing enabled line: %q", out.String())
	}

	// Re-onboarding with the service already installed is a no-op.
	out.Reset()
	maybeEnableCodexSidecarOnOnboard(agent, managedEnrollmentOptions{}, nil, &out)
	if *calls != 1 {
		t.Fatalf("re-onboard reinstalled the service: %d calls", *calls)
	}
	if !strings.Contains(out.String(), "already installed") {
		t.Fatalf("missing already-installed line: %q", out.String())
	}

	// Other agents never touch the Codex sidecar.
	maybeEnableCodexSidecarOnOnboard(AgentConfig{Name: "Cursor"}, managedEnrollmentOptions{}, nil, &out)
	if *calls != 1 {
		t.Fatalf("non-codex agent installed the codex sidecar")
	}
}

func TestCodexOnboardNoSidecarFlag(t *testing.T) {
	calls := stubCodexSidecarInstaller(t)
	var out bytes.Buffer
	maybeEnableCodexSidecarOnOnboard(
		AgentConfig{Name: "Codex CLI"},
		managedEnrollmentOptions{NoSidecar: true},
		nil,
		&out,
	)
	if *calls != 0 {
		t.Fatalf("--no-sidecar still installed the service")
	}
	if !strings.Contains(out.String(), codexSidecarManualHint(codexAgentControlSidecarSpec())) {
		t.Fatalf("expected manual enable hint, got %q", out.String())
	}
	if agentsEnrollCmd.Flags().Lookup("no-sidecar") == nil {
		t.Fatalf("agents onboard has no --no-sidecar flag")
	}
}

func TestCodexOnboardPrintsManualCommandWhenInstallFails(t *testing.T) {
	if runtime.GOOS != "darwin" && runtime.GOOS != "linux" {
		t.Skip("launchd/systemd only")
	}
	calls := stubCodexSidecarInstaller(t)
	agentControlSidecarServiceInstaller = func(agentControlSidecarSpec, io.Writer) error {
		*calls++
		return errAgentControlSidecarServiceUnsupported
	}
	var out bytes.Buffer
	maybeEnableCodexSidecarOnOnboard(AgentConfig{Name: "Codex CLI"}, managedEnrollmentOptions{}, nil, &out)
	if !strings.Contains(out.String(), "preloop codex sidecar enable") {
		t.Fatalf("expected manual enable hint, got %q", out.String())
	}
}

func TestCodexOnboardSkipsSidecarWhenPluginInstallFailed(t *testing.T) {
	if runtime.GOOS != "darwin" && runtime.GOOS != "linux" {
		t.Skip("launchd/systemd only")
	}
	calls := stubCodexSidecarInstaller(t)
	var out bytes.Buffer
	maybeEnableCodexSidecarOnOnboard(
		AgentConfig{Name: "Codex CLI"},
		managedEnrollmentOptions{},
		map[string]interface{}{"control_plugin_install_status": "runtime_plugin_installer_not_found"},
		&out,
	)
	if *calls != 0 {
		t.Fatalf("installed a sidecar service without the plugin")
	}
	if !strings.Contains(out.String(), "preloop codex sidecar enable") {
		t.Fatalf("expected manual enable hint, got %q", out.String())
	}
}

func TestCodexOnboardSkipsSidecarWhenBinaryMissing(t *testing.T) {
	if runtime.GOOS != "darwin" && runtime.GOOS != "linux" {
		t.Skip("launchd/systemd only")
	}
	calls := stubCodexSidecarInstaller(t)
	resolveCodexSidecarInvocation = func(agentControlSidecarSpec) error {
		return errStringForTest("codex sidecar not found")
	}
	var out bytes.Buffer
	maybeEnableCodexSidecarOnOnboard(
		AgentConfig{Name: "Codex CLI"},
		managedEnrollmentOptions{},
		map[string]interface{}{"control_plugin_install_status": "installed_and_verified"},
		&out,
	)
	if *calls != 0 {
		t.Fatalf("installed a sidecar service with no runnable sidecar")
	}
	if !strings.Contains(out.String(), "codex sidecar not found") {
		t.Fatalf("expected the resolve error, got %q", out.String())
	}
}

func TestSidecarServiceWritersReportStartFailure(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	orig := runSidecarServiceManager
	t.Cleanup(func() { runSidecarServiceManager = orig })
	runSidecarServiceManager = func(name string, _ ...string) error {
		return errStringForTest(name + " failed")
	}
	spec := codexAgentControlSidecarSpec()
	var out bytes.Buffer
	if err := writeAgentControlSidecarSystemd(spec, "/usr/local/bin/preloop", &out); !errors.Is(err, errAgentControlSidecarServiceStart) {
		t.Fatalf("systemd start failure not reported: %v", err)
	}
	if err := writeAgentControlSidecarLaunchd(spec, "/usr/local/bin/preloop", &out); !errors.Is(err, errAgentControlSidecarServiceStart) {
		t.Fatalf("launchd start failure not reported: %v", err)
	}
}
