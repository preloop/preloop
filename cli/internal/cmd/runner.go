package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
)

const (
	runnerStateFile      = "runner.json"
	runnerHeartbeatEvery = 15 * time.Second
)

var runnerCmd = &cobra.Command{
	Use:   "runner",
	Short: "Run Preloop flows on this machine",
	Long: `The Preloop CLI is the self-hosted runner. 'preloop runner fg' keeps an
outbound WebSocket to the configured server, leases flow jobs, and runs them
locally. enable/disable install that process as a system service.`,
}

var runnerFgCmd = &cobra.Command{
	Use:   "fg",
	Short: "Run the runner in the foreground",
	RunE:  runRunnerFg,
}

var runnerEnableCmd = &cobra.Command{
	Use:   "enable",
	Short: "Install a system service for preloop runner fg",
	RunE:  runRunnerEnable,
}

var runnerDisableCmd = &cobra.Command{
	Use:   "disable",
	Short: "Remove the runner system service",
	RunE:  runRunnerDisable,
}

var runnerStartCmd = &cobra.Command{
	Use:   "start",
	Short: "Start the installed runner service",
	RunE:  func(cmd *cobra.Command, args []string) error { return runnerServiceControl("start") },
}

var runnerStopCmd = &cobra.Command{
	Use:   "stop",
	Short: "Stop the installed runner service",
	RunE:  func(cmd *cobra.Command, args []string) error { return runnerServiceControl("stop") },
}

var runnerRestartCmd = &cobra.Command{
	Use:   "restart",
	Short: "Restart the installed runner service",
	RunE:  func(cmd *cobra.Command, args []string) error { return runnerServiceControl("restart") },
}

var runnerStatusCmd = &cobra.Command{
	Use:   "status",
	Short: "Print service state and last known runner heartbeat",
	RunE:  runRunnerStatus,
}

func init() {
	runnerCmd.AddCommand(runnerFgCmd)
	runnerCmd.AddCommand(runnerEnableCmd)
	runnerCmd.AddCommand(runnerDisableCmd)
	runnerCmd.AddCommand(runnerStartCmd)
	runnerCmd.AddCommand(runnerStopCmd)
	runnerCmd.AddCommand(runnerRestartCmd)
	runnerCmd.AddCommand(runnerStatusCmd)
	runnerFgCmd.Flags().StringSlice("labels", nil, "labels used to match runner pools")
	runnerFgCmd.Flags().String("name", "", "runner display name (default: hostname)")
}

type runnerState struct {
	ID    string `json:"id"`
	Token string `json:"token"`
	Name  string `json:"name"`
}

type runnerAPIRecord struct {
	ID                 string   `json:"id"`
	Name               string   `json:"name"`
	Status             string   `json:"status"`
	LastHeartbeat      *string  `json:"last_heartbeat"`
	CurrentExecutionID *string  `json:"current_execution_id"`
	Hostname           string   `json:"hostname"`
	Labels             []string `json:"labels"`
	Token              string   `json:"token"`
}

type runnerWSMessage struct {
	Type            string         `json:"type"`
	Job             map[string]any `json:"job,omitempty"`
	Halt            bool           `json:"halt,omitempty"`
	HaltExecutionID string         `json:"halt_execution_id,omitempty"`
	Error           string         `json:"error,omitempty"`
	RunnerID        string         `json:"runner_id,omitempty"`
}

func runRunnerFg(cmd *cobra.Command, args []string) error {
	labels, _ := cmd.Flags().GetStringSlice("labels")
	name, _ := cmd.Flags().GetString("name")
	hostname, _ := os.Hostname()
	if name == "" {
		name = hostname
	}

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}

	state, err := loadOrRegisterRunner(client, name, hostname, labels)
	if err != nil {
		return err
	}
	fmt.Fprintf(cmd.OutOrStdout(), "Runner %s (%s) connecting...\n", state.Name, state.ID)

	interrupt := make(chan os.Signal, 1)
	signal.Notify(interrupt, os.Interrupt, syscall.SIGTERM)
	return runnerForegroundLoop(state, interrupt, cmd.OutOrStdout())
}

func loadOrRegisterRunner(client *api.Client, name, hostname string, labels []string) (*runnerState, error) {
	req := map[string]any{
		"name":     name,
		"hostname": hostname,
		"os":       runtime.GOOS,
		"arch":     runtime.GOARCH,
		"labels":   labels,
	}
	if existing, err := readRunnerState(); err == nil && existing.ID != "" && existing.Token != "" {
		req["runner_id"] = existing.ID
		var resumed runnerAPIRecord
		if err := client.Post("/api/v1/runners/register", req, &resumed); err == nil && resumed.ID != "" {
			existing.ID = resumed.ID
			if resumed.Name != "" {
				existing.Name = resumed.Name
			}
			if resumed.Token != "" {
				existing.Token = resumed.Token
			}
			_ = writeRunnerState(existing)
			return existing, nil
		}
		delete(req, "runner_id")
	}

	var created runnerAPIRecord
	if err := client.Post("/api/v1/runners/register", req, &created); err != nil {
		return nil, fmt.Errorf("register runner: %w", err)
	}
	state := &runnerState{ID: created.ID, Token: created.Token, Name: created.Name}
	if err := writeRunnerState(state); err != nil {
		return nil, err
	}
	return state, nil
}

type leasedJobOutcome struct {
	executionID string
	status      string
	errMsg      string
	lines       []string
}

func runnerForegroundLoop(state *runnerState, interrupt <-chan os.Signal, out io.Writer) error {
	wsURL, err := runnerWebsocketURL(state.ID)
	if err != nil {
		return err
	}
	conn, _, err := websocket.DefaultDialer.Dial(wsURL, http.Header{
		"User-Agent":     []string{"preloop-cli-runner"},
		"X-Runner-Token": []string{state.Token},
	})
	if err != nil {
		return fmt.Errorf("runner websocket: %w", err)
	}
	defer conn.Close() //nolint:errcheck

	fmt.Fprintf(out, "Connected. Waiting for jobs.\n")

	incoming := make(chan runnerWSMessage, 8)
	readErr := make(chan error, 1)
	go func() {
		for {
			var msg runnerWSMessage
			if err := conn.ReadJSON(&msg); err != nil {
				readErr <- err
				return
			}
			incoming <- msg
		}
	}()

	halt := false
	halted := &atomic.Bool{}
	var runningCmd *exec.Cmd
	var runningExecID string
	var jobDone <-chan leasedJobOutcome
	ticker := time.NewTicker(runnerHeartbeatEvery)
	defer ticker.Stop()

	killRunning := func() {
		if !requestJobHalt(halted, runningCmd) {
			halt = false
		}
	}

	for {
		select {
		case <-interrupt:
			fmt.Fprintf(out, "Unregistering...\n")
			killRunning()
			_ = conn.WriteJSON(map[string]any{"type": "unregister"})
			return nil
		case <-ticker.C:
			if err := conn.WriteJSON(map[string]any{"type": "heartbeat"}); err != nil {
				return fmt.Errorf("heartbeat: %w", err)
			}
		case err := <-readErr:
			if websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
				return nil
			}
			return fmt.Errorf("runner read: %w", err)
		case outcome := <-jobDone:
			if len(outcome.lines) > 0 {
				_ = conn.WriteJSON(map[string]any{
					"type":         "logs",
					"execution_id": outcome.executionID,
					"lines":        outcome.lines,
				})
			}
			_ = conn.WriteJSON(map[string]any{
				"type":         "complete",
				"execution_id": outcome.executionID,
				"status":       outcome.status,
				"error":        outcome.errMsg,
			})
			runningCmd = nil
			runningExecID = ""
			jobDone = nil
			halt = false
			halted.Store(false)
		case msg := <-incoming:
			if msg.Error != "" {
				return fmt.Errorf("runner server: %s", msg.Error)
			}
			if msg.Halt || msg.Type == "halt" {
				halt = true
				fmt.Fprintf(out, "Halt received for %s\n", msg.HaltExecutionID)
				killRunning()
				continue
			}
			if msg.Job == nil {
				continue
			}
			if runningExecID != "" {
				fmt.Fprintf(out, "Ignoring job while %s is running\n", runningExecID)
				continue
			}
			if err := beginLeasedJob(conn, msg.Job, halt, out, &runningCmd, &runningExecID, &jobDone, halted); err != nil {
				fmt.Fprintf(out, "Job error: %v\n", err)
				runningCmd = nil
				runningExecID = ""
				jobDone = nil
			}
			halt = false
		}
	}
}

func beginLeasedJob(
	conn *websocket.Conn,
	job map[string]any,
	alreadyHalted bool,
	out io.Writer,
	runningCmd **exec.Cmd,
	runningExecID *string,
	jobDone *<-chan leasedJobOutcome,
	halted *atomic.Bool,
) error {
	executionID, _ := job["execution_id"].(string)
	if executionID == "" {
		return fmt.Errorf("job missing execution_id")
	}
	if halted != nil {
		halted.Store(false)
	}
	fmt.Fprintf(out, "Leased execution %s\n", executionID)
	_ = conn.WriteJSON(map[string]any{
		"type":         "status",
		"execution_id": executionID,
		"status":       "RUNNING",
	})
	_ = conn.WriteJSON(map[string]any{
		"type":         "logs",
		"execution_id": executionID,
		"lines":        []string{"runner leased job " + executionID},
	})
	if alreadyHalted {
		return conn.WriteJSON(map[string]any{
			"type":         "complete",
			"execution_id": executionID,
			"status":       "STOPPED",
		})
	}

	image := runnerImageFromJob(job)
	dockerOK := image != "" && dockerAvailable()
	if reason := leasedJobFailureReason(job, dockerOK); reason != "" {
		_ = conn.WriteJSON(map[string]any{
			"type":         "logs",
			"execution_id": executionID,
			"lines":        []string{reason},
		})
		return conn.WriteJSON(map[string]any{
			"type":         "complete",
			"execution_id": executionID,
			"status":       "FAILED",
			"error":        reason,
		})
	}

	cmd := exec.Command("docker", "run", "--rm", image)
	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	if err := cmd.Start(); err != nil {
		return conn.WriteJSON(map[string]any{
			"type":         "complete",
			"execution_id": executionID,
			"status":       "FAILED",
			"error":        err.Error(),
		})
	}
	*runningCmd = cmd
	*runningExecID = executionID
	done := make(chan leasedJobOutcome, 1)
	*jobDone = done
	go func() {
		done <- waitDockerJob(cmd, executionID, &buf, halted)
	}()
	return nil
}

// requestJobHalt latches halted only when a docker job is actually
// running. An idle halt, or a halt after the job already finished,
// clears the latch so a later lease can start.
func requestJobHalt(halted *atomic.Bool, runningCmd *exec.Cmd) bool {
	if runningCmd != nil && runningCmd.Process != nil {
		if halted != nil {
			halted.Store(true)
		}
		_ = runningCmd.Process.Kill()
		return true
	}
	if halted != nil {
		halted.Store(false)
	}
	return false
}

func waitDockerJob(cmd *exec.Cmd, executionID string, buf *bytes.Buffer, halted *atomic.Bool) leasedJobOutcome {
	err := cmd.Wait()
	lines := splitNonEmptyLines(buf.String())
	if err != nil {
		if halted != nil && halted.Load() {
			return leasedJobOutcome{executionID: executionID, status: "STOPPED", lines: lines}
		}
		return leasedJobOutcome{executionID: executionID, status: "FAILED", errMsg: err.Error(), lines: lines}
	}
	return leasedJobOutcome{executionID: executionID, status: "SUCCEEDED", lines: lines}
}

func splitNonEmptyLines(output string) []string {
	trimmed := strings.TrimSpace(output)
	if trimmed == "" {
		return nil
	}
	return strings.Split(trimmed, "\n")
}

func leasedJobFailureReason(job map[string]any, dockerOK bool) string {
	if runnerImageFromJob(job) == "" {
		return "no agent image in payload"
	}
	if !dockerOK {
		return "docker is not available"
	}
	return ""
}

func runnerImageFromJob(job map[string]any) string {
	cfg, _ := job["agent_config"].(map[string]any)
	if cfg == nil {
		return ""
	}
	if image, ok := cfg["image"].(string); ok {
		return image
	}
	if image, ok := cfg["docker_image"].(string); ok {
		return image
	}
	return ""
}

func dockerAvailable() bool {
	cmd := exec.Command("docker", "info")
	return cmd.Run() == nil
}

func runnerWebsocketURL(runnerID string) (string, error) {
	cfg, err := config.Resolve(FlagToken, FlagURL)
	if err != nil {
		return "", err
	}
	base := strings.TrimRight(cfg.APIURL, "/")
	u, err := url.Parse(base)
	if err != nil {
		return "", err
	}
	switch u.Scheme {
	case "https":
		u.Scheme = "wss"
	default:
		u.Scheme = "ws"
	}
	u.Path = "/api/v1/runners/" + runnerID + "/ws"
	u.RawQuery = ""
	return u.String(), nil
}

func runnerStatePath() (string, error) {
	dir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, runnerStateFile), nil
}

func readRunnerState() (*runnerState, error) {
	path, err := runnerStatePath()
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var state runnerState
	if err := json.Unmarshal(data, &state); err != nil {
		return nil, err
	}
	return &state, nil
}

func writeRunnerState(state *runnerState) error {
	path, err := runnerStatePath()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0o600)
}

func runRunnerEnable(cmd *cobra.Command, args []string) error {
	bin, err := os.Executable()
	if err != nil {
		return err
	}
	switch runtime.GOOS {
	case "darwin":
		return writeLaunchdPlist(bin)
	case "linux":
		return writeSystemdUserUnit(bin)
	case "windows":
		return writeWindowsScheduledTask(bin)
	default:
		return fmt.Errorf("service install is not implemented on %s; use preloop runner fg", runtime.GOOS)
	}
}

func runRunnerDisable(cmd *cobra.Command, args []string) error {
	_ = runnerServiceControl("stop")
	switch runtime.GOOS {
	case "darwin":
		return os.Remove(launchdPlistPath())
	case "linux":
		return os.Remove(systemdUserUnitPath())
	case "windows":
		return exec.Command("schtasks", "/Delete", "/TN", "PreloopRunner", "/F").Run()
	default:
		return fmt.Errorf("service install is not implemented on %s", runtime.GOOS)
	}
}

func runRunnerStatus(cmd *cobra.Command, args []string) error {
	fmt.Fprintf(cmd.OutOrStdout(), "install: %s\n", runnerServiceState())
	if state, err := readRunnerState(); err == nil {
		fmt.Fprintf(cmd.OutOrStdout(), "runner_id: %s\n", state.ID)
		fmt.Fprintf(cmd.OutOrStdout(), "name: %s\n", state.Name)
	}
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return nil
	}
	var runners []runnerAPIRecord
	if err := client.Get("/api/v1/runners", &runners); err != nil {
		return nil
	}
	state, _ := readRunnerState()
	for _, row := range runners {
		if state != nil && row.ID == state.ID {
			fmt.Fprintf(cmd.OutOrStdout(), "status: %s\n", row.Status)
			if row.LastHeartbeat != nil {
				fmt.Fprintf(cmd.OutOrStdout(), "last_heartbeat: %s\n", *row.LastHeartbeat)
			}
			if row.CurrentExecutionID != nil {
				fmt.Fprintf(cmd.OutOrStdout(), "current_execution: %s\n", *row.CurrentExecutionID)
			}
		}
	}
	return nil
}

func launchdPlistPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, "Library", "LaunchAgents", "ai.preloop.runner.plist")
}

func systemdUserUnitPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".config", "systemd", "user", "preloop-runner.service")
}

func writeLaunchdPlist(bin string) error {
	path := launchdPlistPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	body := fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.preloop.runner</string>
  <key>ProgramArguments</key>
  <array><string>%s</string><string>runner</string><string>fg</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
`, bin)
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		return err
	}
	_ = exec.Command("launchctl", "load", path).Run()
	fmt.Printf("Installed %s\n", path)
	return nil
}

func writeSystemdUserUnit(bin string) error {
	path := systemdUserUnitPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	body := fmt.Sprintf(`[Unit]
Description=Preloop self-hosted runner
After=network-online.target

[Service]
ExecStart=%s runner fg
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
`, bin)
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		return err
	}
	_ = exec.Command("systemctl", "--user", "daemon-reload").Run()
	_ = exec.Command("systemctl", "--user", "enable", "preloop-runner.service").Run()
	fmt.Printf("Installed %s\n", path)
	return nil
}

func writeWindowsScheduledTask(bin string) error {
	cmd := exec.Command(
		"schtasks",
		"/Create",
		"/TN", "PreloopRunner",
		"/TR", fmt.Sprintf(`"%s" runner fg`, bin),
		"/SC", "ONLOGON",
		"/RL", "LIMITED",
		"/F",
	)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("schtasks: %w (%s)", err, strings.TrimSpace(string(out)))
	}
	fmt.Println("Installed scheduled task PreloopRunner")
	return nil
}

func runnerServiceControl(action string) error {
	switch runtime.GOOS {
	case "darwin":
		path := launchdPlistPath()
		switch action {
		case "start":
			return exec.Command("launchctl", "load", path).Run()
		case "stop":
			return exec.Command("launchctl", "unload", path).Run()
		case "restart":
			_ = exec.Command("launchctl", "unload", path).Run()
			return exec.Command("launchctl", "load", path).Run()
		}
	case "linux":
		unit := "preloop-runner.service"
		switch action {
		case "start", "stop", "restart":
			return exec.Command("systemctl", "--user", action, unit).Run()
		}
	case "windows":
		switch action {
		case "start":
			return exec.Command("schtasks", "/Run", "/TN", "PreloopRunner").Run()
		case "stop":
			return exec.Command("schtasks", "/End", "/TN", "PreloopRunner").Run()
		case "restart":
			_ = exec.Command("schtasks", "/End", "/TN", "PreloopRunner").Run()
			return exec.Command("schtasks", "/Run", "/TN", "PreloopRunner").Run()
		}
	}
	return fmt.Errorf("service %s is not implemented on %s", action, runtime.GOOS)
}

func runnerServiceState() string {
	switch runtime.GOOS {
	case "darwin":
		out, err := exec.Command("launchctl", "list", "ai.preloop.runner").CombinedOutput()
		if err != nil {
			return "not loaded"
		}
		return strings.TrimSpace(string(out))
	case "linux":
		out, err := exec.Command("systemctl", "--user", "is-active", "preloop-runner.service").CombinedOutput()
		if err != nil {
			return "inactive"
		}
		return strings.TrimSpace(string(out))
	case "windows":
		out, err := exec.Command("schtasks", "/Query", "/TN", "PreloopRunner").CombinedOutput()
		if err != nil {
			return "not installed"
		}
		return strings.TrimSpace(string(out))
	default:
		return "unsupported"
	}
}
