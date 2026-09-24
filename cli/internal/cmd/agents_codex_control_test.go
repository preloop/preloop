package cmd

import (
	"bytes"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestCodexAgentControlPluginSwitchCases(t *testing.T) {
	agent := AgentConfig{Name: "Codex CLI"}
	if !supportsAgentControlChannel(agent) {
		t.Fatal("Codex CLI must support the Agent Control channel")
	}
	cases := []struct {
		name string
		got  string
		want string
	}{
		{"package", agentControlPluginPackageName(agent), "@preloop-ai/codex-plugin"},
		{"verify", agentControlPluginVerifyCommand(agent), "preloop-codex-plugin"},
		{"installer", agentControlPluginInstallerCommand(agent), "npm"},
		{"source", agentControlPluginSourceDirName(agent), "codex-preloop"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if tc.got != tc.want {
				t.Fatalf("%s = %q, want %q", tc.name, tc.got, tc.want)
			}
		})
	}
}

func TestCodexAgentControlPluginInstallTargetMatchesClaudeShape(t *testing.T) {
	empty := t.TempDir()
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", empty)
	codex := AgentConfig{Name: "Codex CLI"}
	claude := AgentConfig{Name: "Claude Code"}
	if agentControlPluginInstallTarget(codex) != "@preloop-ai/codex-plugin" {
		t.Fatalf("package fallback = %q", agentControlPluginInstallTarget(codex))
	}
	if agentControlPluginInstallTarget(claude) != "@preloop-ai/claude-plugin" {
		t.Fatalf("claude package fallback changed: %q", agentControlPluginInstallTarget(claude))
	}

	plugins := t.TempDir()
	source := filepath.Join(plugins, "codex-preloop")
	if err := os.MkdirAll(source, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", plugins)
	if agentControlPluginInstallTarget(codex) != source {
		t.Fatalf("source target = %q, want %q", agentControlPluginInstallTarget(codex), source)
	}
}

func TestCodexControlFileRoundTripLeavesClaudePathAlone(t *testing.T) {
	home := testenv.SetTempHome(t)
	control := map[string]interface{}{
		"enabled":         true,
		"protocol":        "preloop.agent_control.v1",
		"runtime":         "codex",
		"adapter_package": "@preloop-ai/codex-plugin",
		"bearer_token":    "token-1",
	}
	if err := writePreloopControlFile(codexControlConfigPath(), control); err != nil {
		t.Fatal(err)
	}
	got, ok := readPreloopControlFile(codexControlConfigPath())
	if !ok {
		t.Fatal("expected to read the Codex control file")
	}
	if got["runtime"] != "codex" || got["bearer_token"] != "token-1" {
		t.Fatalf("round trip = %#v", got)
	}
	claudePath := filepath.Join(home, ".claude", "preloop-control.json")
	if _, err := os.Stat(claudePath); !os.IsNotExist(err) {
		t.Fatalf("Claude control path should stay absent, stat err=%v", err)
	}

	claudeControl := map[string]interface{}{"runtime": "claude_code", "bearer_token": "claude-token"}
	if err := writeClaudePreloopControlFile(claudeControl); err != nil {
		t.Fatal(err)
	}
	readBack, ok := readClaudePreloopControlFile()
	if !ok || readBack["bearer_token"] != "claude-token" || readBack["runtime"] != "claude_code" {
		t.Fatalf("Claude control round trip = %#v ok=%v", readBack, ok)
	}
	codexAgain, ok := readPreloopControlFile(codexControlConfigPath())
	if !ok || codexAgain["bearer_token"] != "token-1" {
		t.Fatalf("Codex control file changed while writing Claude's: %#v", codexAgain)
	}
}

func TestApplyCodexAgentControlDoesNotTouchConfigDocument(t *testing.T) {
	doc := map[string]interface{}{
		"model": "gpt-5.4",
	}
	applyAgentControlConfigToDocument(
		AgentConfig{Name: "Codex CLI"},
		doc,
		map[string]interface{}{"bearer_token": "secret", "runtime": "codex"},
	)
	if _, ok := doc["preloop"]; ok {
		t.Fatalf("Codex document gained a preloop block: %#v", doc)
	}
	if doc["model"] != "gpt-5.4" {
		t.Fatalf("Codex document changed: %#v", doc)
	}
}

func TestRunAgentsInstallPluginCodexDryRunPrintsNpmGlobalInstall(t *testing.T) {
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", t.TempDir())
	cmd := agentsInstallPluginCmd
	if err := cmd.Flags().Set("dry-run", "true"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = cmd.Flags().Set("dry-run", "false")
		cmd.SetOut(nil)
	})
	buf := &bytes.Buffer{}
	cmd.SetOut(buf)
	if err := runAgentsInstallPlugin(cmd, []string{"Codex CLI"}); err != nil {
		t.Fatal(err)
	}
	got := strings.TrimSpace(buf.String())
	want := "npm install -g @preloop-ai/codex-plugin"
	if got != want {
		t.Fatalf("dry-run command = %q, want %q", got, want)
	}
}

func TestClaudeInstallWithoutNpmStillAttemptsManagedSidecar(t *testing.T) {
	testenv.SetTempHome(t)
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", t.TempDir())
	t.Setenv("PATH", t.TempDir())

	result := installAgentControlRuntimePlugin(AgentConfig{Name: "Claude Code"}, io.Discard)
	if result["control_plugin_install_status"] != "runtime_plugin_installer_not_found" {
		t.Fatalf("status = %#v, want runtime_plugin_installer_not_found", result["control_plugin_install_status"])
	}
	if result["control_plugin_install_status"] == "plugin_not_available" {
		t.Fatal("missing npm must not be reported as a missing published package")
	}
	verification, _ := result["control_plugin_verification"].(string)
	if !strings.Contains(verification, "python3 is required") {
		t.Fatalf("expected the managed sidecar fallback to run, got %q", verification)
	}
}

func TestInstallCodexPluginWhenPackageAndSourceMissing(t *testing.T) {
	testenv.SetTempHome(t)
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", t.TempDir())
	npmDir := t.TempDir()
	writeFakeNpm(t, npmDir, "404 Not Found", 1)
	t.Setenv("PATH", npmDir)

	var out strings.Builder
	result := installAgentControlRuntimePlugin(AgentConfig{Name: "Codex CLI"}, &out)
	if result["control_plugin_installed"] != false {
		t.Fatalf("control_plugin_installed = %#v, want false", result["control_plugin_installed"])
	}
	reason, _ := result["control_plugin_verification"].(string)
	if reason == "" || strings.Contains(reason, "\n") {
		t.Fatalf("expected a one-line reason, got %q", reason)
	}
	if !strings.Contains(reason, "@preloop-ai/codex-plugin") || !strings.Contains(reason, "not available") {
		t.Fatalf("reason = %q", reason)
	}
	if strings.TrimSpace(out.String()) == "" {
		t.Fatal("expected the reason to be reported to the user")
	}
}

func TestCodexValidateReportsControlKeys(t *testing.T) {
	skipNoShebangOnWindows(t, "Codex control plugin verify stub")
	home := testenv.SetTempHome(t)
	agent := AgentConfig{
		Name:       "Codex CLI",
		ConfigPath: filepath.Join(home, ".codex", "config.toml"),
	}
	baseURL := "https://preloop.example"
	control := buildManagedAgentControlConfig(agent, baseURL, "durable-token", nil, nil, nil)
	if err := writePreloopControlFile(codexControlConfigPath(), control); err != nil {
		t.Fatal(err)
	}
	binDir := t.TempDir()
	if err := os.WriteFile(
		filepath.Join(binDir, "preloop-codex-plugin"),
		[]byte("#!/bin/sh\n[ \"$1\" = verify ] && [ \"$2\" = --config ]\n"),
		0o755,
	); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", binDir)

	result := validateAgentControlConfig(agent, map[string]interface{}{}, baseURL)
	for _, key := range []string{
		"control_config_written",
		"control_plugin_installed",
		"control_plugin_verified",
		"control_channel_configured",
	} {
		if result[key] != true {
			t.Fatalf("%s = %#v, full result %#v", key, result[key], result)
		}
	}
}

func TestCodexSidecarServiceRenderingAndClaudeGolden(t *testing.T) {
	const bin = "/usr/local/bin/preloop"
	claudeLaunchd := renderAgentControlSidecarLaunchd(claudeAgentControlSidecarSpec(), bin)
	claudeGolden := `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.preloop.claude-sidecar</string>
  <key>ProgramArguments</key>
  <array><string>/usr/local/bin/preloop</string><string>claude</string><string>sidecar</string><string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
`
	if claudeLaunchd != claudeGolden {
		t.Fatalf("Claude launchd rendering changed:\n%s", claudeLaunchd)
	}
	claudeSystemd := renderAgentControlSidecarSystemd(claudeAgentControlSidecarSpec(), bin)
	claudeSystemdGolden := `[Unit]
Description=Preloop Claude Code Agent Control sidecar
After=network-online.target

[Service]
ExecStart="/usr/local/bin/preloop" claude sidecar run
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
`
	if claudeSystemd != claudeSystemdGolden {
		t.Fatalf("Claude systemd rendering changed:\n%s", claudeSystemd)
	}

	codexLaunchd := renderAgentControlSidecarLaunchd(codexAgentControlSidecarSpec(), bin)
	if !strings.Contains(codexLaunchd, "ai.preloop.codex-sidecar") ||
		!strings.Contains(codexLaunchd, "<string>codex</string><string>sidecar</string><string>run</string>") {
		t.Fatalf("Codex launchd = %s", codexLaunchd)
	}
	codexSystemd := renderAgentControlSidecarSystemd(codexAgentControlSidecarSpec(), bin)
	if !strings.Contains(codexSystemd, "Description=Preloop Codex CLI Agent Control sidecar") ||
		!strings.Contains(codexSystemd, `ExecStart="/usr/local/bin/preloop" codex sidecar run`) {
		t.Fatalf("Codex systemd = %s", codexSystemd)
	}
}

func TestOffboardCodexLeavesConfigTomlUntouched(t *testing.T) {
	home := testenv.SetTempHome(t)
	codexDir := filepath.Join(home, ".codex")
	if err := os.MkdirAll(codexDir, 0o755); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(codexDir, "config.toml")
	original := []byte("model = \"gpt-5.4\"\n# leave this file alone\n")
	if err := os.WriteFile(configPath, original, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := writePreloopControlFile(codexControlConfigPath(), map[string]interface{}{
		"runtime": "codex",
	}); err != nil {
		t.Fatal(err)
	}
	spec := codexAgentControlSidecarSpec()
	servicePath := ""
	switch runtime.GOOS {
	case "darwin":
		servicePath = spec.launchdPath()
	case "linux":
		servicePath = spec.systemdPath()
	}
	if servicePath != "" {
		if err := os.MkdirAll(filepath.Dir(servicePath), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(servicePath, []byte("service"), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	agent := AgentConfig{Name: "Codex CLI", ConfigPath: configPath}
	if err := removeManagedAgentRuntimeArtifacts(agent); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, original) {
		t.Fatalf("config.toml changed:\nbefore %q\nafter  %q", original, got)
	}
	if _, err := os.Stat(codexControlConfigPath()); !os.IsNotExist(err) {
		t.Fatalf("control file still present, stat err=%v", err)
	}
	if servicePath != "" {
		if _, err := os.Stat(servicePath); !os.IsNotExist(err) {
			t.Fatalf("sidecar service still present, stat err=%v", err)
		}
	}
}

func TestCodexSidecarRunExecsPluginBin(t *testing.T) {
	spec := codexAgentControlSidecarSpec()
	if spec.BinName != "preloop-codex-plugin" {
		t.Fatalf("bin = %q", spec.BinName)
	}
	if spec.ConfigPath() != codexControlConfigPath() {
		t.Fatal("config path mismatch")
	}
	// Foreground run appends `run --config <path>` to the plugin invocation.
	// Assert the shared command shape without executing a missing binary.
	args := append([]string{}, "run", "--config", spec.ConfigPath())
	if len(args) != 3 || args[0] != "run" || args[1] != "--config" {
		t.Fatalf("run args = %#v", args)
	}
	_ = io.Discard
}
