package cmd

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"sort"
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
	// The sidecar owns the control socket; without it there is no remote
	// control and the dial below can never succeed. Stop with one actionable
	// error instead of warning and then failing on the dial.
	if err := ensureClaudeSidecarRunning(cmd.OutOrStdout()); err != nil {
		return fmt.Errorf("cannot start the Claude Code sidecar: %w", err)
	}
	printClaudePairingHint(cmd.OutOrStdout())

	cwd, err := os.Getwd()
	if err != nil {
		return err
	}
	conn, err := dialClaudeControlSocket(8 * time.Second)
	if err != nil {
		return fmt.Errorf(
			"claude sidecar started but did not open %s: %w (check %s)",
			claudeControlSocketPath(),
			err,
			claudeSidecarLogPath(),
		)
	}
	defer conn.Close()

	incoming := make(chan claudeIPCMessage, 8)
	go readClaudeIPC(conn, incoming)

	_ = writeClaudeIPC(conn, claudeIPCMessage{Type: "hello", Mode: "local", Cwd: cwd})
	_ = writeClaudeIPC(conn, claudeIPCMessage{Type: "local_ready", Cwd: cwd})

	signals := make(chan os.Signal, 1)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(signals)

	return runClaudeLauncherLoop(claudeLauncherLoop{
		out:        cmd.OutOrStdout(),
		conn:       conn,
		incoming:   incoming,
		signals:    signals,
		spawn:      startClaudeTUI,
		args:       args,
		waitRemote: waitForAnyKeyOrRelease,
	})
}

// claudeLauncherLoop wires the launcher event loop to its environment so the
// loop itself is testable without a terminal or a real claude binary.
type claudeLauncherLoop struct {
	out        io.Writer
	conn       net.Conn
	incoming   chan claudeIPCMessage
	signals    chan os.Signal
	spawn      func(extra []string, resumeSessionID string) (*exec.Cmd, error)
	args       []string
	waitRemote func(incoming <-chan claudeIPCMessage, signals <-chan os.Signal)
}

// runClaudeLauncherLoop owns exactly one live TUI child at a time. The TUI is
// respawned only after this loop itself terminated the previous child (switch
// to remote, or an explicit release). Informational IPC frames ("status",
// "session") must NOT respawn: the sidecar sends a status broadcast for every
// hello/local_ready, and respawning per frame stacked up multiple `claude`
// children that all stopped on SIGTTIN (the silent-launcher bug).
func runClaudeLauncherLoop(loop claudeLauncherLoop) error {
	sessionID := ""
	for {
		child, startErr := loop.spawn(loop.args, sessionID)
		if startErr != nil {
			return startErr
		}
		childDone := make(chan error, 1)
		go func() { childDone <- child.Wait() }()

		respawn := false
		for !respawn {
			select {
			case <-loop.signals:
				_ = terminateProcess(child, childDone)
				return nil
			case waitErr := <-childDone:
				if waitErr != nil && !isExpectedClaudeExit(waitErr) {
					return waitErr
				}
				return nil
			case msg := <-loop.incoming:
				switch msg.Type {
				case "switch":
					_ = terminateProcess(child, childDone)
					_ = writeClaudeIPC(loop.conn, claudeIPCMessage{Type: "switched", SessionID: sessionID})
					if msg.SessionID != "" {
						sessionID = msg.SessionID
					}
					fmt.Fprintln(loop.out, "Remote. Press any key to return.")
					loop.waitRemote(loop.incoming, loop.signals)
					_ = writeClaudeIPC(loop.conn, claudeIPCMessage{Type: "release", SessionID: sessionID})
					respawn = true
				case "release":
					_ = terminateProcess(child, childDone)
					if msg.SessionID != "" {
						sessionID = msg.SessionID
					}
					respawn = true
				case "status", "session":
					// Bookkeeping only; the current TUI keeps running.
					if msg.SessionID != "" {
						sessionID = msg.SessionID
					}
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
	// No SysProcAttr on purpose: the TUI must inherit the launcher's process
	// group, which is the terminal's foreground group. Spawning it with
	// Setpgid put it in a BACKGROUND group, so its first tty read delivered
	// SIGTTIN and the TUI sat stopped (state T) forever while the launcher
	// waited silently. Detaching is only for the sidecar daemon.
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
			defer func() { _ = term.Restore(fd, old) }()
		}
	}
	waitForStdinOrRelease(fd, incoming, signals)
}

func waitForStdinOrRelease(fd int, incoming <-chan claudeIPCMessage, signals <-chan os.Signal) {
	// Poll the fd with a timeout instead of a blocking Read goroutine.
	// SetReadDeadline is unsupported on os.Stdin, and SetNonblock cannot
	// interrupt a read already sitting in the kernel, so that leftover
	// goroutine would steal the first byte from the restarted TUI.
	for {
		ready, err := stdinByteReady(fd, 50*time.Millisecond)
		if err == nil && ready {
			consumeStdinByte(fd)
			return
		}
		select {
		case <-signals:
			return
		case msg := <-incoming:
			if msg.Type == "release" || msg.Type == "released" {
				return
			}
		default:
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
	return ensureAgentControlSidecarRunning(claudeAgentControlSidecarSpec(), out)
}

func startClaudeSidecarProcess() error {
	// The shared starter records the launch in the sidecar log so a 0-byte
	// file is not ambiguous with "never ran".
	return startAgentControlSidecarProcess(claudeAgentControlSidecarSpec())
}

func claudeSidecarLogPath() string {
	return claudeAgentControlSidecarSpec().logPath()
}

// claudeSidecarInvocation describes how to start the sidecar: either the
// preloop-claude-plugin bin directly, or node with the package entry point
// when npm never linked the bin.
type claudeSidecarInvocation struct {
	bin  string
	args []string
}

// resolveClaudeSidecarInvocation locates the sidecar executable. npm skips
// creating the preloop-claude-plugin bin link when the package was installed
// from a source checkout whose dist/ was not built yet, so a missing bin does
// not mean the package is missing. Fall back to the package entry point under
// the npm global root before telling the user to onboard.
func resolveClaudeSidecarInvocation() (claudeSidecarInvocation, error) {
	return resolveAgentControlSidecarInvocation(claudeAgentControlSidecarSpec())
}

// findClaudeSidecarPackageEntry looks for @preloop-ai/claude-plugin under the
// given npm global node_modules roots. Returns the dist entry point when the
// package is installed and built. When the package directory exists but the
// build output is missing (a source-folder install that never ran a build),
// return an error that names the broken install instead of a generic
// not-found message.
func findClaudeSidecarPackageEntry(roots []string) (string, bool, error) {
	return findAgentControlSidecarPackageEntry(claudeAgentControlSidecarSpec(), roots)
}

// claudeNpmGlobalRootsFunc is a seam for tests: claudeNpmGlobalRoots probes
// fixed absolute prefixes (for example /opt/homebrew/lib/node_modules), so a
// machine with the plugin genuinely installed there would leak into tests
// that pin PATH and HOME to temp dirs.
var claudeNpmGlobalRootsFunc = claudeNpmGlobalRoots

// claudeNpmGlobalRoots returns candidate npm global node_modules directories:
// whatever npm itself reports, plus common prefixes for machines where npm is
// unavailable or misconfigured.
func claudeNpmGlobalRoots() []string {
	roots := []string{}
	if npmBin, err := resolveRuntimeExecutable("npm"); err == nil {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		output, cmdErr := exec.CommandContext(ctx, npmBin, "root", "-g").Output()
		cancel()
		if cmdErr == nil {
			if root := strings.TrimSpace(string(output)); root != "" {
				roots = append(roots, root)
			}
		}
	}
	if home, err := os.UserHomeDir(); err == nil {
		roots = append(roots,
			filepath.Join(home, ".npm-global", "lib", "node_modules"),
		)
		if matches, globErr := filepath.Glob(
			filepath.Join(home, ".nvm", "versions", "node", "*", "lib", "node_modules"),
		); globErr == nil {
			sort.Sort(sort.Reverse(sort.StringSlice(matches)))
			roots = append(roots, matches...)
		}
	}
	roots = append(roots,
		"/opt/homebrew/lib/node_modules",
		"/usr/local/lib/node_modules",
	)
	seen := map[string]bool{}
	unique := roots[:0]
	for _, root := range roots {
		if root == "" || seen[root] {
			continue
		}
		seen[root] = true
		unique = append(unique, root)
	}
	return unique
}

func claudeSidecarLaunchdPath() string {
	return claudeAgentControlSidecarSpec().launchdPath()
}

func claudeSidecarSystemdPath() string {
	return claudeAgentControlSidecarSpec().systemdPath()
}

func runClaudeSidecarEnable(cmd *cobra.Command, args []string) error {
	return runAgentControlSidecarEnable(claudeAgentControlSidecarSpec(), cmd, args)
}

func runClaudeSidecarDisable(cmd *cobra.Command, args []string) error {
	return runAgentControlSidecarDisable(claudeAgentControlSidecarSpec(), cmd, args)
}

func runClaudeSidecarStatus(cmd *cobra.Command, args []string) error {
	return runAgentControlSidecarStatus(claudeAgentControlSidecarSpec(), cmd, args)
}

func runClaudeSidecarForeground(cmd *cobra.Command, args []string) error {
	return runAgentControlSidecarForeground(claudeAgentControlSidecarSpec(), cmd, args)
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
	return writeAgentControlSidecarLaunchd(claudeAgentControlSidecarSpec(), bin, out)
}

func writeClaudeSidecarSystemd(bin string, out io.Writer) error {
	return writeAgentControlSidecarSystemd(claudeAgentControlSidecarSpec(), bin, out)
}
