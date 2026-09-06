package cmd

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"sort"
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
	runnerPingWait       = 5 * time.Second
)

var (
	runnerReconnectMin = time.Second
	runnerReconnectMax = 30 * time.Second
	runnerReadWait     = 45 * time.Second
	runnerHasDocker    = dockerAvailable
	newRunnerJobCmd    = defaultNewRunnerJobCmd
)

// runnerFatalError stops the process (auth/server rejection). Transport
// drops reconnect instead.
type runnerFatalError struct{ error }

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
	logBuffer   *runnerLogBuffer
	result      map[string]any
	exitCode    int
	executionID string
	status      string
	errMsg      string
	lines       []string
}

func nextRunnerBackoff(current time.Duration) time.Duration {
	next := current * 2
	if next > runnerReconnectMax {
		return runnerReconnectMax
	}
	if next < runnerReconnectMin {
		return runnerReconnectMin
	}
	return next
}

func waitOrInterrupt(interrupt <-chan os.Signal, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-interrupt:
		return false
	case <-timer.C:
		return true
	}
}

func dialRunnerWebsocket(wsURL, token string) (*websocket.Conn, error) {
	conn, _, err := websocket.DefaultDialer.Dial(wsURL, http.Header{
		"User-Agent":     []string{"preloop-cli-runner"},
		"X-Runner-Token": []string{token},
	})
	if err != nil {
		return nil, err
	}
	return conn, nil
}

func writeJobOutcome(conn *websocket.Conn, outcome leasedJobOutcome) error {
	if conn == nil {
		return nil
	}
	if err := flushRunnerLogs(conn, outcome.executionID, outcome.logBuffer, true); err != nil {
		return err
	}
	if len(outcome.lines) > 0 {
		if err := conn.WriteJSON(map[string]any{
			"type":         "logs",
			"execution_id": outcome.executionID,
			"lines":        outcome.lines,
		}); err != nil {
			return err
		}
	}
	return conn.WriteJSON(map[string]any{
		"type":                "complete",
		"launch_version":      runnerLaunchVersion,
		"completion_protocol": "docker_v1",
		"exit_code":           outcome.exitCode,
		"result":              outcome.result,
		"execution_id":        outcome.executionID,
		"status":              outcome.status,
		"error":               outcome.errMsg,
	})
}

func rememberOutcome(dst **leasedJobOutcome, outcome leasedJobOutcome) {
	if dst == nil {
		return
	}
	copy := outcome
	*dst = &copy
}

func applyJobOutcome(
	conn *websocket.Conn,
	outcome leasedJobOutcome,
	runningCmd **exec.Cmd,
	runningExecID *string,
	jobDone *<-chan leasedJobOutcome,
	halt *bool,
	halted *atomic.Bool,
	lastComplete **leasedJobOutcome,
) {
	rememberOutcome(lastComplete, outcome)
	_ = writeJobOutcome(conn, outcome)
	if runningCmd != nil {
		*runningCmd = nil
	}
	if runningExecID != nil {
		*runningExecID = ""
	}
	if jobDone != nil {
		*jobDone = nil
	}
	if halt != nil {
		*halt = false
	}
	if halted != nil {
		halted.Store(false)
	}
}

func flushPendingOutcome(
	conn *websocket.Conn,
	jobDone *<-chan leasedJobOutcome,
	runningCmd **exec.Cmd,
	runningExecID *string,
	halt *bool,
	halted *atomic.Bool,
	lastComplete **leasedJobOutcome,
) {
	if jobDone == nil || *jobDone == nil {
		return
	}
	select {
	case outcome := <-*jobDone:
		applyJobOutcome(
			conn, outcome, runningCmd, runningExecID, jobDone, halt, halted, lastComplete,
		)
	default:
	}
}

func runnerForegroundLoop(state *runnerState, interrupt <-chan os.Signal, out io.Writer) error {
	wsURL, err := runnerWebsocketURL(state.ID)
	if err != nil {
		return err
	}

	halt := false
	halted := &atomic.Bool{}
	var runningCmd *exec.Cmd
	var runningExecID string
	var jobDone <-chan leasedJobOutcome
	var lastComplete *leasedJobOutcome
	backoff := runnerReconnectMin
	connectedOnce := false

	for {
		conn, err := dialRunnerWebsocket(wsURL, state.Token)
		if err != nil {
			fmt.Fprintf(out, "Connection failed (%v). Retrying in %s...\n", err, backoff)
			if !waitOrInterrupt(interrupt, backoff) {
				stopForegroundOnInterrupt(state, runningCmd, &halt, halted, out)
				return nil
			}
			backoff = nextRunnerBackoff(backoff)
			continue
		}
		if connectedOnce {
			fmt.Fprintf(out, "Reconnected. Waiting for jobs.\n")
		} else {
			fmt.Fprintf(out, "Connected. Waiting for jobs.\n")
			connectedOnce = true
		}
		backoff = runnerReconnectMin
		err = runRunnerSession(
			conn,
			interrupt,
			out,
			&runningCmd,
			&runningExecID,
			&jobDone,
			&halt,
			halted,
			&lastComplete,
		)
		_ = conn.Close()
		if err == nil {
			return nil
		}
		var fatal *runnerFatalError
		if errors.As(err, &fatal) {
			return err
		}
		fmt.Fprintf(out, "Connection lost (%v). Reconnecting in %s...\n", err, backoff)
		if !waitOrInterrupt(interrupt, backoff) {
			stopForegroundOnInterrupt(state, runningCmd, &halt, halted, out)
			return nil
		}
		backoff = nextRunnerBackoff(backoff)
	}
}

func stopForegroundOnInterrupt(
	state *runnerState,
	runningCmd *exec.Cmd,
	halt *bool,
	halted *atomic.Bool,
	out io.Writer,
) {
	fmt.Fprintf(out, "Unregistering...\n")
	if !requestJobHalt(halted, runningCmd) && halt != nil {
		*halt = false
	}
	unregisterRunnerBestEffort(state)
}

func unregisterRunnerBestEffort(state *runnerState) {
	if state == nil || state.Token == "" {
		return
	}
	wsURL, err := runnerWebsocketURL(state.ID)
	if err != nil {
		return
	}
	conn, err := dialRunnerWebsocket(wsURL, state.Token)
	if err != nil {
		return
	}
	defer func() { _ = conn.Close() }()
	_ = conn.SetWriteDeadline(time.Now().Add(2 * time.Second))
	_ = conn.WriteJSON(map[string]any{"type": "unregister"})
}

func runRunnerSession(
	conn *websocket.Conn,
	interrupt <-chan os.Signal,
	out io.Writer,
	runningCmd **exec.Cmd,
	runningExecID *string,
	jobDone *<-chan leasedJobOutcome,
	halt *bool,
	halted *atomic.Bool,
	lastComplete **leasedJobOutcome,
) error {
	_ = conn.SetReadDeadline(time.Now().Add(runnerReadWait))
	conn.SetPongHandler(func(string) error {
		return conn.SetReadDeadline(time.Now().Add(runnerReadWait))
	})

	incoming := make(chan runnerWSMessage, 8)
	readErr := make(chan error, 1)
	go func() {
		for {
			var msg runnerWSMessage
			if err := conn.ReadJSON(&msg); err != nil {
				readErr <- err
				return
			}
			_ = conn.SetReadDeadline(time.Now().Add(runnerReadWait))
			incoming <- msg
		}
	}()

	flushPendingOutcome(
		conn, jobDone, runningCmd, runningExecID, halt, halted, lastComplete,
	)

	killRunning := func() {
		if !requestJobHalt(halted, *runningCmd) {
			*halt = false
		}
	}

	logTicker := time.NewTicker(time.Second)
	defer logTicker.Stop()
	ticker := time.NewTicker(runnerHeartbeatEvery)
	defer ticker.Stop()

	for {
		select {
		case <-interrupt:
			fmt.Fprintf(out, "Unregistering...\n")
			killRunning()
			_ = conn.WriteJSON(map[string]any{"type": "unregister"})
			return nil
		case <-logTicker.C:
			if *runningCmd != nil {
				if buffer, ok := (*runningCmd).Stdout.(*runnerLogBuffer); ok {
					if err := flushRunnerLogs(conn, *runningExecID, buffer, false); err != nil {
						return err
					}
				}
			}
		case <-ticker.C:
			_ = conn.WriteControl(
				websocket.PingMessage, nil, time.Now().Add(runnerPingWait),
			)
			if err := conn.WriteJSON(map[string]any{"type": "heartbeat"}); err != nil {
				return fmt.Errorf("heartbeat: %w", err)
			}
		case err := <-readErr:
			return fmt.Errorf("runner read: %w", err)
		case outcome := <-*jobDone:
			applyJobOutcome(
				conn, outcome, runningCmd, runningExecID, jobDone, halt, halted, lastComplete,
			)
		case msg := <-incoming:
			if msg.Error != "" {
				return &runnerFatalError{fmt.Errorf("runner server: %s", msg.Error)}
			}
			if msg.Halt || msg.Type == "halt" {
				*halt = true
				fmt.Fprintf(out, "Halt received for %s\n", msg.HaltExecutionID)
				killRunning()
				continue
			}
			if msg.Job == nil {
				continue
			}
			jobID, _ := msg.Job["execution_id"].(string)
			if lastComplete != nil && *lastComplete != nil &&
				jobID != "" && (*lastComplete).executionID == jobID {
				_ = writeJobOutcome(conn, **lastComplete)
				continue
			}
			if *runningExecID != "" {
				fmt.Fprintf(out, "Ignoring job while %s is running\n", *runningExecID)
				continue
			}
			if err := beginLeasedJob(
				conn, msg.Job, *halt, out, runningCmd, runningExecID, jobDone, halted, lastComplete,
			); err != nil {
				fmt.Fprintf(out, "Job error: %v\n", err)
				*runningCmd = nil
				*runningExecID = ""
				*jobDone = nil
			}
			*halt = false
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
	lastComplete **leasedJobOutcome,
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
		outcome := leasedJobOutcome{executionID: executionID, status: "STOPPED"}
		rememberOutcome(lastComplete, outcome)
		return writeJobOutcome(conn, outcome)
	}

	opts, optsErr := runnerDockerOptsFromJob(job)
	resumeFrom := jobResumeFrom(job)
	if optsErr == nil && opts.PersistWorkspace {
		if hostDir, persistErr := preparePersistWorkspace(executionID, resumeFrom); persistErr == nil {
			opts.WorkspaceHostDir = hostDir
		} else {
			optsErr = fmt.Errorf("persist workspace: %w", persistErr)
		}
	}
	_ = cleanupStaleWorkspaces(map[string]bool{executionID: true})

	image := runnerImageFromJob(job)
	dockerOK := image != "" && runnerHasDocker()
	if reason := leasedJobFailureReason(job, dockerOK); reason != "" {
		outcome := leasedJobOutcome{
			executionID: executionID,
			status:      "FAILED",
			errMsg:      reason,
			lines:       []string{reason},
		}
		rememberOutcome(lastComplete, outcome)
		return writeJobOutcome(conn, outcome)
	}

	apiURL, err := runnerControlPlaneURL()
	if err != nil {
		reason := "PRELOOP_URL could not be resolved: " + err.Error()
		outcome := leasedJobOutcome{
			executionID: executionID,
			status:      "FAILED",
			errMsg:      reason,
			lines:       []string{reason},
		}
		rememberOutcome(lastComplete, outcome)
		return writeJobOutcome(conn, outcome)
	}

	launch, launchErr := runnerLaunchFromJob(job)
	if launchErr != nil {
		outcome := leasedJobOutcome{executionID: executionID, status: "FAILED", errMsg: launchErr.Error()}
		rememberOutcome(lastComplete, outcome)
		return writeJobOutcome(conn, outcome)
	}
	env := runnerJobEnv(job, apiURL)
	for key, value := range launch["env"].(map[string]any) {
		env[key] = value.(string)
	}
	env["PRELOOP_RUNNER_SCRIPT"] = launch["script"].(string)
	env["PRELOOP_MCP_URL"] = strings.TrimRight(apiURL, "/") + "/mcp/v1"
	opts.Launch = true
	opts.PreserveEntrypoint = image == "ghcr.io/openai/codex-universal:latest"
	if cfg, ok := job["agent_config"].(map[string]any); ok {
		if runner, ok := cfg["runner"].(map[string]any); ok {
			if preserve, ok := runner["preserve_image_entrypoint"].(bool); ok {
				opts.PreserveEntrypoint = preserve
			}
		}
	}
	if optsErr != nil {
		reason := optsErr.Error()
		outcome := leasedJobOutcome{
			executionID: executionID,
			status:      "FAILED",
			errMsg:      reason,
			lines:       []string{reason},
		}
		rememberOutcome(lastComplete, outcome)
		return writeJobOutcome(conn, outcome)
	}
	if opts.Network != "" {
		if netErr := ensureDockerNetwork(opts.Network); netErr != nil {
			reason := netErr.Error()
			outcome := leasedJobOutcome{
				executionID: executionID,
				status:      "FAILED",
				errMsg:      reason,
				lines:       []string{reason},
			}
			rememberOutcome(lastComplete, outcome)
			return writeJobOutcome(conn, outcome)
		}
	}
	cmd := newRunnerJobCmd(image, env, opts)
	var buf runnerLogBuffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	if err := cmd.Start(); err != nil {
		outcome := leasedJobOutcome{
			executionID: executionID,
			status:      "FAILED",
			errMsg:      err.Error(),
		}
		rememberOutcome(lastComplete, outcome)
		return writeJobOutcome(conn, outcome)
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

func waitDockerJob(cmd *exec.Cmd, executionID string, buf interface{ String() string }, halted *atomic.Bool) leasedJobOutcome {
	err := cmd.Wait()
	buffer, streaming := buf.(*runnerLogBuffer)
	if streaming {
		buffer.finish()
	}
	result, lines, resultErr := runnerStructuredResult(splitNonEmptyLines(buf.String()))
	outcome := leasedJobOutcome{executionID: executionID, status: "SUCCEEDED", lines: lines, result: result}
	if streaming {
		outcome.logBuffer = buffer
		outcome.lines = nil
	}
	if cmd.ProcessState != nil {
		outcome.exitCode = cmd.ProcessState.ExitCode()
	}
	if err != nil {
		if halted != nil && halted.Load() {
			outcome.status = "STOPPED"
			return outcome
		}
		outcome.status = "FAILED"
		outcome.errMsg = err.Error()
		return outcome
	}
	if resultErr != nil {
		outcome.status = "FAILED"
		outcome.errMsg = resultErr.Error()
		if streaming && buffer.overflow {
			outcome.errMsg = "Runner log buffer exceeded its limit; execution markers may be missing"
		}
	} else if runnerResultIsFailure(result) {
		outcome.status = "FAILED"
	}
	return outcome
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
	if image, ok := cfg["image"].(string); ok && strings.TrimSpace(image) != "" {
		return strings.TrimSpace(image)
	}
	if image, ok := cfg["docker_image"].(string); ok {
		return strings.TrimSpace(image)
	}
	return ""
}

// runnerJobEnv maps a leased job payload onto the environment contract
// hosted agent containers already receive (container.py): FLOW_ID,
// EXECUTION_ID, AGENT_PROMPT, AGENT_CONFIG, AI_MODEL, AI_MODEL_PROVIDER,
// and PRELOOP_API_TOKEN. PRELOOP_URL points the agent back at the
// control plane that leased the job.
func runnerJobEnv(job map[string]any, apiURL string) map[string]string {
	env := map[string]string{}
	setIf := func(key string, value any) {
		if s, ok := value.(string); ok && s != "" {
			env[key] = s
		}
	}
	setIf("EXECUTION_ID", job["execution_id"])
	setIf("FLOW_ID", job["flow_id"])
	setIf("AGENT_PROMPT", job["prompt"])
	setIf("AI_MODEL", job["model_identifier"])
	setIf("AI_MODEL_PROVIDER", job["model_provider"])
	setIf("PRELOOP_API_TOKEN", job["account_api_token"])
	if apiURL != "" {
		env["PRELOOP_URL"] = apiURL
	}
	if executionID, ok := job["execution_id"].(string); ok && executionID != "" {
		env["COMPOSE_PROJECT_NAME"] = composeProjectName(executionID)
	}
	if cfg, ok := job["agent_config"].(map[string]any); ok && len(cfg) > 0 {
		sanitized := sanitizeAgentConfig(cfg)
		if len(sanitized) > 0 {
			if data, err := json.Marshal(sanitized); err == nil {
				env["AGENT_CONFIG"] = string(data)
			}
		}
	}
	return env
}

func sanitizeAgentConfig(cfg map[string]any) map[string]any {
	out := make(map[string]any, len(cfg))
	for key, value := range cfg {
		if isAgentConfigSecretKey(key) {
			continue
		}
		if nested, ok := value.(map[string]any); ok {
			out[key] = sanitizeAgentConfig(nested)
			continue
		}
		out[key] = value
	}
	return out
}

func isAgentConfigSecretKey(key string) bool {
	switch strings.ToLower(key) {
	case "api_key", "apikey", "api_token", "access_token", "secret", "password", "token":
		return true
	default:
		lower := strings.ToLower(key)
		return strings.HasSuffix(lower, "_api_key") ||
			strings.HasSuffix(lower, "_access_token") ||
			strings.HasSuffix(lower, "_password") ||
			strings.HasSuffix(lower, "_secret")
	}
}

func formatJobEnv(env map[string]string) []string {
	pairs := make([]string, 0, len(env))
	for key, value := range env {
		pairs = append(pairs, key+"="+value)
	}
	sort.Strings(pairs)
	return pairs
}

func runnerControlPlaneURL() (string, error) {
	cfg, err := config.Resolve(FlagToken, FlagURL)
	if err != nil {
		return "", err
	}
	apiURL := strings.TrimRight(cfg.APIURL, "/")
	if apiURL == "" {
		return "", fmt.Errorf("PRELOOP_URL is empty")
	}
	return apiURL, nil
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
		return writeLaunchdPlist(bin, cmd.OutOrStdout())
	case "linux":
		return writeSystemdUserUnit(bin, cmd.OutOrStdout())
	case "windows":
		return writeWindowsScheduledTask(bin, cmd.OutOrStdout())
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

func writeLaunchdPlist(bin string, out io.Writer) error {
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
	fmt.Fprintf(out, "Installed %s\n", path)
	return nil
}

func writeSystemdUserUnit(bin string, out io.Writer) error {
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
	fmt.Fprintf(out, "Installed %s\n", path)
	return nil
}

func writeWindowsScheduledTask(bin string, out io.Writer) error {
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
	fmt.Fprintln(out, "Installed scheduled task PreloopRunner")
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
