package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

// agentControlSidecarSpec describes one npm-installed Agent Control sidecar.
// Claude and Codex share service-file rendering, start/stop, status probing,
// and the log path. Fields that appear in generated files are part of the
// Claude golden output and must stay stable.
type agentControlSidecarSpec struct {
	Command      string
	ServiceSlug  string
	SocketFile   string
	LogFile      string
	Description  string
	DisplayName  string
	BinName      string
	PackageName  string
	LaunchPhrase string
	OnboardHint  string
	ConfigPath   func() string
}

func claudeAgentControlSidecarSpec() agentControlSidecarSpec {
	return agentControlSidecarSpec{
		Command:      "claude",
		ServiceSlug:  "claude-sidecar",
		SocketFile:   "claude-control.sock",
		LogFile:      "claude-sidecar.log",
		Description:  "Preloop Claude Code Agent Control sidecar",
		DisplayName:  "Claude",
		BinName:      "preloop-claude-plugin",
		PackageName:  "@preloop-ai/claude-plugin",
		LaunchPhrase: "starting Claude sidecar",
		OnboardHint:  `preloop agents onboard "Claude Code"`,
		ConfigPath:   claudeControlConfigPath,
	}
}

func codexAgentControlSidecarSpec() agentControlSidecarSpec {
	return agentControlSidecarSpec{
		Command:      "codex",
		ServiceSlug:  "codex-sidecar",
		SocketFile:   "codex-control.sock",
		LogFile:      "codex-sidecar.log",
		Description:  "Preloop Codex CLI Agent Control sidecar",
		DisplayName:  "Codex",
		BinName:      "preloop-codex-plugin",
		PackageName:  "@preloop-ai/codex-plugin",
		LaunchPhrase: "starting Codex sidecar",
		OnboardHint:  `preloop agents onboard "Codex CLI"`,
		ConfigPath:   codexControlConfigPath,
	}
}

func (spec agentControlSidecarSpec) socketPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".preloop", spec.SocketFile)
}

func (spec agentControlSidecarSpec) logPath() string {
	return filepath.Join(filepath.Dir(spec.socketPath()), "logs", spec.LogFile)
}

func (spec agentControlSidecarSpec) launchdPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, "Library", "LaunchAgents", "ai.preloop."+spec.ServiceSlug+".plist")
}

func (spec agentControlSidecarSpec) systemdPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".config", "systemd", "user", "preloop-"+spec.ServiceSlug+".service")
}

func (spec agentControlSidecarSpec) systemdUnit() string {
	return "preloop-" + spec.ServiceSlug + ".service"
}

func renderAgentControlSidecarLaunchd(spec agentControlSidecarSpec, bin string) string {
	escaped := xmlEscapeAttr(bin)
	return fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.preloop.%s</string>
  <key>ProgramArguments</key>
  <array><string>%s</string><string>%s</string><string>sidecar</string><string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
`, spec.ServiceSlug, escaped, spec.Command)
}

func renderAgentControlSidecarSystemd(spec agentControlSidecarSpec, bin string) string {
	quoted, _ := json.Marshal(bin)
	return fmt.Sprintf(`[Unit]
Description=%s
After=network-online.target

[Service]
ExecStart=%s %s sidecar run
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
`, spec.Description, string(quoted), spec.Command)
}

func ensureAgentControlSidecarRunning(spec agentControlSidecarSpec, out io.Writer) error {
	socket := spec.socketPath()
	if _, err := os.Stat(socket); err == nil {
		if conn, dialErr := net.DialTimeout("unix", socket, 200*time.Millisecond); dialErr == nil {
			_ = conn.Close()
			return nil
		}
	}
	if spec.Command == "claude" {
		fmt.Fprintln(out, "Starting Claude Code sidecar...")
	} else {
		fmt.Fprintf(out, "Starting %s sidecar...\n", spec.DisplayName)
	}
	return startAgentControlSidecarProcess(spec)
}

func startAgentControlSidecarProcess(spec agentControlSidecarSpec) error {
	invocation, err := resolveAgentControlSidecarInvocation(spec)
	if err != nil {
		return err
	}
	configPath := spec.ConfigPath()
	cmd := exec.Command(invocation.bin, append(invocation.args, "run", "--config", configPath)...)
	cmd.SysProcAttr = claudeSidecarSysProcAttr()
	logDir := filepath.Dir(spec.logPath())
	_ = os.MkdirAll(logDir, 0o700)
	stdout, err := os.OpenFile(spec.logPath(), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	fmt.Fprintf(
		stdout,
		"[%s] launcher: %s: %s %s\n",
		time.Now().Format(time.RFC3339),
		spec.LaunchPhrase,
		invocation.bin,
		strings.Join(append(invocation.args, "run", "--config", configPath), " "),
	)
	cmd.Stdout = stdout
	cmd.Stderr = stdout
	if err := cmd.Start(); err != nil {
		_ = stdout.Close()
		return err
	}
	go func() {
		_ = cmd.Wait()
		_ = stdout.Close()
	}()
	return nil
}

func resolveAgentControlSidecarInvocation(spec agentControlSidecarSpec) (claudeSidecarInvocation, error) {
	if bin, err := resolveRuntimeExecutable(spec.BinName); err == nil {
		return claudeSidecarInvocation{bin: bin}, nil
	}
	entry, found, err := findAgentControlSidecarPackageEntry(spec, claudeNpmGlobalRootsFunc())
	if err != nil {
		return claudeSidecarInvocation{}, err
	}
	if found {
		node, nodeErr := resolveRuntimeExecutable("node")
		if nodeErr != nil {
			return claudeSidecarInvocation{}, fmt.Errorf(
				"found the %s sidecar at %s but node was not found on %s",
				spec.DisplayName,
				entry,
				runtimeExecutableSearchDescription("node"),
			)
		}
		return claudeSidecarInvocation{bin: node, args: []string{entry}}, nil
	}
	return claudeSidecarInvocation{}, fmt.Errorf(
		"%s was not found on %s or in the npm global directory; run: %s",
		spec.BinName,
		runtimeExecutableSearchDescription(spec.BinName),
		spec.OnboardHint,
	)
}

func findAgentControlSidecarPackageEntry(
	spec agentControlSidecarSpec,
	roots []string,
) (string, bool, error) {
	scope, name, ok := strings.Cut(spec.PackageName, "/")
	if !ok || !strings.HasPrefix(scope, "@") || name == "" {
		return "", false, fmt.Errorf("invalid sidecar package name %q", spec.PackageName)
	}
	for _, root := range roots {
		pkgDir := filepath.Join(root, scope, name)
		info, err := os.Stat(pkgDir)
		if err != nil || !info.IsDir() {
			continue
		}
		entry := filepath.Join(pkgDir, "dist", "index.js")
		if entryInfo, entryErr := os.Stat(entry); entryErr == nil && !entryInfo.IsDir() {
			return entry, true, nil
		}
		return "", false, fmt.Errorf(
			"%s is installed at %s but its dist/index.js build output is missing; "+
				"rerun %s to rebuild it, "+
				"or run: npm install -g %s",
			spec.PackageName,
			pkgDir,
			spec.OnboardHint,
			spec.PackageName,
		)
	}
	return "", false, nil
}

func runAgentControlSidecarEnable(spec agentControlSidecarSpec, cmd *cobra.Command, _ []string) error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	switch runtime.GOOS {
	case "darwin":
		return writeAgentControlSidecarLaunchd(spec, self, cmd.OutOrStdout())
	case "linux":
		return writeAgentControlSidecarSystemd(spec, self, cmd.OutOrStdout())
	default:
		return fmt.Errorf(
			"sidecar service install is not implemented on %s; use preloop %s sidecar run",
			runtime.GOOS,
			spec.Command,
		)
	}
}

func runAgentControlSidecarDisable(spec agentControlSidecarSpec, _ *cobra.Command, _ []string) error {
	return removeAgentControlSidecarService(spec)
}

func removeAgentControlSidecarService(spec agentControlSidecarSpec) error {
	switch runtime.GOOS {
	case "darwin":
		path := spec.launchdPath()
		_ = exec.Command("launchctl", "unload", path).Run()
		return os.Remove(path)
	case "linux":
		_ = exec.Command("systemctl", "--user", "disable", "--now", spec.systemdUnit()).Run()
		return os.Remove(spec.systemdPath())
	default:
		return fmt.Errorf("sidecar service install is not implemented on %s", runtime.GOOS)
	}
}

func runAgentControlSidecarStatus(spec agentControlSidecarSpec, cmd *cobra.Command, _ []string) error {
	path := spec.launchdPath()
	if runtime.GOOS == "linux" {
		path = spec.systemdPath()
	}
	if _, err := os.Stat(path); err == nil {
		fmt.Fprintf(cmd.OutOrStdout(), "install: present (%s)\n", path)
	} else {
		fmt.Fprintln(cmd.OutOrStdout(), "install: missing")
	}
	if conn, err := net.DialTimeout("unix", spec.socketPath(), 200*time.Millisecond); err == nil {
		_ = conn.Close()
		fmt.Fprintln(cmd.OutOrStdout(), "socket: listening")
	} else {
		fmt.Fprintln(cmd.OutOrStdout(), "socket: down")
	}
	return nil
}

func runAgentControlSidecarForeground(spec agentControlSidecarSpec, cmd *cobra.Command, _ []string) error {
	invocation, err := resolveAgentControlSidecarInvocation(spec)
	if err != nil {
		return err
	}
	child := exec.Command(invocation.bin, append(invocation.args, "run", "--config", spec.ConfigPath())...)
	child.Stdout = cmd.OutOrStdout()
	child.Stderr = cmd.ErrOrStderr()
	return child.Run()
}

func writeAgentControlSidecarLaunchd(spec agentControlSidecarSpec, bin string, out io.Writer) error {
	path := spec.launchdPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	body := renderAgentControlSidecarLaunchd(spec, bin)
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		return err
	}
	_ = exec.Command("launchctl", "load", path).Run()
	fmt.Fprintf(out, "Installed %s\n", path)
	return nil
}

func writeAgentControlSidecarSystemd(spec agentControlSidecarSpec, bin string, out io.Writer) error {
	path := spec.systemdPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	body := renderAgentControlSidecarSystemd(spec, bin)
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		return err
	}
	_ = exec.Command("systemctl", "--user", "daemon-reload").Run()
	_ = exec.Command("systemctl", "--user", "enable", "--now", spec.systemdUnit()).Run()
	fmt.Fprintf(out, "Installed %s\n", path)
	return nil
}

func codexControlConfigPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".codex", "preloop-control.json")
}

// removeCodexAgentControlArtifacts drops the Codex control file and sidecar
// service. It does not touch ~/.codex/config.toml.
func removeCodexAgentControlArtifacts() error {
	if err := removeAgentControlSidecarService(codexAgentControlSidecarSpec()); err != nil && !os.IsNotExist(err) {
		if runtime.GOOS == "darwin" || runtime.GOOS == "linux" {
			return err
		}
	}
	if err := os.Remove(codexControlConfigPath()); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("failed to remove Codex Agent Control config: %w", err)
	}
	return nil
}

func npmSidecarPackageMissingReason(packageName string, detail string) string {
	detail = strings.TrimSpace(strings.Split(detail, "\n")[0])
	if detail == "" {
		return fmt.Sprintf(
			"Agent Control plugin %s is not available: no npm package and no local source directory",
			packageName,
		)
	}
	return fmt.Sprintf(
		"Agent Control plugin %s is not available: %s",
		packageName,
		detail,
	)
}

func npmSidecarUnavailableResult(installTarget, reason string) map[string]interface{} {
	return map[string]interface{}{
		"control_plugin_installed":      false,
		"control_plugin_verified":       false,
		"control_plugin_verification":   reason,
		"control_plugin_install_status": "plugin_not_available",
		"control_plugin_install_target": installTarget,
		"control_plugin_install_error":  reason,
	}
}

func installTargetIsLocalSource(installTarget string) bool {
	info, err := os.Stat(installTarget)
	return err == nil && info.IsDir()
}

// probeNpmPackageListed reports whether npm can see the registry package.
// A missing package is a clean miss. A missing npm binary is also a miss.
// Tests replace this seam so the check never depends on a published package.
var probeNpmPackageListed = func(packageName string) (bool, string) {
	npmPath, err := resolveRuntimeExecutable("npm")
	if err != nil {
		return false, "npm was not found and no local source directory is present"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	output, cmdErr := exec.CommandContext(ctx, npmPath, "view", packageName, "version", "--json").CombinedOutput()
	if cmdErr != nil {
		message := strings.TrimSpace(string(output))
		if message == "" {
			message = cmdErr.Error()
		}
		return false, message
	}
	return true, ""
}
