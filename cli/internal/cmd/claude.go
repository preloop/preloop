package cmd

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"

	"github.com/spf13/cobra"
	"golang.org/x/term"

	"github.com/preloop/preloop/cli/internal/config"
)

// preloop claude: Happy-class launcher. Native TUI locally, Agent SDK when a
// remote surface takes over, any-key or Release returns to the TUI.
var claudeCmd = &cobra.Command{
	Use:   "claude [flags] [-- claude-args...]",
	Short: "Run Claude Code under Preloop Agent Control",
	Long: `Run Claude Code with Happy-class remote control.

Local: the native Claude TUI. Remote: phone, web console, or watch takes
over through the Agent SDK sidecar. Messages sent while Local queue and
switch the session to Remote. Press any key (or Release on a remote
surface) to return to the TUI.

Start Claude through this command (or a post-onboard alias). Raw claude
still has approvals, but cannot be steered.

  preloop claude
  preloop claude sidecar enable
  preloop claude sidecar status
`,
	Args: cobra.ArbitraryArgs,
	RunE: runClaudeLauncher,
}

var claudeSidecarCmd = &cobra.Command{
	Use:   "sidecar",
	Short: "Manage the durable Claude Code Agent Control sidecar",
}

var claudeSidecarEnableCmd = &cobra.Command{
	Use:   "enable",
	Short: "Install launchd/systemd so the sidecar stays up",
	RunE:  runClaudeSidecarEnable,
}

var claudeSidecarDisableCmd = &cobra.Command{
	Use:   "disable",
	Short: "Remove the durable sidecar service",
	RunE:  runClaudeSidecarDisable,
}

var claudeSidecarStatusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show whether the sidecar service is installed",
	RunE:  runClaudeSidecarStatus,
}

var claudeSidecarRunCmd = &cobra.Command{
	Use:    "run",
	Short:  "Run the sidecar in the foreground (used by launchd/systemd)",
	Hidden: true,
	RunE:   runClaudeSidecarForeground,
}

func init() {
	claudeSidecarCmd.AddCommand(claudeSidecarEnableCmd)
	claudeSidecarCmd.AddCommand(claudeSidecarDisableCmd)
	claudeSidecarCmd.AddCommand(claudeSidecarStatusCmd)
	claudeSidecarCmd.AddCommand(claudeSidecarRunCmd)
	claudeCmd.AddCommand(claudeSidecarCmd)
	rootCmd.AddCommand(claudeCmd)
}

type claudeIPCMessage struct {
	Type      string `json:"type"`
	Mode      string `json:"mode,omitempty"`
	SessionID string `json:"session_id,omitempty"`
	Cwd       string `json:"cwd,omitempty"`
}

func claudeControlSocketPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".preloop", "claude-control.sock")
}

func claudeControlConfigPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".claude", "preloop-control.json")
}

func runClaudeLauncher(cmd *cobra.Command, args []string) error {
	if err := ensureClaudeSidecarRunning(cmd.OutOrStdout()); err != nil {
		fmt.Fprintf(cmd.ErrOrStderr(), "Warning: sidecar: %v\n", err)
	}
	printClaudePairingHint(cmd.OutOrStdout())

	cwd, err := os.Getwd()
	if err != nil {
		return err
	}
	conn, err := dialClaudeControlSocket(8 * time.Second)
	if err != nil {
		return fmt.Errorf("claude sidecar is not listening (%s): %w", claudeControlSocketPath(), err)
	}
	defer conn.Close()

	sessionID := ""
	mode := "local"
	incoming := make(chan claudeIPCMessage, 8)
	go readClaudeIPC(conn, incoming)

	_ = writeClaudeIPC(conn, claudeIPCMessage{Type: "hello", Mode: "local", Cwd: cwd})
	_ = writeClaudeIPC(conn, claudeIPCMessage{Type: "local_ready", Cwd: cwd})

	signals := make(chan os.Signal, 1)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(signals)

	for {
		child, startErr := startClaudeTUI(args, sessionID)
		if startErr != nil {
			return startErr
		}
		childDone := make(chan error, 1)
		go func() { childDone <- child.Wait() }()

		select {
		case <-signals:
			_ = terminateProcess(child, childDone)
			return nil
		case waitErr := <-childDone:
			if waitErr != nil && !isExpectedClaudeExit(waitErr) {
				return waitErr
			}
			return nil
		case msg := <-incoming:
			switch msg.Type {
			case "switch":
				_ = terminateProcess(child, childDone)
				_ = writeClaudeIPC(conn, claudeIPCMessage{Type: "switched", SessionID: sessionID})
				mode = "remote"
				if msg.SessionID != "" {
					sessionID = msg.SessionID
				}
				fmt.Fprintln(cmd.OutOrStdout(), "Remote. Press any key to return.")
				waitForAnyKeyOrRelease(incoming, signals)
				_ = writeClaudeIPC(conn, claudeIPCMessage{Type: "release", SessionID: sessionID})
				mode = "local"
			case "release":
				_ = terminateProcess(child, childDone)
				if msg.SessionID != "" {
					sessionID = msg.SessionID
				}
				mode = "local"
			case "status":
				if msg.SessionID != "" {
					sessionID = msg.SessionID
				}
				if msg.Mode != "" {
					mode = msg.Mode
				}
				_ = mode
			case "session":
				if msg.SessionID != "" {
					sessionID = msg.SessionID
				}
			}
		}
	}
}

func startClaudeTUI(extra []string, resumeSessionID string) (*exec.Cmd, error) {
	bin, err := resolveRuntimeExecutable("claude")
	if err != nil {
		return nil, fmt.Errorf("claude not found on PATH: %w", err)
	}
	args := append([]string{}, extra...)
	if resumeSessionID != "" {
		args = append([]string{"--resume", resumeSessionID}, args...)
	}
	cmd := exec.Command(bin, args...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.SysProcAttr = claudeSysProcAttr()
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func waitForAnyKeyOrRelease(incoming <-chan claudeIPCMessage, signals <-chan os.Signal) {
	fd := int(os.Stdin.Fd())
	var old *term.State
	if term.IsTerminal(fd) {
		state, err := term.MakeRaw(fd)
		if err == nil {
			old = state
		}
	}
	key := make(chan struct{}, 1)
	readerDone := make(chan struct{})
	go func() {
		defer close(readerDone)
		var b [1]byte
		_, _ = os.Stdin.Read(b[:])
		select {
		case key <- struct{}{}:
		default:
		}
	}()
	defer func() {
		cancelStdinRead(fd)
		select {
		case <-readerDone:
		case <-time.After(100 * time.Millisecond):
		}
		restoreStdinRead(fd)
		if old != nil {
			_ = term.Restore(fd, old)
		}
	}()
	for {
		select {
		case <-key:
			return
		case <-signals:
			return
		case msg := <-incoming:
			if msg.Type == "release" || msg.Type == "released" {
				return
			}
		}
	}
}

func dialClaudeControlSocket(timeout time.Duration) (net.Conn, error) {
	deadline := time.Now().Add(timeout)
	var last error
	for time.Now().Before(deadline) {
		conn, err := net.DialTimeout("unix", claudeControlSocketPath(), 500*time.Millisecond)
		if err == nil {
			return conn, nil
		}
		last = err
		time.Sleep(250 * time.Millisecond)
	}
	return nil, last
}

func writeClaudeIPC(conn net.Conn, msg claudeIPCMessage) error {
	data, err := json.Marshal(msg)
	if err != nil {
		return err
	}
	_, err = conn.Write(append(data, '\n'))
	return err
}

func readClaudeIPC(conn net.Conn, out chan<- claudeIPCMessage) {
	scanner := bufio.NewScanner(conn)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var msg claudeIPCMessage
		if err := json.Unmarshal([]byte(line), &msg); err != nil {
			continue
		}
		out <- msg
	}
}

func terminateProcess(cmd *exec.Cmd, wait <-chan error) error {
	if cmd == nil || cmd.Process == nil {
		return nil
	}
	return terminateClaudeProcess(cmd, wait)
}

func isExpectedClaudeExit(err error) bool {
	if err == nil {
		return true
	}
	if _, ok := err.(*exec.ExitError); ok {
		return true
	}
	return false
}

func printClaudePairingHint(out io.Writer) {
	cfg, err := config.Load()
	base := config.DefaultAPIURL
	if err == nil && strings.TrimSpace(cfg.APIURL) != "" {
		base = strings.TrimRight(cfg.APIURL, "/")
	}
	url := base + "/console/agents"
	fmt.Fprintf(out, "Preloop Claude Control\nPair: %s\n", url)
	if qrencode, lookErr := exec.LookPath("qrencode"); lookErr == nil {
		cmd := exec.Command(qrencode, "-t", "ANSIUTF8", url)
		cmd.Stdout = out
		cmd.Stderr = io.Discard
		_ = cmd.Run()
	}
}

func ensureClaudeSidecarRunning(out io.Writer) error {
	if _, err := os.Stat(claudeControlSocketPath()); err == nil {
		if conn, dialErr := net.DialTimeout("unix", claudeControlSocketPath(), 200*time.Millisecond); dialErr == nil {
			_ = conn.Close()
			return nil
		}
	}
	fmt.Fprintln(out, "Starting Claude Code sidecar...")
	return startClaudeSidecarProcess()
}

func startClaudeSidecarProcess() error {
	bin, err := resolveRuntimeExecutable("preloop-claude-plugin")
	if err != nil {
		return fmt.Errorf("preloop-claude-plugin not found; run preloop agents onboard \"Claude Code\"")
	}
	cmd := exec.Command(bin, "run", "--config", claudeControlConfigPath())
	cmd.SysProcAttr = claudeSysProcAttr()
	logDir := filepath.Join(filepath.Dir(claudeControlSocketPath()), "logs")
	_ = os.MkdirAll(logDir, 0o700)
	stdout, err := os.OpenFile(filepath.Join(logDir, "claude-sidecar.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
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

func claudeSidecarLaunchdPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, "Library", "LaunchAgents", "ai.preloop.claude-sidecar.plist")
}

func claudeSidecarSystemdPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".config", "systemd", "user", "preloop-claude-sidecar.service")
}

func runClaudeSidecarEnable(cmd *cobra.Command, args []string) error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	switch runtime.GOOS {
	case "darwin":
		return writeClaudeSidecarLaunchd(self, cmd.OutOrStdout())
	case "linux":
		return writeClaudeSidecarSystemd(self, cmd.OutOrStdout())
	default:
		return fmt.Errorf("sidecar service install is not implemented on %s; use preloop claude sidecar run", runtime.GOOS)
	}
}

func runClaudeSidecarDisable(cmd *cobra.Command, args []string) error {
	switch runtime.GOOS {
	case "darwin":
		path := claudeSidecarLaunchdPath()
		_ = exec.Command("launchctl", "unload", path).Run()
		return os.Remove(path)
	case "linux":
		_ = exec.Command("systemctl", "--user", "disable", "--now", "preloop-claude-sidecar.service").Run()
		return os.Remove(claudeSidecarSystemdPath())
	default:
		return fmt.Errorf("sidecar service install is not implemented on %s", runtime.GOOS)
	}
}

func runClaudeSidecarStatus(cmd *cobra.Command, args []string) error {
	path := claudeSidecarLaunchdPath()
	if runtime.GOOS == "linux" {
		path = claudeSidecarSystemdPath()
	}
	if _, err := os.Stat(path); err == nil {
		fmt.Fprintf(cmd.OutOrStdout(), "install: present (%s)\n", path)
	} else {
		fmt.Fprintln(cmd.OutOrStdout(), "install: missing")
	}
	if conn, err := net.DialTimeout("unix", claudeControlSocketPath(), 200*time.Millisecond); err == nil {
		_ = conn.Close()
		fmt.Fprintln(cmd.OutOrStdout(), "socket: listening")
	} else {
		fmt.Fprintln(cmd.OutOrStdout(), "socket: down")
	}
	return nil
}

func runClaudeSidecarForeground(cmd *cobra.Command, args []string) error {
	bin, err := resolveRuntimeExecutable("preloop-claude-plugin")
	if err != nil {
		return fmt.Errorf("preloop-claude-plugin not found: %w", err)
	}
	child := exec.Command(bin, "run", "--config", claudeControlConfigPath())
	child.Stdout = cmd.OutOrStdout()
	child.Stderr = cmd.ErrOrStderr()
	return child.Run()
}

func xmlEscapeAttr(value string) string {
	replacer := strings.NewReplacer(
		"&", "&amp;",
		"<", "&lt;",
		">", "&gt;",
		`"`, "&quot;",
		"'", "&apos;",
	)
	return replacer.Replace(value)
}

func writeClaudeSidecarLaunchd(bin string, out io.Writer) error {
	path := claudeSidecarLaunchdPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	escaped := xmlEscapeAttr(bin)
	body := fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.preloop.claude-sidecar</string>
  <key>ProgramArguments</key>
  <array><string>%s</string><string>claude</string><string>sidecar</string><string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
`, escaped)
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		return err
	}
	_ = exec.Command("launchctl", "load", path).Run()
	fmt.Fprintf(out, "Installed %s\n", path)
	return nil
}

func writeClaudeSidecarSystemd(bin string, out io.Writer) error {
	path := claudeSidecarSystemdPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	quoted, _ := json.Marshal(bin)
	body := fmt.Sprintf(`[Unit]
Description=Preloop Claude Code Agent Control sidecar
After=network-online.target

[Service]
ExecStart=%s claude sidecar run
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
`, string(quoted))
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		return err
	}
	_ = exec.Command("systemctl", "--user", "daemon-reload").Run()
	_ = exec.Command("systemctl", "--user", "enable", "--now", "preloop-claude-sidecar.service").Run()
	fmt.Fprintf(out, "Installed %s\n", path)
	return nil
}
