package cmd

import (
	"context"
	"encoding/json"
	"errors"
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

// agentControlSidecarServiceEnv is what a service manager needs to run the
// sidecar outside a login shell: launchd and systemd start jobs with a
// minimal PATH that usually lacks node, and they discard output unless a log
// file is named.
type agentControlSidecarServiceEnv struct {
	// Path is the PATH the service runs with. Empty leaves it unset.
	Path string
	// LogPath receives stdout and stderr. Empty leaves output unredirected.
	LogPath string
	// Home pins HOME to the enabling user's home, so the job reads the same
	// control config and socket the enabling shell does (#1531).
	Home string
}

// systemdLogPathUsable reports whether path can be written as a systemd
// "append:" target, which takes no quoting.
func systemdLogPathUsable(path string) bool {
	return path != "" && !strings.ContainsAny(path, " \t\n\"\\%")
}

// agentControlSidecarServicePath returns the PATH to write into the service
// file: the directories of the resolved plugin binary and node first, then
// the enabling shell's PATH, without duplicates.
func agentControlSidecarServicePath(spec agentControlSidecarSpec, shellPath string) string {
	var dirs []string
	seen := map[string]bool{}
	add := func(dir string) {
		dir = strings.TrimSpace(dir)
		if dir == "" || !filepath.IsAbs(dir) || seen[dir] {
			return
		}
		seen[dir] = true
		dirs = append(dirs, dir)
	}
	if invocation, err := resolveAgentControlSidecarInvocation(spec); err == nil {
		add(filepath.Dir(invocation.bin))
	}
	if node, err := resolveRuntimeExecutable("node"); err == nil {
		add(filepath.Dir(node))
	}
	for _, dir := range filepath.SplitList(shellPath) {
		add(dir)
	}
	for _, dir := range []string{"/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"} {
		add(dir)
	}
	return strings.Join(dirs, string(os.PathListSeparator))
}

func agentControlSidecarServiceEnvFor(spec agentControlSidecarSpec) agentControlSidecarServiceEnv {
	home, _ := os.UserHomeDir()
	return agentControlSidecarServiceEnv{
		Path:    agentControlSidecarServicePath(spec, os.Getenv("PATH")),
		LogPath: spec.logPath(),
		Home:    home,
	}
}

func renderAgentControlSidecarLaunchd(spec agentControlSidecarSpec, bin string, env agentControlSidecarServiceEnv) string {
	escaped := xmlEscapeAttr(bin)
	var extra strings.Builder
	if env.Path != "" || env.Home != "" {
		extra.WriteString("  <key>EnvironmentVariables</key>\n  <dict>")
		if env.Home != "" {
			fmt.Fprintf(&extra, "<key>HOME</key><string>%s</string>", xmlEscapeAttr(env.Home))
		}
		if env.Path != "" {
			fmt.Fprintf(&extra, "<key>PATH</key><string>%s</string>", xmlEscapeAttr(env.Path))
		}
		extra.WriteString("</dict>\n")
	}
	if env.LogPath != "" {
		logPath := xmlEscapeAttr(env.LogPath)
		fmt.Fprintf(&extra, "  <key>StandardOutPath</key><string>%s</string>\n  <key>StandardErrorPath</key><string>%s</string>\n", logPath, logPath)
	}
	return fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.preloop.%s</string>
  <key>ProgramArguments</key>
  <array><string>%s</string><string>%s</string><string>sidecar</string><string>run</string></array>
%s  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
`, spec.ServiceSlug, escaped, spec.Command, extra.String())
}

// systemdQuote quotes one value for a systemd unit line, escaping the
// characters systemd treats specially inside double quotes.
func systemdQuote(value string) string {
	replacer := strings.NewReplacer(`\`, `\\`, `"`, `\"`, "%", "%%", "\n", " ")
	return `"` + replacer.Replace(value) + `"`
}

func renderAgentControlSidecarSystemd(spec agentControlSidecarSpec, bin string, env agentControlSidecarServiceEnv) string {
	quoted, _ := json.Marshal(bin)
	var extra strings.Builder
	if env.Home != "" {
		fmt.Fprintf(&extra, "Environment=%s\n", systemdQuote("HOME="+env.Home))
	}
	if env.Path != "" {
		fmt.Fprintf(&extra, "Environment=%s\n", systemdQuote("PATH="+env.Path))
	}
	if systemdLogPathUsable(env.LogPath) {
		fmt.Fprintf(&extra, "StandardOutput=append:%s\nStandardError=append:%s\n", env.LogPath, env.LogPath)
	}
	return fmt.Sprintf(`[Unit]
Description=%s
After=network-online.target

[Service]
ExecStart=%s %s sidecar run
%sRestart=always
RestartSec=5

[Install]
WantedBy=default.target
`, spec.Description, string(quoted), spec.Command, extra.String())
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
	err := agentControlSidecarServiceInstaller(spec, cmd.OutOrStdout())
	if errors.Is(err, errAgentControlSidecarServiceStart) {
		// The unit file is written; a re-enable of an already loaded service
		// can fail here too, so warn instead of failing the command.
		fmt.Fprintf(cmd.ErrOrStderr(), "Warning: %v\n", err) //nolint:errcheck
		return nil
	}
	return err
}

// errAgentControlSidecarServiceStart marks a service file that was written
// but whose service manager (launchctl, systemctl) did not start it.
var errAgentControlSidecarServiceStart = errors.New("sidecar service was installed but did not start")

// runSidecarServiceManager runs launchctl or systemctl. It is a variable so
// tests never touch the real user session.
var runSidecarServiceManager = func(name string, args ...string) error {
	output, err := exec.Command(name, args...).CombinedOutput()
	if err != nil {
		detail := strings.TrimSpace(string(output))
		if detail != "" {
			return fmt.Errorf("%s %s: %w: %s", name, strings.Join(args, " "), err, detail)
		}
		return fmt.Errorf("%s %s: %w", name, strings.Join(args, " "), err)
	}
	return nil
}

var errAgentControlSidecarServiceUnsupported = errors.New("sidecar service install is not implemented on this OS")

// agentControlSidecarServiceFile is the service definition path for this OS,
// or "" where no service manager is supported.
func agentControlSidecarServiceFile(spec agentControlSidecarSpec) string {
	switch runtime.GOOS {
	case "darwin":
		return spec.launchdPath()
	case "linux":
		return spec.systemdPath()
	default:
		return ""
	}
}

func installAgentControlSidecarService(spec agentControlSidecarSpec, out io.Writer) error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	switch runtime.GOOS {
	case "darwin":
		return writeAgentControlSidecarLaunchd(spec, self, out)
	case "linux":
		return writeAgentControlSidecarSystemd(spec, self, out)
	default:
		return fmt.Errorf(
			"%w (%s); use preloop %s sidecar run",
			errAgentControlSidecarServiceUnsupported,
			runtime.GOOS,
			spec.Command,
		)
	}
}

// agentControlSidecarServiceInstaller installs and starts the sidecar
// service. It is a variable so tests never touch launchctl or systemctl.
var agentControlSidecarServiceInstaller = installAgentControlSidecarService

// codexSidecarManualHint is the command to print when onboarding does not
// start the service itself: enable where a service manager is supported,
// otherwise the foreground run command.
func codexSidecarManualHint(spec agentControlSidecarSpec) string {
	if agentControlSidecarServiceFile(spec) == "" {
		return "Run `preloop codex sidecar run` to start the sidecar."
	}
	return "Run `preloop codex sidecar enable` to keep the sidecar up."
}

// resolveCodexSidecarInvocation reports whether the sidecar binary can run.
// It is a variable so tests do not depend on the host's npm install.
var resolveCodexSidecarInvocation = func(spec agentControlSidecarSpec) error {
	_, err := resolveAgentControlSidecarInvocation(spec)
	return err
}

// codexPluginInstallFailed reports whether onboarding tried and failed to
// install the runtime plugin the sidecar service would run.
func codexPluginInstallFailed(pluginResult map[string]interface{}) bool {
	status, _ := pluginResult["control_plugin_install_status"].(string)
	if status == "" || strings.HasPrefix(status, "installed") {
		return false
	}
	return pluginResult["control_plugin_verified"] != true
}

// maybeEnableCodexSidecarOnOnboard installs the Codex Agent Control sidecar
// service after onboarding wrote ~/.codex/preloop-control.json, so Talk can
// connect without a manual step. It is idempotent and honours --no-sidecar.
// It skips the install when the plugin is missing (the service would crash
// loop) and only prints the manual command on any failure; it never fails
// the enrollment.
func maybeEnableCodexSidecarOnOnboard(
	agent AgentConfig,
	opts managedEnrollmentOptions,
	pluginResult map[string]interface{},
	out io.Writer,
) {
	if runtimeSessionSourceTypeForAgent(agent.Name) != "codex" || !supportsAgentControlChannel(agent) {
		return
	}
	if _, err := os.Stat(codexControlConfigPath()); err != nil {
		return
	}
	spec := codexAgentControlSidecarSpec()
	hint := codexSidecarManualHint(spec)
	if opts.NoSidecar {
		fmt.Fprintf(out, "  Codex Agent Control sidecar not started (--no-sidecar). %s\n", hint) //nolint:errcheck
		return
	}
	path := agentControlSidecarServiceFile(spec)
	if path == "" {
		fmt.Fprintf(out, "  Codex Agent Control sidecar service is not supported on %s. %s\n", runtime.GOOS, hint) //nolint:errcheck
		return
	}
	if _, err := os.Stat(path); err == nil {
		fmt.Fprintf(out, "  Codex Agent Control sidecar service already installed (%s)\n", path) //nolint:errcheck
		return
	}
	if codexPluginInstallFailed(pluginResult) {
		fmt.Fprintf(out, "  Codex Agent Control sidecar service not installed: the Codex plugin did not install. Fix the plugin, then: %s\n", hint) //nolint:errcheck
		return
	}
	if err := resolveCodexSidecarInvocation(spec); err != nil {
		fmt.Fprintf(out, "  Codex Agent Control sidecar service not installed: %v. %s\n", err, hint) //nolint:errcheck
		return
	}
	if err := agentControlSidecarServiceInstaller(spec, out); err != nil {
		fmt.Fprintf(out, "  Warning: Codex Agent Control sidecar service not started: %v. %s\n", err, hint) //nolint:errcheck
		return
	}
	fmt.Fprintln(out, "  Codex Agent Control sidecar service enabled (remove with `preloop codex sidecar disable`; skip with --no-sidecar)") //nolint:errcheck
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
		if line := agentControlSidecarServiceExitStatus(spec); line != "" {
			fmt.Fprintln(cmd.OutOrStdout(), line)
		}
		fmt.Fprintf(cmd.OutOrStdout(), "log: %s\n", spec.logPath())
	}
	return nil
}

// agentControlSidecarServiceManagerOutput runs the service manager query for
// the sidecar job. Tests replace it.
var agentControlSidecarServiceManagerOutput = func(spec agentControlSidecarSpec) (string, error) {
	switch runtime.GOOS {
	case "darwin":
		out, err := exec.Command("launchctl", "list", "ai.preloop."+spec.ServiceSlug).Output()
		return string(out), err
	case "linux":
		out, err := exec.Command(
			"systemctl", "--user", "show", spec.systemdUnit(),
			"--property=ExecMainStatus,ActiveState",
		).Output()
		return string(out), err
	default:
		return "", fmt.Errorf("unsupported")
	}
}

// agentControlSidecarServiceExitStatus reports the last exit status the
// service manager recorded for the sidecar job, or "" when unknown.
func agentControlSidecarServiceExitStatus(spec agentControlSidecarSpec) string {
	out, err := agentControlSidecarServiceManagerOutput(spec)
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimSpace(line)
		// launchctl list <label>: "LastExitStatus" = 256;
		if strings.HasPrefix(line, `"LastExitStatus"`) {
			_, value, ok := strings.Cut(line, "=")
			if !ok {
				continue
			}
			value = strings.TrimSuffix(strings.TrimSpace(value), ";")
			return "last exit status: " + launchdExitStatusText(value)
		}
		// systemctl show: ExecMainStatus=1
		if value, ok := strings.CutPrefix(line, "ExecMainStatus="); ok {
			return "last exit status: " + strings.TrimSpace(value)
		}
	}
	return ""
}

// launchdExitStatusText turns launchd's wait(2) status into text: a
// non-zero low byte is the terminating signal, otherwise the high byte is
// the exit code. A non-numeric value is returned as is.
func launchdExitStatusText(raw string) string {
	var status int
	if _, err := fmt.Sscanf(raw, "%d", &status); err != nil {
		return raw
	}
	if status < 0 {
		return fmt.Sprintf("%d", status)
	}
	if signal := status & 0x7f; signal != 0 {
		return fmt.Sprintf("terminated by signal %d", signal)
	}
	return fmt.Sprintf("%d", status>>8)
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
	env := agentControlSidecarServiceEnvFor(spec)
	if err := os.MkdirAll(filepath.Dir(env.LogPath), 0o700); err != nil {
		return err
	}
	body := renderAgentControlSidecarLaunchd(spec, bin, env)
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		return err
	}
	fmt.Fprintf(out, "Installed %s\n", path)
	if err := runSidecarServiceManager("launchctl", "load", path); err != nil {
		return fmt.Errorf("%w: %v", errAgentControlSidecarServiceStart, err)
	}
	return nil
}

func writeAgentControlSidecarSystemd(spec agentControlSidecarSpec, bin string, out io.Writer) error {
	path := spec.systemdPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	env := agentControlSidecarServiceEnvFor(spec)
	if err := os.MkdirAll(filepath.Dir(env.LogPath), 0o700); err != nil {
		return err
	}
	if !systemdLogPathUsable(env.LogPath) {
		fmt.Fprintf(out, "Warning: %s cannot be a systemd log target (it has a space, quote or %%); read logs with: journalctl --user -u %s\n", env.LogPath, spec.systemdUnit())
	}
	body := renderAgentControlSidecarSystemd(spec, bin, env)
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		return err
	}
	fmt.Fprintf(out, "Installed %s\n", path)
	if err := runSidecarServiceManager("systemctl", "--user", "daemon-reload"); err != nil {
		return fmt.Errorf("%w: %v", errAgentControlSidecarServiceStart, err)
	}
	if err := runSidecarServiceManager("systemctl", "--user", "enable", "--now", spec.systemdUnit()); err != nil {
		return fmt.Errorf("%w: %v", errAgentControlSidecarServiceStart, err)
	}
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
