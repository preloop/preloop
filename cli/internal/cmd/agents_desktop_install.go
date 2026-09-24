package cmd

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

const (
	desktopDisplay = ":99"
	desktopVNCHost = "127.0.0.1"
	desktopVNCPort = 5900
	desktopService = "preloop-desktop.service"
)

// desktopInstallOptions configures a loopback-only headless desktop install.
// Tests inject paths and command runners so the install never needs root.
type desktopInstallOptions struct {
	Runtime       string
	HomeDir       string
	OSReleasePath string
	GOOS          string
	Output        io.Writer
	Run           func(ctx context.Context, name string, args ...string) ([]byte, error)
	LookPath      func(string) (string, error)
	Now           func() time.Time
	Random        func(n int) ([]byte, error)
	// EUID overrides os.Geteuid. Nil uses the real user id.
	EUID func() int
}

var errDesktopPrivilegeRequired = errors.New("desktop_privilege_required")

// installDesktop installs Xvfb, a loopback-only x11vnc server, and a browser.
// Non-Debian distros return desktop_unsupported_distro before any runtime
// install step. The VNC password is passed only to x11vnc -storepasswd and is
// never written as plaintext.
func installDesktop(ctx context.Context, opts desktopInstallOptions) error {
	if ctx == nil {
		ctx = context.Background()
	}
	goos := opts.GOOS
	if goos == "" {
		goos = runtime.GOOS
	}
	if goos != "linux" {
		return errors.New("desktop_unsupported_os")
	}
	home, err := opts.homeDir()
	if err != nil {
		return err
	}
	debian, err := osReleaseIsDebian(opts.osReleasePath())
	if err != nil || !debian {
		return errors.New("desktop_unsupported_distro")
	}
	out := opts.Output
	if out == nil {
		out = os.Stdout
	}

	if err := installDesktopPackages(ctx, opts); err != nil {
		return err
	}
	browser, err := findDesktopBrowser(opts)
	if err != nil {
		return err
	}
	desktopDir := filepath.Join(home, ".preloop", "desktop")
	if err := os.MkdirAll(desktopDir, 0o700); err != nil {
		return fmt.Errorf("failed to create desktop directory: %w", err)
	}
	passwdPath := filepath.Join(desktopDir, "vncpasswd")
	if err := ensureDesktopVNCPassword(ctx, opts, passwdPath); err != nil {
		return err
	}
	startScript := filepath.Join(desktopDir, "start.sh")
	if err := writeDesktopFile(startScript, renderDesktopStartScript(passwdPath), 0o700); err != nil {
		return err
	}
	unitPath := filepath.Join(home, ".config", "systemd", "user", desktopService)
	if err := writeDesktopFile(unitPath, renderDesktopUnit(), 0o644); err != nil {
		return err
	}
	if err := enableDesktopService(ctx, opts, out, startScript); err != nil {
		return err
	}
	if err := writeDesktopManifest(home, browser, opts.now()); err != nil {
		return err
	}
	if err := applyDesktopDisplay(home, opts.Runtime); err != nil {
		return err
	}
	fmt.Fprintf( //nolint:errcheck
		out,
		"Installed loopback-only desktop on DISPLAY=%s (VNC %s:%d)\n",
		desktopDisplay,
		desktopVNCHost,
		desktopVNCPort,
	)
	return nil
}

func desktopDryRunText() string {
	return strings.Join([]string{
		"Would install a loopback-only headless desktop:",
		"  apt-get install -y xvfb x11vnc xdotool chromium (sudo -n when not root; fall back to chromium-browser)",
		"  x11vnc -storepasswd (password not printed) ~/.preloop/desktop/vncpasswd",
		"  write ~/.preloop/desktop/start.sh with Xvfb :99 and x11vnc -localhost -rfbport 5900",
		"  write ~/.config/systemd/user/preloop-desktop.service (Restart=on-failure)",
		"  systemctl --user enable --now preloop-desktop, or nohup if systemd --user is unavailable",
		"  write ~/.preloop/desktop.json",
		"  export DISPLAY=:99 for the runtime",
		"",
	}, "\n")
}

func (o desktopInstallOptions) homeDir() (string, error) {
	if strings.TrimSpace(o.HomeDir) != "" {
		return o.HomeDir, nil
	}
	return os.UserHomeDir()
}

func (o desktopInstallOptions) osReleasePath() string {
	if strings.TrimSpace(o.OSReleasePath) != "" {
		return o.OSReleasePath
	}
	return "/etc/os-release"
}

func (o desktopInstallOptions) now() time.Time {
	if o.Now != nil {
		return o.Now().UTC()
	}
	return time.Now().UTC()
}

func (o desktopInstallOptions) run(ctx context.Context, name string, args ...string) ([]byte, error) {
	if o.Run != nil {
		return o.Run(ctx, name, args...)
	}
	return runDesktopCommand(ctx, name, args...)
}

func runDesktopCommand(ctx context.Context, name string, args ...string) ([]byte, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	bin, err := exec.LookPath(name)
	if err != nil {
		return nil, err
	}
	cmd := exec.CommandContext(ctx, bin, args...)
	if name == "apt-get" {
		cmd.Env = append(os.Environ(), "DEBIAN_FRONTEND=noninteractive")
	}
	return cmd.CombinedOutput()
}

func osReleaseIsDebian(path string) (bool, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, err
	}
	return osReleaseContentIsDebian(string(data)), nil
}

func osReleaseContentIsDebian(content string) bool {
	values := parseOSRelease(content)
	for _, key := range []string{"ID", "ID_LIKE"} {
		for _, token := range strings.Fields(values[key]) {
			if strings.EqualFold(token, "debian") {
				return true
			}
		}
	}
	return false
}

func parseOSRelease(content string) map[string]string {
	values := make(map[string]string)
	for _, line := range strings.Split(content, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		values[strings.TrimSpace(key)] = strings.Trim(strings.TrimSpace(value), `"'`)
	}
	return values
}

func (o desktopInstallOptions) euid() int {
	if o.EUID != nil {
		return o.EUID()
	}
	return os.Geteuid()
}

func installDesktopPackages(ctx context.Context, opts desktopInstallOptions) error {
	packages := []string{"xvfb", "x11vnc", "xdotool", "chromium"}
	err := aptGetInstall(ctx, opts, packages)
	if err == nil {
		return nil
	}
	if errors.Is(err, errDesktopPrivilegeRequired) {
		return err
	}
	packages[3] = "chromium-browser"
	if err = aptGetInstall(ctx, opts, packages); err != nil {
		if errors.Is(err, errDesktopPrivilegeRequired) {
			return err
		}
		return fmt.Errorf("desktop package install failed: %w", err)
	}
	return nil
}

func aptGetInstall(ctx context.Context, opts desktopInstallOptions, packages []string) error {
	args := append([]string{"install", "-y"}, packages...)
	command := "apt-get"
	// GCP deployment SSHes in as the non-root metadata user. Ubuntu images
	// grant that user passwordless sudo; apt-get itself still needs root.
	if opts.euid() != 0 {
		if opts.Run == nil {
			if _, err := exec.LookPath("sudo"); err != nil {
				return errDesktopPrivilegeRequired
			}
		}
		command = "sudo"
		args = append([]string{"-n", "apt-get"}, args...)
	}
	if _, err := opts.run(ctx, command, args...); err != nil {
		return err
	}
	return nil
}

func findDesktopBrowser(opts desktopInstallOptions) (string, error) {
	look := opts.LookPath
	if look == nil {
		look = exec.LookPath
	}
	for _, name := range []string{"chromium", "chromium-browser"} {
		path, err := look(name)
		if err == nil && strings.TrimSpace(path) != "" {
			return path, nil
		}
	}
	if opts.LookPath != nil {
		return "", errors.New("desktop browser not found")
	}
	for _, path := range []string{"/usr/bin/chromium", "/usr/bin/chromium-browser"} {
		info, err := os.Stat(path)
		if err == nil && !info.IsDir() && info.Mode()&0o111 != 0 {
			return path, nil
		}
	}
	return "", errors.New("desktop browser not found")
}

func ensureDesktopVNCPassword(ctx context.Context, opts desktopInstallOptions, passwdPath string) error {
	if _, err := os.Stat(passwdPath); err == nil {
		return os.Chmod(passwdPath, 0o600)
	} else if !os.IsNotExist(err) {
		return err
	}
	password, err := newDesktopVNCPassword(opts)
	if err != nil {
		return err
	}
	// x11vnc hashes the password into passwdPath. The plaintext exists only
	// as this process argument (visible briefly in /proc/pid/cmdline) and is
	// omitted from the error returned here. VNC DES uses the first 8
	// characters; the 24-byte seed is still what the issue requires.
	if _, err := opts.run(ctx, "x11vnc", "-storepasswd", password, passwdPath); err != nil {
		return errors.New("failed to store the VNC password")
	}
	return os.Chmod(passwdPath, 0o600)
}

func newDesktopVNCPassword(opts desktopInstallOptions) (string, error) {
	read := opts.Random
	if read == nil {
		read = readRandomBytes
	}
	buf, err := read(24)
	if err != nil {
		return "", fmt.Errorf("failed to generate VNC password: %w", err)
	}
	if len(buf) != 24 {
		return "", errors.New("failed to generate VNC password")
	}
	return hex.EncodeToString(buf), nil
}

func readRandomBytes(n int) ([]byte, error) {
	buf := make([]byte, n)
	if _, err := rand.Read(buf); err != nil {
		return nil, err
	}
	return buf, nil
}

func renderDesktopStartScript(passwdPath string) string {
	return "#!/bin/sh\n" +
		"Xvfb :99 -screen 0 1920x1080x24 &\n" +
		"exec x11vnc -display :99 -localhost -rfbport 5900 -rfbauth " +
		shellSingleQuote(passwdPath) +
		" -forever -shared=0 -noipv6\n"
}

func renderDesktopUnit() string {
	return `[Unit]
Description=Preloop headless desktop
After=network.target

[Service]
Type=simple
ExecStart=%h/.preloop/desktop/start.sh
Restart=on-failure

[Install]
WantedBy=default.target
`
}

func writeDesktopFile(path, contents string, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("failed to create %s: %w", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, []byte(contents), mode); err != nil {
		return fmt.Errorf("failed to write %s: %w", path, err)
	}
	if err := os.Chmod(path, mode); err != nil {
		return fmt.Errorf("failed to set permissions on %s: %w", path, err)
	}
	return nil
}

func enableDesktopService(ctx context.Context, opts desktopInstallOptions, out io.Writer, startScript string) error {
	steps := [][]string{
		{"--user", "daemon-reload"},
		{"--user", "enable", "--now", desktopService},
		{"--user", "restart", desktopService},
	}
	for i, args := range steps {
		if _, err := opts.run(ctx, "systemctl", args...); err != nil {
			if i == len(steps)-1 {
				return fmt.Errorf("failed to restart %s: %w", desktopService, err)
			}
			fmt.Fprintln(out, "systemd user session unavailable; started the headless desktop with nohup") //nolint:errcheck
			quoted := shellSingleQuote(startScript)
			_, err := opts.run(ctx, "sh", "-c", "nohup "+quoted+" >/dev/null 2>&1 &")
			if err != nil {
				return fmt.Errorf("failed to start desktop with nohup: %w", err)
			}
			return nil
		}
	}
	return nil
}

type desktopManifest struct {
	Display     string         `json:"display"`
	VNC         desktopVNCInfo `json:"vnc"`
	Browser     string         `json:"browser"`
	InstalledAt string         `json:"installed_at"`
}

type desktopVNCInfo struct {
	Host string `json:"host"`
	Port int    `json:"port"`
	Auth string `json:"auth"`
}

func writeDesktopManifest(home, browser string, now time.Time) error {
	manifest := desktopManifest{
		Display: desktopDisplay,
		VNC: desktopVNCInfo{
			Host: desktopVNCHost,
			Port: desktopVNCPort,
			Auth: "rfbauth",
		},
		Browser:     browser,
		InstalledAt: now.UTC().Format(time.RFC3339),
	}
	data, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	return writeDesktopFile(filepath.Join(home, ".preloop", "desktop.json"), string(data), 0o600)
}

// applyDesktopDisplay exports DISPLAY=:99 where this CLI already persists
// runtime environment. PRELOOP_MODEL_GATEWAY and cli/internal/agents are not
// in this tree. The nearest writer is renderManagedRuntimeEnv, which emits
// export lines under ~/.preloop/agents/runtime. Hermes also reads
// ~/.hermes/.env (hermesEnvFile). OpenClaw reads an env block from its JSON
// config (resolveOpenClawEnvVar) when that file is strict JSON.
func applyDesktopDisplay(home, runtime string) error {
	runtime = strings.ToLower(strings.TrimSpace(runtime))
	if runtime == "" {
		runtime = "runtime"
	}
	envPath := filepath.Join(home, ".preloop", "agents", "runtime", runtime+".env")
	if err := upsertShellExport(envPath, "DISPLAY", desktopDisplay, 0o600); err != nil {
		return err
	}
	switch runtime {
	case "hermes":
		if err := upsertShellExport(filepath.Join(home, ".hermes", ".env"), "DISPLAY", desktopDisplay, 0o600); err != nil {
			return err
		}
	case "openclaw":
		if err := setOpenClawDisplay(home); err != nil {
			return err
		}
	}
	return os.Setenv("DISPLAY", desktopDisplay)
}

func setOpenClawDisplay(home string) error {
	path := filepath.Join(home, ".openclaw", "openclaw.json")
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	if len(strings.TrimSpace(string(data))) == 0 {
		return nil
	}
	var doc map[string]interface{}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil
	}
	if doc == nil {
		doc = map[string]interface{}{}
	}
	envBlock, ok := doc["env"].(map[string]interface{})
	if !ok {
		if _, present := doc["env"]; present {
			return nil
		}
		envBlock = map[string]interface{}{}
		doc["env"] = envBlock
	}
	envBlock["DISPLAY"] = desktopDisplay
	return writeJSONDocument(path, doc)
}

func upsertShellExport(path, key, value string, mode os.FileMode) error {
	data, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	assignment := "export " + key + "=" + shellSingleQuote(value)
	var kept []string
	replaced := false
	if len(data) > 0 {
		text := strings.ReplaceAll(string(data), "\r\n", "\n")
		for _, line := range strings.Split(text, "\n") {
			if shellExportKey(line) == key {
				if !replaced {
					kept = append(kept, assignment)
					replaced = true
				}
				continue
			}
			kept = append(kept, line)
		}
	}
	if !replaced {
		if len(kept) > 0 && kept[len(kept)-1] == "" {
			kept[len(kept)-1] = assignment
		} else {
			kept = append(kept, assignment)
		}
	}
	for len(kept) > 0 && kept[len(kept)-1] == "" {
		kept = kept[:len(kept)-1]
	}
	body := strings.Join(kept, "\n")
	if body != "" {
		body += "\n"
	}
	return writeDesktopFile(path, body, mode)
}

func shellExportKey(line string) string {
	trimmed := strings.TrimSpace(line)
	if trimmed == "" || strings.HasPrefix(trimmed, "#") {
		return ""
	}
	if strings.HasPrefix(trimmed, "export ") {
		trimmed = strings.TrimSpace(strings.TrimPrefix(trimmed, "export "))
	}
	name, _, ok := strings.Cut(trimmed, "=")
	if !ok {
		return ""
	}
	return strings.TrimSpace(name)
}

// loadDesktopStatus reports the desktop block for `agents status --json`.
// A missing desktop.json is a null desktop, not an error.
func loadDesktopStatus() (map[string]interface{}, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(filepath.Join(home, ".preloop", "desktop.json"))
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var manifest desktopManifest
	if err := json.Unmarshal(data, &manifest); err != nil {
		return nil, fmt.Errorf("failed to read desktop status: %w", err)
	}
	return map[string]interface{}{
		"installed": true,
		"display":   manifest.Display,
		"vnc_port":  manifest.VNC.Port,
	}, nil
}
