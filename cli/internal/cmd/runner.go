package cmd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"os/user"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"strconv"
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
	runnerWriteWait    = 5 * time.Second
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
	Use:     "enable",
	Aliases: []string{"install"},
	Short:   "Install a system service for preloop runner fg",
	RunE:    runRunnerEnable,
}

var runnerDisableCmd = &cobra.Command{
	Use:     "disable",
	Aliases: []string{"uninstall"},
	Short:   "Remove the runner system service",
	Long: `Stop and remove the runner system service.

With --delete the runner is also deleted on the server once the service has
stopped: its token stops working and the local runner state is removed. The
server refuses while the runner still holds an execution; --force halts those
executions and deletes the runner anyway.`,
	RunE: runRunnerDisable,
}

var runnerRotateTokenCmd = &cobra.Command{
	Use:   "rotate-token",
	Short: "Issue a new token for this runner and restart the service",
	Long: `Ask the server for a new runner token, write it to the local runner
state and restart the installed service so it reconnects with it. The old
token is rejected from the moment the server answers.`,
	RunE: runRunnerRotateToken,
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
	runnerFgCmd.Flags().StringVar(&runnerDeviceRef, "device-ref", "", "workstation reference override, matching agents discover --device-ref")
	runnerCmd.AddCommand(runnerFgCmd)
	runnerCmd.AddCommand(runnerSetupCmd)
	runnerCmd.AddCommand(runnerEnableCmd)
	runnerCmd.AddCommand(runnerDisableCmd)
	runnerCmd.AddCommand(runnerStartCmd)
	runnerCmd.AddCommand(runnerStopCmd)
	runnerCmd.AddCommand(runnerRestartCmd)
	runnerCmd.AddCommand(runnerStatusCmd)
	runnerCmd.AddCommand(runnerRotateTokenCmd)
	runnerDisableCmd.Flags().Bool(
		"delete", false,
		"also delete the runner on the server and forget its local state",
	)
	runnerDisableCmd.Flags().Bool(
		"force", false,
		"with --delete, halt executions the runner still holds instead of refusing",
	)
	runnerFgCmd.Flags().StringSlice("labels", nil, "labels used to match runner pools")
	runnerFgCmd.Flags().String("name", "", "runner display name (default: hostname)")
	runnerFgCmd.Flags().Bool("once", false, "exit after the first leased execution finishes")
	runnerFgCmd.Flags().Bool(
		"ephemeral", false,
		"register for this process only and unregister on every exit path",
	)
	runnerFgCmd.Flags().Duration(
		"wait-for-job", defaultRunnerWaitForJob,
		"how long --once waits for a job before exiting non-zero",
	)
	runnerFgCmd.Flags().Int(
		"concurrency",
		0,
		"executions to run at once (default: runner.concurrency in config, else 2)",
	)
}

type runnerState struct {
	ID        string `json:"id"`
	Token     string `json:"token"`
	Name      string `json:"name"`
	DeviceRef string `json:"device_ref,omitempty"`
	// AuthorizedDirectories is the operator's list of directories remote
	// sessions may open (preloop runner dirs). Kept here so a token
	// rotation or re-registration never drops it.
	AuthorizedDirectories []authorizedDirectory `json:"authorized_directories,omitempty"`
	// Harnesses holds per-harness switches, e.g.
	// {"cursor_cli": {"enabled": false}}. Kept on rewrite.
	Harnesses map[string]runnerHarnessConfig `json:"harnesses,omitempty"`
}

type runnerAPIRecord struct {
	ID                  string   `json:"id"`
	Name                string   `json:"name"`
	Status              string   `json:"status"`
	LastHeartbeat       *string  `json:"last_heartbeat"`
	CurrentExecutionID  *string  `json:"current_execution_id"`
	RunningExecutionIDs []string `json:"running_execution_ids"`
	Concurrency         int      `json:"concurrency"`
	Capacity            int      `json:"capacity"`
	RunningCount        int      `json:"running_count"`
	Hostname            string   `json:"hostname"`
	Labels              []string `json:"labels"`
	Token               string   `json:"token"`
}

type runnerWSMessage struct {
	LogAcknowledgements bool   `json:"log_acknowledgements,omitempty"`
	BatchID             string `json:"batch_id,omitempty"`

	Version       int                `json:"version,omitempty"`
	ExecutionID   string             `json:"execution_id,omitempty"`
	Nonce         string             `json:"nonce,omitempty"`
	HeadSHA       string             `json:"head_sha,omitempty"`
	TreeSHA       string             `json:"tree_sha,omitempty"`
	BundleSHA256  string             `json:"bundle_sha256,omitempty"`
	Image         string             `json:"image,omitempty"`
	BudgetSeconds int                `json:"budget_seconds,omitempty"`
	Checks        []publicationCheck `json:"checks,omitempty"`
	Binding       map[string]any     `json:"binding,omitempty"`
	Lease         map[string]any     `json:"lease,omitempty"`

	Type      string `json:"type"`
	Ephemeral bool   `json:"ephemeral,omitempty"`
	// Job is the first held execution and Jobs is all of them. A server
	// that predates concurrency sends only Job.
	Job              map[string]any   `json:"job,omitempty"`
	Jobs             []map[string]any `json:"jobs,omitempty"`
	Concurrency      int              `json:"concurrency,omitempty"`
	Halt             bool             `json:"halt,omitempty"`
	HaltExecutionID  string           `json:"halt_execution_id,omitempty"`
	HaltExecutionIDs []string         `json:"halt_execution_ids,omitempty"`
	Error            string           `json:"error,omitempty"`
	RunnerID         string           `json:"runner_id,omitempty"`

	// Remote session frames (session_start, session_turn, session_stop).
	RemoteSessionID string              `json:"remote_session_id,omitempty"`
	TurnID          string              `json:"turn_id,omitempty"`
	Text            string              `json:"text,omitempty"`
	Mode            string              `json:"mode,omitempty"`
	Harness         string              `json:"harness,omitempty"`
	Model           string              `json:"model,omitempty"`
	FirstPrompt     string              `json:"first_prompt,omitempty"`
	Workspace       map[string]any      `json:"workspace,omitempty"`
	Actor           *runnerSessionActor `json:"actor,omitempty"`
	Limits          map[string]any      `json:"limits,omitempty"`

	// InventoryWanted on an ack asks for the full harness inventory.
	InventoryWanted bool `json:"inventory_wanted,omitempty"`
}

// deliveredJobs returns every job in one server frame, first one first and
// without duplicates, so old and new servers are handled the same way.
func (m runnerWSMessage) deliveredJobs() []map[string]any {
	jobs := make([]map[string]any, 0, len(m.Jobs)+1)
	seen := map[string]bool{}
	for _, job := range append([]map[string]any{m.Job}, m.Jobs...) {
		if job == nil {
			continue
		}
		id, _ := job["execution_id"].(string)
		if id != "" {
			if seen[id] {
				continue
			}
			seen[id] = true
		}
		jobs = append(jobs, job)
	}
	return jobs
}

// haltedExecutions returns the executions a halt frame names.
func (m runnerWSMessage) haltedExecutions() []string {
	ids := make([]string, 0, len(m.HaltExecutionIDs)+1)
	seen := map[string]bool{}
	for _, id := range append([]string{m.HaltExecutionID}, m.HaltExecutionIDs...) {
		if id == "" || seen[id] {
			continue
		}
		seen[id] = true
		ids = append(ids, id)
	}
	return ids
}

func runRunnerFg(cmd *cobra.Command, args []string) error {
	flagLabels, _ := cmd.Flags().GetStringSlice("labels")
	labels := effectiveRunnerLabels(flagLabels)
	name, _ := cmd.Flags().GetString("name")
	once, err := runnerOnceFromFlags(cmd)
	if err != nil {
		return err
	}
	runnerOnce = once
	requested, _ := cmd.Flags().GetInt("concurrency")
	hostname, _ := os.Hostname()
	if once.registersEphemeral() {
		labels = ephemeralRunnerLabels(labels, hostname, os.Getpid())
	}
	if name == "" {
		name = hostname
	}
	concurrency := resolveRunnerConcurrency(requested)

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}

	state, err := loadOrRegisterRunner(client, name, hostname, labels, concurrency)
	if err != nil {
		return err
	}
	// Checkouts of sessions that did not survive the restart are garbage;
	// a session restored from disk keeps its clone.
	_ = cleanupSessionWorkspaces(liveSessionCheckouts())
	// Unregister on every exit path an ephemeral process can take: a clean
	// one-shot finish, a fatal server rejection, a panic. The signal handler
	// below covers SIGTERM/SIGINT/SIGHUP; SIGKILL cannot be caught, which is
	// why the server also deletes ephemeral rows whose heartbeat lapses.
	if once.registersEphemeral() {
		defer unregisterRunnerBestEffort(state)
	}
	fmt.Fprintf(
		cmd.OutOrStdout(),
		"Runner %s (%s) connecting with %d slots...\n",
		state.Name, state.ID, concurrency,
	)

	reapOrphanedPublicationRuntimes()

	interrupt := make(chan os.Signal, 1)
	signal.Notify(interrupt, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(interrupt)
	if once.registersEphemeral() {
		// A CI job whose controlling process dies sends SIGHUP; a runner
		// that stays registered after it is exactly the phantom this mode
		// exists to prevent. Long-lived services keep ignoring SIGHUP.
		signal.Notify(interrupt, syscall.SIGHUP)
	}
	stopWaiting := once.armWaitForJob(interrupt)
	defer stopWaiting()
	if err := runnerForegroundLoop(state, interrupt, cmd.OutOrStdout(), concurrency); err != nil {
		return err
	}
	return once.result()
}

// resolveRunnerConcurrency picks how many executions this process will hold:
// the flag when given, otherwise runner.concurrency from the environment or
// config file, otherwise the default. The value is a ceiling the server may
// lower, never a promise it must honour, so an out-of-range request is
// clamped rather than rejected.
func resolveRunnerConcurrency(flagValue int) int {
	concurrency := flagValue
	if concurrency <= 0 {
		concurrency = config.RunnerConcurrency()
	}
	if concurrency < 1 {
		return 1
	}
	if concurrency > config.MaxRunnerConcurrency {
		return config.MaxRunnerConcurrency
	}
	return concurrency
}

func loadOrRegisterRunner(
	client *api.Client, name, hostname string, labels []string, concurrency int,
) (*runnerState, error) {
	if concurrency < 1 {
		concurrency = config.DefaultRunnerConcurrency
	}
	req := map[string]any{
		"harness_inventory":      runnerInventory.current(true),
		"host_exec_profiles":     hostExecAdvertisements(),
		"authorized_directories": authorizedDirectoryAdvertisements(),
		"docker_available":       advertisedDockerAvailable(),
		"name":                   name,
		"hostname":               hostname,
		"os":                     runtime.GOOS,
		"arch":                   runtime.GOARCH,
		"labels":                 labels,
		"concurrency":            concurrency,
	}
	if runnerOnce.registersEphemeral() {
		return registerEphemeralRunner(client, req)
	}
	deviceRef := runnerDeviceRef
	if existing, err := readRunnerState(); err == nil && deviceRef == "" {
		deviceRef = existing.DeviceRef
	}
	runnerFingerprint = newRunnerFingerprintPublisher(client, deviceRef)
	runnerFingerprint.addToHeartbeat(req)
	if deviceRef != "" {
		if err := updateRunnerState(func(current *runnerState) error {
			current.DeviceRef = deviceRef
			return nil
		}); err != nil {
			return nil, err
		}
	}
	if existing, err := readRunnerState(); err == nil && existing.ID != "" && existing.Token != "" {
		req["runner_id"] = existing.ID
		var resumed runnerAPIRecord
		if err := client.Post("/api/v1/runners/register", req, &resumed); err == nil && resumed.ID != "" {
			// Re-read under the lock: a `dirs add` during the register
			// request must survive this write.
			_ = updateRunnerState(func(current *runnerState) error {
				current.ID = resumed.ID
				current.Name = existing.Name
				if resumed.Name != "" {
					current.Name = resumed.Name
				}
				current.Token = existing.Token
				if resumed.Token != "" {
					current.Token = resumed.Token
				}
				existing = current
				return nil
			})
			runnerInventory.markSent(registeredInventoryHash(req))
			return existing, nil
		}
		delete(req, "runner_id")
	}

	var created runnerAPIRecord
	if err := client.Post("/api/v1/runners/register", req, &created); err != nil {
		return nil, fmt.Errorf("register runner: %w", err)
	}
	var state *runnerState
	// Directories authorized before the first registration (or after a
	// stale identity was discarded) belong to this host, not to the runner
	// row, so they survive a fresh registration, as do the operator's
	// harness switches.
	if err := updateRunnerState(func(current *runnerState) error {
		current.ID, current.Token, current.Name = created.ID, created.Token, created.Name
		state = current
		return nil
	}); err != nil {
		return nil, err
	}
	runnerInventory.markSent(registeredInventoryHash(req))
	return state, nil
}

type leasedJobOutcome struct {
	publicationRequired     bool
	publicationAcknowledged bool
	hostExec                bool
	profile                 string
	logBuffer               *runnerLogBuffer
	result                  map[string]any
	exitCode                int
	executionID             string
	status                  string
	errMsg                  string
	lines                   []string
	evidenceUpload          string
	// hostPublication is the runner-authored managed publication receipt
	// ({status, branch, head_sha, reason}); never agent output.
	hostPublication map[string]any
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

// All application frames share a bounded write deadline. A read timeout alone
// cannot interrupt the session loop while it is blocked writing a log frame.
func writeRunnerJSON(conn *websocket.Conn, message any) error {
	if err := conn.SetWriteDeadline(time.Now().Add(runnerWriteWait)); err != nil {
		return err
	}
	return conn.WriteJSON(message)
}

func writeJobOutcome(conn *websocket.Conn, outcome leasedJobOutcome) error {
	if outcome.publicationRequired && !outcome.publicationAcknowledged && outcome.status == "SUCCEEDED" {
		outcome.status = "FAILED"
		outcome.errMsg = "isolated publication was not confirmed; write requests are never replayed automatically"
	}
	if conn == nil {
		return nil
	}
	if err := flushRunnerLogs(conn, outcome.executionID, outcome.logBuffer, true); err != nil {
		return err
	}
	for offset := 0; offset < len(outcome.lines); offset += 128 {
		end := offset + 128
		if end > len(outcome.lines) {
			end = len(outcome.lines)
		}
		if err := writeRunnerJSON(conn, map[string]any{
			"type":         "logs",
			"execution_id": outcome.executionID,
			"lines":        outcome.lines[offset:end],
		}); err != nil {
			return err
		}
	}
	message := map[string]any{
		"type": "complete", "exit_code": outcome.exitCode,
		"result": outcome.result, "execution_id": outcome.executionID,
		"status": outcome.status, "error": outcome.errMsg,
	}
	if outcome.hostExec {
		message["completion_protocol"] = hostExecCompletionProtocol
		message["host_exec_profile"] = outcome.profile
	} else {
		message["launch_version"] = runnerLaunchVersion
		message["completion_protocol"] = "docker_v1"
	}
	if outcome.evidenceUpload != "" {
		message["evidence_upload"] = outcome.evidenceUpload
	}
	if outcome.hostExec && outcome.hostPublication != nil {
		message["host_publication"] = outcome.hostPublication
	}
	if err := writeRunnerJSON(conn, message); err != nil {
		return err
	}
	// Every terminal report for the single execution passes through here,
	// including the ones beginLeasedJob writes without ever starting a
	// container ("docker is not available", a rejected payload).
	if runnerOnce.record(outcome) {
		return errRunnerOnceDone
	}
	return nil
}

// applyJobOutcome reports one execution's terminal outcome and frees the
// slot it held. Other jobs on this runner are untouched.
func applyJobOutcome(
	conn *websocket.Conn,
	outcome leasedJobOutcome,
	jobs *runnerJobs,
) error {
	if jobs != nil {
		jobs.remember(outcome)
	}
	err := writeJobOutcome(conn, outcome)
	if jobs != nil {
		jobs.finish(outcome.executionID)
	}
	return err
}

// flushPendingOutcomes delivers outcomes that completed while the socket was
// down, before the new session reports anything else.
func flushPendingOutcomes(conn *websocket.Conn, jobs *runnerJobs) error {
	if jobs == nil {
		return nil
	}
	for {
		select {
		case outcome := <-jobs.outcomes:
			releaseWorkspaceLease(outcome.executionID)
			if err := applyJobOutcome(conn, outcome, jobs); err != nil {
				return err
			}
		default:
			return nil
		}
	}
}

func runnerForegroundLoop(
	state *runnerState, interrupt <-chan os.Signal, out io.Writer, concurrency int,
) error {
	wsURL, err := runnerWebsocketURL(state.ID)
	if err != nil {
		return err
	}

	jobs := newRunnerJobs(concurrency)
	jobs.sessions = newRunnerSessionManager(out)
	defer jobs.sessions.shutdown()
	backoff := runnerReconnectMin
	connectedOnce := false

	for {
		// Probe before opening the socket so host checks cannot delay server errors.
		initialHeartbeat := runnerHeartbeatMessage(jobs.concurrency)
		conn, err := dialRunnerWebsocket(wsURL, state.Token)
		if err != nil {
			fmt.Fprintf(out, "Connection failed (%v). Retrying in %s...\n", err, backoff)
			if !waitOrInterrupt(interrupt, backoff) {
				stopForegroundOnInterrupt(state, jobs, out)
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
		err = runRunnerSession(conn, interrupt, out, jobs, initialHeartbeat)
		_ = conn.Close()
		if err == nil {
			return nil
		}
		// --once: the single execution reported a terminal status and the
		// server already has the frame. Do not reconnect for a job that
		// will never come.
		if errors.Is(err, errRunnerOnceDone) {
			return nil
		}
		var fatal *runnerFatalError
		if errors.As(err, &fatal) {
			return err
		}
		fmt.Fprintf(out, "Connection lost (%v). Reconnecting in %s...\n", err, backoff)
		if !waitOrInterrupt(interrupt, backoff) {
			stopForegroundOnInterrupt(state, jobs, out)
			return nil
		}
		backoff = nextRunnerBackoff(backoff)
	}
}

func stopForegroundOnInterrupt(state *runnerState, jobs *runnerJobs, out io.Writer) {
	fmt.Fprintf(out, "Unregistering...\n")
	if jobs != nil {
		jobs.haltAll()
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
	if err := conn.WriteJSON(map[string]any{"type": "unregister"}); err != nil {
		return
	}
	// Wait for the acknowledgement before dropping the socket. This frame is
	// the last act of an ephemeral runner, and a process that exits while it
	// is still in flight leaves behind the row it promised to remove. The
	// read deadline bounds the wait; the server sends its hello first.
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	for {
		var reply runnerWSMessage
		if err := conn.ReadJSON(&reply); err != nil || reply.Type == "ack" {
			return
		}
	}
}

func runRunnerSession(
	conn *websocket.Conn,
	interrupt <-chan os.Signal,
	out io.Writer,
	jobs *runnerJobs,
	preparedHeartbeat ...map[string]any,
) error {
	// Publication controllers talk to the server over this socket, so they
	// cannot outlive the session. The jobs themselves can, and do.
	defer jobs.abortPublications()
	_ = conn.SetReadDeadline(time.Now().Add(runnerReadWait))
	conn.SetPongHandler(func(string) error {
		return conn.SetReadDeadline(time.Now().Add(runnerReadWait))
	})

	sessionDone := make(chan struct{})
	defer close(sessionDone)
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
			select {
			case incoming <- msg:
			case <-sessionDone:
				if msg.Lease != nil {
					delete(msg.Lease, "token")
				}
				return
			}
		}
	}()

	initialHeartbeat := map[string]any(nil)
	if len(preparedHeartbeat) > 0 {
		initialHeartbeat = preparedHeartbeat[0]
	} else {
		initialHeartbeat = runnerHeartbeatMessage(jobs.concurrency)
	}
	if err := writeRunnerJSON(conn, initialHeartbeat); err != nil {
		return fmt.Errorf("initial heartbeat: %w", err)
	}
	// Remote sessions outlive the socket; tell this connection which are
	// live so the server resumes delivery to them.
	jobs.sessions.reportLive()
	sendSessionFrame := func(msg map[string]any) error { return writeRunnerJSON(conn, msg) }
	if err := jobs.sessions.flush(sendSessionFrame); err != nil {
		return fmt.Errorf("session report: %w", err)
	}

	// Unacknowledged output has to be offered again on the new socket, for
	// every held job and every outcome still waiting to be reported.
	for _, id := range jobs.ids() {
		if buffer := jobs.job(id).logBuffer(); buffer != nil {
			buffer.resetDelivery()
		}
	}
	for _, outcome := range jobs.completed {
		if outcome != nil && outcome.logBuffer != nil {
			outcome.logBuffer.resetDelivery()
		}
	}
	logAcknowledgements := false

	logTicker := time.NewTicker(100 * time.Millisecond)
	defer logTicker.Stop()
	ticker := time.NewTicker(runnerHeartbeatEvery)
	defer ticker.Stop()

	for {
		select {
		case <-interrupt:
			fmt.Fprintf(out, "Unregistering...\n")
			jobs.haltAll()
			jobs.sessions.shutdown()
			_ = writeRunnerJSON(conn, map[string]any{"type": "unregister"})
			return nil
		case <-logTicker.C:
			for _, id := range jobs.ids() {
				buffer := jobs.job(id).logBuffer()
				if buffer == nil {
					continue
				}
				if err := flushRunnerLogs(conn, id, buffer, false); err != nil {
					return err
				}
			}
		case event := <-jobs.events:
			if event.outcome != nil {
				if err := applyJobOutcome(conn, *event.outcome, jobs); err != nil {
					return err
				}
			} else if event.message != nil {
				if err := writeRunnerJSON(conn, event.message); err != nil {
					return err
				}
			}
		case <-jobs.sessions.notifyChan():
			if err := jobs.sessions.flush(sendSessionFrame); err != nil {
				return fmt.Errorf("session delivery: %w", err)
			}
		case <-ticker.C:
			jobs.sessions.tick()
			if !jobs.publicationActive() {
				_ = cleanupPublicationRecovery(time.Now())
			}
			// Retention progresses even when the runner receives no new jobs,
			// and it must keep the workspace of every job, not just one.
			for _, id := range jobs.ids() {
				_ = touchWorkspaceLease(id)
			}
			_ = cleanupStaleWorkspaces(jobs.keepSet())
			_ = conn.WriteControl(
				websocket.PingMessage, nil, time.Now().Add(runnerPingWait),
			)
			if err := writeRunnerJSON(conn, runnerHeartbeatMessage(jobs.concurrency)); err != nil {
				return fmt.Errorf("heartbeat: %w", err)
			}
		case err := <-readErr:
			return fmt.Errorf("runner read: %w", err)
		case outcome := <-jobs.outcomes:
			releaseWorkspaceLease(outcome.executionID)
			job := jobs.job(outcome.executionID)
			if job != nil && job.publication != nil && !job.publication.started {
				// The agent finished; the slot stays held until its
				// publication reports a terminal outcome of its own.
				job.cmd = nil
				job.publication.start(outcome)
				continue
			}
			if err := applyJobOutcome(conn, outcome, jobs); err != nil {
				return err
			}
		case msg := <-incoming:
			if jobs.sessions.handle(msg) {
				continue
			}
			if msg.Type == "hello" {
				logAcknowledgements = msg.LogAcknowledgements
				// The echo is how a one-shot run learns whether this
				// control plane will delete its row on the way out.
				runnerOnce.noteHelloEphemeral(msg.Ephemeral)
				for _, id := range jobs.ids() {
					if b := jobs.job(id).logBuffer(); b != nil {
						b.setLogAcknowledgements(logAcknowledgements)
					}
				}
				if err := flushPendingOutcomes(conn, jobs); err != nil {
					return err
				}
				for _, outcome := range jobs.completed {
					if outcome == nil {
						continue
					}
					if b := outcome.logBuffer; b != nil {
						b.setLogAcknowledgements(logAcknowledgements)
					}
					if err := writeJobOutcome(conn, *outcome); err != nil {
						return err
					}
				}
			}
			if msg.Type == "ack" && msg.InventoryWanted {
				runnerInventory.requestFull()
			}
			if msg.Type == "logs_ack" {
				if b := jobs.job(msg.ExecutionID).logBuffer(); b != nil {
					b.acknowledgeBatch(msg.BatchID)
				}
				if done := jobs.completedOutcome(msg.ExecutionID); done != nil && done.logBuffer != nil {
					done.logBuffer.acknowledgeBatch(msg.BatchID)
				}
				continue
			}
			if msg.Type == "error" && msg.Error == "Invalid runner log batch" && msg.BatchID != "" {
				// The batch is unidentifiable, so retire it everywhere.
				for _, id := range jobs.ids() {
					if b := jobs.job(id).logBuffer(); b != nil {
						b.acknowledgeBatch(msg.BatchID)
					}
				}
				for _, outcome := range jobs.completed {
					if outcome != nil && outcome.logBuffer != nil {
						outcome.logBuffer.acknowledgeBatch(msg.BatchID)
					}
				}
				continue
			}
			if strings.HasPrefix(msg.Type, "publication_") {
				job := jobs.job(msg.ExecutionID)
				if job == nil || job.publication == nil {
					return errors.New("unexpected publication message without active lease")
				}
				if msg.Error != "" {
					job.publication.abort()
					return errors.New("publication controller rejected transition")
				}
				if err := job.publication.accept(msg); err != nil {
					if msg.Lease != nil {
						delete(msg.Lease, "token")
					}
					job.publication.abort()
					return err
				}
				continue
			}
			if msg.Error != "" {
				return &runnerFatalError{fmt.Errorf("runner server: %s", msg.Error)}
			}
			if msg.Halt || msg.Type == "halt" {
				halted := msg.haltedExecutions()
				if len(halted) == 0 {
					// A server that names no execution means the runner.
					fmt.Fprintf(out, "Halt received for this runner\n")
					jobs.haltAll()
					continue
				}
				for _, executionID := range halted {
					fmt.Fprintf(out, "Halt received for %s\n", executionID)
					jobs.haltOne(executionID)
				}
				continue
			}
			for _, job := range msg.deliveredJobs() {
				jobID, _ := job["execution_id"].(string)
				if done := jobs.completedOutcome(jobID); done != nil {
					if err := writeJobOutcome(conn, *done); err != nil {
						return err
					}
					continue
				}
				if jobs.job(jobID) != nil {
					// Redelivery of work this process already holds.
					continue
				}
				if jobs.freeSlots() <= 0 {
					fmt.Fprintf(
						out,
						"Ignoring job %s: all %d slots are busy\n",
						jobID, jobs.concurrency,
					)
					continue
				}
				if err := beginLeasedJob(conn, job, out, jobs); err != nil {
					return fmt.Errorf("job delivery: %w", err)
				}
				if b := jobs.job(jobID).logBuffer(); b != nil {
					b.setLogAcknowledgements(logAcknowledgements)
				}
			}
		}
	}
}

// beginLeasedJob takes one slot in jobs and starts the execution in it.
// Everything it needs is per execution: its own halt latch, its own log
// buffer, its own workspace and its own publication controller.
func beginLeasedJob(
	conn *websocket.Conn,
	job map[string]any,
	out io.Writer,
	jobs *runnerJobs,
) error {
	executionID, _ := job["execution_id"].(string)
	if executionID == "" {
		return fmt.Errorf("job missing execution_id")
	}
	runnerOnce.markLeased(executionID)
	if err := isolatedPublicationHostExecError(job); err != nil {
		outcome := leasedJobOutcome{executionID: executionID, status: "FAILED", errMsg: err.Error()}
		jobs.remember(outcome)
		return writeJobOutcome(conn, outcome)
	}
	alreadyHalted := jobs.pendingHalt[executionID]
	halted := &atomic.Bool{}
	fmt.Fprintf(out, "Leased execution %s\n", executionID)
	_ = writeRunnerJSON(conn, map[string]any{
		"type":         "status",
		"execution_id": executionID,
		"status":       "RUNNING",
	})
	_ = writeRunnerJSON(conn, map[string]any{
		"type":         "logs",
		"execution_id": executionID,
		"lines":        []string{"runner leased job " + executionID},
	})
	if alreadyHalted {
		delete(jobs.pendingHalt, executionID)
		outcome := leasedJobOutcome{executionID: executionID, status: "STOPPED", hostExec: jobHostExecProfileName(job) != "", profile: jobHostExecProfileName(job)}
		jobs.remember(outcome)
		return writeJobOutcome(conn, outcome)
	}

	if jobHostExecProfileName(job) != "" {
		return beginHostExecJob(conn, job, executionID, halted, jobs)
	}

	opts, optsErr := runnerDockerOptsFromJob(job)
	var isolated *runnerPublication
	if optsErr == nil {
		isolated, optsErr = publicationFromJob(job, opts)
	}
	if optsErr != nil {
		outcome := leasedJobOutcome{executionID: executionID, status: "FAILED", errMsg: optsErr.Error()}
		jobs.remember(outcome)
		return writeJobOutcome(conn, outcome)
	}
	if isolated != nil {
		opts.Publication = isolated
	}
	launched := false
	defer func() {
		if isolated != nil && !launched {
			isolated.cancel()
			_ = isolated.removeVolume(isolated.exportVolume)
		}
	}()
	resumeFrom := jobResumeFrom(job)
	if optsErr == nil && opts.PersistWorkspace {
		if hostDir, recovered, persistErr := preparePersistWorkspace(executionID, resumeFrom); persistErr == nil {
			opts.WorkspaceHostDir = hostDir
			_ = touchWorkspaceLease(executionID)
			if !recovered {
				// Starting anyway would clone cold and silently discard the
				// unpushed work this job was leased to continue.
				optsErr = workspaceRecoveryUnavailable(resumeFrom, hostDir)
			}
		} else {
			optsErr = fmt.Errorf("persist workspace: %w", persistErr)
		}
	}
	keep := jobs.keepSet()
	keep[executionID] = true
	_ = cleanupStaleWorkspaces(keep)

	image := runnerImageFromJob(job)
	dockerOK := image != "" && runnerHasDocker()
	if reason := leasedJobFailureReason(job, dockerOK); reason != "" {
		outcome := leasedJobOutcome{
			executionID: executionID,
			status:      "FAILED",
			errMsg:      reason,
			lines:       []string{reason},
		}
		jobs.remember(outcome)
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
		jobs.remember(outcome)
		return writeJobOutcome(conn, outcome)
	}

	launch, launchErr := runnerLaunchFromJob(job)
	if launchErr != nil {
		outcome := leasedJobOutcome{executionID: executionID, status: "FAILED", errMsg: launchErr.Error()}
		jobs.remember(outcome)
		return writeJobOutcome(conn, outcome)
	}
	env := runnerJobEnv(job, apiURL)
	for key, value := range launch["env"].(map[string]any) {
		env[key] = value.(string)
	}
	env["PRELOOP_RUNNER_SCRIPT"] = launch["script"].(string)
	env["PRELOOP_MCP_URL"] = strings.TrimRight(apiURL, "/") + "/mcp/v1"
	opts.Launch = true
	opts.PreserveEntrypoint = preserveCodexUniversalEntrypoint(image)
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
		jobs.remember(outcome)
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
			jobs.remember(outcome)
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
		jobs.remember(outcome)
		return writeJobOutcome(conn, outcome)
	}
	launched = true
	if isolated != nil {
		// One controller per execution: two concurrent publications each
		// need their own nonce, writer credential and expiry.
		isolated.events = jobs.events
	}
	jobs.start(&runnerJob{
		executionID: executionID,
		cmd:         cmd,
		halted:      halted,
		publication: isolated,
	})
	outcomes := jobs.outcomes
	go func() {
		outcome := waitDockerJob(cmd, executionID, &buf, halted)
		outcome.publicationRequired = isolated != nil
		outcomes <- outcome
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
		killRunnerJobProcess(runningCmd)
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
	result, lines, evidenceUpload, resultErr := parseRunnerStructuredResult(splitNonEmptyLines(buf.String()))
	outcome := leasedJobOutcome{executionID: executionID, status: "SUCCEEDED", lines: lines, result: result, evidenceUpload: evidenceUpload}
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

func isolatedPublicationHostExecError(job map[string]any) error {
	if jobHostExecProfileName(job) == "" {
		return nil
	}
	raw, exists := job["publication"]
	if !exists || raw == nil {
		return nil
	}
	return errors.New("native host execution cannot use isolated publication")
}

func splitNonEmptyLines(output string) []string {
	trimmed := strings.TrimSpace(output)
	if trimmed == "" {
		return nil
	}
	return strings.Split(trimmed, "\n")
}

func leasedJobFailureReason(job map[string]any, dockerOK bool) string {
	if jobHostExecProfileName(job) != "" {
		return ""
	}
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
	for _, key := range []string{"git_clone_config", "custom_commands"} {
		if value, ok := job[key]; ok && value != nil {
			if data, err := json.Marshal(value); err == nil {
				env[strings.ToUpper(key)] = string(data)
			}
		}
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
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, runnerDockerBinary(), "ps", "--quiet")
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

// mergeRunnerStateDoc renders state over the current runner.json, keeping
// the keys runnerState does not model (for example "sessions", written by
// `preloop runner sessions`) and the per-harness keys other commands own.
func mergeRunnerStateDoc(path string, state *runnerState) ([]byte, error) {
	encoded, err := json.Marshal(state)
	if err != nil {
		return nil, err
	}
	known := map[string]any{}
	if err := json.Unmarshal(encoded, &known); err != nil {
		return nil, err
	}
	doc := map[string]any{}
	if raw, err := os.ReadFile(path); err == nil && len(raw) > 0 {
		_ = json.Unmarshal(raw, &doc)
	}
	for _, key := range runnerStateKeys() {
		delete(doc, key)
	}
	for key, value := range known {
		doc[key] = value
	}
	return json.MarshalIndent(doc, "", "  ")
}

// runnerStateKeys are the top-level runner.json keys runnerState owns.
func runnerStateKeys() []string {
	t := reflect.TypeOf(runnerState{})
	keys := make([]string, 0, t.NumField())
	for i := 0; i < t.NumField(); i++ {
		name, _, _ := strings.Cut(t.Field(i).Tag.Get("json"), ",")
		if name != "" && name != "-" {
			keys = append(keys, name)
		}
	}
	return keys
}

// withRunnerStateLock runs fn holding the exclusive runner.json lock.
func withRunnerStateLock(fn func() error) (err error) {
	path, err := runnerStatePath()
	if err != nil {
		return err
	}
	if err = os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	lockFile, err := os.OpenFile(path+".lock", os.O_CREATE|os.O_RDWR, 0o600) //nolint:gosec
	if err != nil {
		return err
	}
	defer func() {
		if cerr := lockFile.Close(); err == nil && cerr != nil {
			err = cerr
		}
	}()
	if err = lockOperatorNoteFile(lockFile); err != nil {
		return err
	}
	defer unlockOperatorNoteFile(lockFile) //nolint:errcheck
	return fn()
}

// updateRunnerState is a read-modify-write of runner.json under an
// exclusive lock (runner.json.lock). Every writer that keeps fields it did
// not change goes through it, so `preloop runner dirs`, a token rotation
// and a registration cannot overwrite each other's change.
func updateRunnerState(fn func(state *runnerState) error) error {
	return withRunnerStateLock(func() error {
		state, err := readRunnerState()
		if err != nil {
			if !os.IsNotExist(err) {
				return err
			}
			state = &runnerState{}
		}
		if err := fn(state); err != nil {
			return err
		}
		return writeRunnerState(state)
	})
}

func writeRunnerState(state *runnerState) error {
	path, err := runnerStatePath()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	data, err := mergeRunnerStateDoc(path, state)
	if err != nil {
		return err
	}
	// Atomic: the running runner re-reads this file on every heartbeat for
	// the authorized directory list, and `preloop runner dirs` edits it
	// from another process.
	return writeFileAtomic(path, data, 0o600)
}

func runRunnerEnable(cmd *cobra.Command, args []string) error {
	return installRunnerService(cmd.OutOrStdout())
}

// installRunnerService writes the platform service unit and, on macOS,
// loads it. Linux enable does not start the unit. Windows creates the
// logon task and does not run it until start.
func installRunnerService(out io.Writer) error {
	bin, err := os.Executable()
	if err != nil {
		return err
	}
	switch runtime.GOOS {
	case "darwin":
		return writeLaunchdPlist(bin, out)
	case "linux":
		return writeSystemdUserUnit(bin, out)
	case "windows":
		return writeWindowsScheduledTask(bin, out)
	default:
		return fmt.Errorf("service install is not implemented on %s; use preloop runner fg", runtime.GOOS)
	}
}

// effectiveRunnerLabels uses --labels when the caller passed any, and
// otherwise the labels saved by `preloop runner setup` or login.
func effectiveRunnerLabels(flag []string) []string {
	if len(flag) > 0 {
		return flag
	}
	// Never nil: configs written before #1479 have no runner.labels, and
	// a nil slice marshals as null, which the register endpoint rejects.
	labels := config.RunnerLabels()
	if labels == nil {
		return []string{}
	}
	return labels
}

// Service hooks. Variables so tests can drive disable and rotate-token
// without touching launchd, systemd or the Windows task scheduler.
var (
	runnerServiceAction    = runnerServiceControl
	runnerServiceInstalled = runnerServiceIsInstalled
	runnerServiceRemove    = removeRunnerService
)

func runRunnerDisable(cmd *cobra.Command, args []string) error {
	deleteRunner, _ := cmd.Flags().GetBool("delete")
	force, _ := cmd.Flags().GetBool("force")
	if force && !deleteRunner {
		return errors.New("--force only applies together with --delete")
	}
	// Asked before the removal, which is what makes it false on macOS and
	// Linux. On Windows a missing task is a generic schtasks exit error, not
	// ErrNotExist, so the removal error alone cannot tell the two apart.
	installed := runnerServiceInstalled()
	// Stop first: a service still running would reconnect, or take a new
	// lease, between the delete and its removal.
	_ = runnerServiceAction("stop")
	removeErr := runnerServiceRemove()
	if !deleteRunner {
		return removeErr
	}
	if removeErr != nil && (!installed || errors.Is(removeErr, os.ErrNotExist)) {
		// No service was installed (a runner started with fg). Deleting the
		// server row is still what was asked for.
		removeErr = nil
	}
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return errors.Join(removeErr, err)
	}
	return errors.Join(removeErr, deleteRegisteredRunner(client, force, cmd.OutOrStdout()))
}

func removeRunnerService() error {
	switch runtime.GOOS {
	case "darwin":
		return os.Remove(launchdPlistPath())
	case "linux":
		return os.Remove(systemdUserUnitPath())
	case "windows":
		return removeWindowsScheduledTask()
	default:
		return fmt.Errorf("service install is not implemented on %s", runtime.GOOS)
	}
}

func runnerServiceIsInstalled() bool {
	switch runtime.GOOS {
	case "darwin":
		_, err := os.Stat(launchdPlistPath())
		return err == nil
	case "linux":
		_, err := os.Stat(systemdUserUnitPath())
		return err == nil
	case "windows":
		_, _, ok := findWindowsRunnerTask()
		return ok
	default:
		return false
	}
}

type runnerDeleteResponse struct {
	ID                 string   `json:"id"`
	Deleted            bool     `json:"deleted"`
	HaltedExecutionIDs []string `json:"halted_execution_ids"`
}

// deleteRegisteredRunner deletes the runner in the local state on the
// server and then forgets it locally. A runner the server no longer knows
// counts as deleted, so a retry after a partial failure converges.
func deleteRegisteredRunner(client *api.Client, force bool, out io.Writer) error {
	state, err := readRunnerState()
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return errors.New("no registered runner on this machine (runner.json not found)")
		}
		return fmt.Errorf("read runner state: %w", err)
	}
	if state.ID == "" {
		return errors.New("runner state has no runner id")
	}
	path := "/api/v1/runners/" + url.PathEscape(state.ID)
	if force {
		path += "?force=true"
	}
	var response runnerDeleteResponse
	err = client.Delete(path, &response)
	switch {
	case err == nil:
	case api.IsStatus(err, http.StatusNotFound):
		fmt.Fprintf(out, "Runner %s was already gone on the server\n", state.ID)
	case api.IsStatus(err, http.StatusConflict):
		return fmt.Errorf(
			"%w\nThe service is stopped. Re-run with --delete --force to halt those executions and delete the runner",
			err,
		)
	default:
		return fmt.Errorf("delete runner %s: %w", state.ID, err)
	}
	if statePath, pathErr := runnerStatePath(); pathErr == nil {
		if removeErr := os.Remove(statePath); removeErr != nil && !errors.Is(removeErr, os.ErrNotExist) {
			return fmt.Errorf("runner deleted, but removing %s failed: %w", statePath, removeErr)
		}
	}
	if err == nil {
		fmt.Fprintf(out, "Deleted runner %s\n", state.ID)
	}
	for _, executionID := range response.HaltedExecutionIDs {
		fmt.Fprintf(out, "Halted execution %s\n", executionID)
	}
	return nil
}

func runRunnerRotateToken(cmd *cobra.Command, args []string) error {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}
	return rotateRunnerToken(client, cmd.OutOrStdout())
}

// rotateRunnerToken swaps the runner token and restarts the service so
// the running process picks it up. The token itself is never printed: it
// only ever lives in runner.json.
func rotateRunnerToken(client *api.Client, out io.Writer) error {
	state, err := readRunnerState()
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return errors.New("no registered runner on this machine (runner.json not found)")
		}
		return fmt.Errorf("read runner state: %w", err)
	}
	if state.ID == "" {
		return errors.New("runner state has no runner id")
	}
	var rotated runnerAPIRecord
	if err := client.Post(
		"/api/v1/runners/"+url.PathEscape(state.ID)+"/token", nil, &rotated,
	); err != nil {
		return fmt.Errorf("rotate runner token: %w", err)
	}
	if rotated.Token == "" {
		return errors.New("rotate runner token: the server returned no token")
	}
	// Re-read under the lock: the state read before the request may be
	// stale by now (a `dirs add` in between).
	if err := updateRunnerState(func(current *runnerState) error {
		if current.ID == "" {
			current.ID, current.Name = state.ID, state.Name
		}
		current.Token = rotated.Token
		return nil
	}); err != nil {
		// The old token is already dead, so say where the new one went.
		return fmt.Errorf(
			"the server rotated the token but writing runner.json failed: %w. "+
				"Delete runner.json and start the runner again to register afresh",
			err,
		)
	}
	fmt.Fprintf(out, "Rotated the token for runner %s\n", state.ID)
	if !runnerServiceInstalled() {
		fmt.Fprintln(out, "No runner service is installed. Restart preloop runner fg to use the new token")
		return nil
	}
	if err := runnerServiceAction("restart"); err != nil {
		return fmt.Errorf("token rotated, but restarting the runner service failed: %w", err)
	}
	fmt.Fprintln(out, "Restarted the runner service")
	return nil
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
			if row.Capacity > 0 {
				fmt.Fprintf(
					cmd.OutOrStdout(),
					"running: %d/%d\n",
					row.RunningCount, row.Capacity,
				)
			}
			for _, executionID := range row.RunningExecutionIDs {
				fmt.Fprintf(cmd.OutOrStdout(), "execution: %s\n", executionID)
			}
			if len(row.RunningExecutionIDs) == 0 && row.CurrentExecutionID != nil {
				fmt.Fprintf(cmd.OutOrStdout(), "execution: %s\n", *row.CurrentExecutionID)
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

// runnerServiceLogPath is where the managed macOS and Windows services write
// runner output. systemd captures output in the journal, so Linux has no file.
func runnerServiceLogPath() (string, error) {
	dir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	return filepath.Join(dir, "runner.log"), nil
}

// xmlEscape escapes a value for embedding in the launchd property list.
func xmlEscape(value string) string {
	return strings.NewReplacer(
		"&", "&amp;", "<", "&lt;", ">", "&gt;",
	).Replace(value)
}

// launchdPlistBody renders the LaunchAgent. HOME is pinned to the
// installing user's home: launchd otherwise uses the login HOME, so a
// service installed from another HOME would read a different ~/.preloop
// (#1531). The agent runs in the user's
// login session so the operator's local agent CLI logins stay visible.
// launchd starts agents with a minimal PATH, so the common Homebrew and
// local-bin locations are appended for the runner's child processes.
func launchdPlistBody(bin, logPath, home string) string {
	// The plist targets macOS, so the PATH is joined with "/" regardless of
	// the OS this code compiles on (the unit test runs everywhere).
	path := strings.Join([]string{
		strings.TrimRight(home, "/") + "/.local/bin",
		strings.TrimRight(home, "/") + "/.opencode/bin",
		strings.TrimRight(home, "/") + "/.docker/bin",
		"/Applications/Docker.app/Contents/Resources/bin",
		"/opt/homebrew/bin",
		"/usr/local/bin",
		"/usr/bin", "/bin", "/usr/sbin", "/sbin",
	}, ":")
	return fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.preloop.runner</string>
  <key>ProgramArguments</key>
  <array><string>%s</string><string>runner</string><string>fg</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>%s</string>
  <key>StandardErrorPath</key><string>%s</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>%s</string>
    <key>HOME</key>
    <string>%s</string>
  </dict>
</dict>
</plist>
`, xmlEscape(bin), xmlEscape(logPath), xmlEscape(logPath), xmlEscape(path), xmlEscape(home))
}

func writeLaunchdPlist(bin string, out io.Writer) error {
	path := launchdPlistPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	logPath, err := runnerServiceLogPath()
	if err != nil {
		return err
	}
	home, _ := os.UserHomeDir()
	body := launchdPlistBody(bin, logPath, home)
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		return err
	}
	_ = exec.Command("launchctl", "load", path).Run()
	fmt.Fprintf(out, "Installed %s (logs: %s)\n", path, logPath)
	return nil
}

func writeSystemdUserUnit(bin string, out io.Writer) error {
	home, err := os.UserHomeDir()
	if err != nil {
		return fmt.Errorf("resolve home directory: %w", err)
	}
	path := systemdUserUnitPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	body := systemdUserUnitBody(bin, home)
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		return err
	}
	_ = exec.Command("systemctl", "--user", "daemon-reload").Run()
	_ = exec.Command("systemctl", "--user", "enable", "preloop-runner.service").Run()
	fmt.Fprintf(out, "Installed %s\n", path)
	return nil
}

// systemdUserUnitBody renders the user unit. HOME is pinned to the
// installing user's home so the service reads the same ~/.preloop config
// the install used, never another profile's (#1531).
func systemdUserUnitBody(bin, home string) string {
	return fmt.Sprintf(`[Unit]
Description=Preloop self-hosted runner
After=network-online.target

[Service]
Environment=%s
ExecStart=%s runner fg
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
`, strconv.Quote("HOME="+home), bin)
}

// windowsRunnerTaskScriptPath is the PowerShell launcher the scheduled task
// runs. A script file sidesteps schtasks /TR quoting limits and captures the
// runner's output to a log file, which a headless task otherwise discards.
func windowsRunnerTaskScriptPath() (string, error) {
	dir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "runner-task.ps1"), nil
}

// windowsRunnerTaskXMLPath is the task definition next to the launcher. It
// stays on disk after registration so an administrator can register the
// same task for this user when a policy blocks the user from doing it.
func windowsRunnerTaskXMLPath() (string, error) {
	dir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "runner-task.xml"), nil
}

// windowsRunnerTaskScript renders the launcher. Paths are single-quoted for
// PowerShell (embedded single quotes doubled), and *>> appends every output
// stream to the log.
// HOME and USERPROFILE are pinned to the installing user's home so the
// task reads the same config the install used (#1531).
func windowsRunnerTaskScript(bin, logPath, home string) string {
	quote := func(s string) string {
		return "'" + strings.ReplaceAll(s, "'", "''") + "'"
	}
	return "$env:HOME = " + quote(home) + "\n" +
		"$env:USERPROFILE = " + quote(home) + "\n" +
		"& " + quote(bin) + " runner fg *>> " + quote(logPath) + "\n"
}

// windowsPowerShellPath is the Windows PowerShell the task runs; the full
// path keeps the task independent of the PATH the Task Scheduler builds.
func windowsPowerShellPath() string {
	root := os.Getenv("SystemRoot")
	if root == "" {
		root = `C:\Windows`
	}
	return filepath.Join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
}

// Windows task hooks. Variables so tests drive enable, disable and status
// without schtasks or a real user account.
var (
	// runWindowsSchtasks runs schtasks with args and returns its decoded
	// combined output.
	runWindowsSchtasks = func(args ...string) (string, error) {
		raw, err := exec.Command("schtasks", args...).CombinedOutput()
		return decodeWindowsTaskOutput(raw), err
	}
	// windowsLogonTaskIdentity is the principal (DOMAIN\user) the task runs
	// as and the per-user task name.
	windowsLogonTaskIdentity = func() (string, string) {
		username := ""
		if current, err := user.Current(); err == nil {
			username = current.Username
		}
		if username == "" {
			username = os.Getenv("USERNAME")
		}
		machine := os.Getenv("USERDOMAIN")
		if machine == "" {
			machine, _ = os.Hostname()
		}
		return windowsLogonTaskUser(username, machine)
	}
)

// writeWindowsScheduledTask registers the logon task for the current user
// from an XML definition (see runner_logon_task.go): a standard user can
// do that without elevation, and the task runs as the user (not SYSTEM)
// in the interactive session, so the operator's harness logins stay
// visible to host execution and remote sessions.
func writeWindowsScheduledTask(bin string, out io.Writer) error {
	scriptPath, err := windowsRunnerTaskScriptPath()
	if err != nil {
		return err
	}
	logPath, err := runnerServiceLogPath()
	if err != nil {
		return err
	}
	home, _ := os.UserHomeDir()
	script := windowsRunnerTaskScript(bin, logPath, home)
	if err := os.WriteFile(scriptPath, []byte(script), 0o600); err != nil {
		return err
	}
	userID, taskName := windowsLogonTaskIdentity()
	xmlPath, err := windowsRunnerTaskXMLPath()
	if err != nil {
		return err
	}
	definition := windowsLogonTaskXML(windowsLogonTaskSpec{
		Name:       taskName,
		UserID:     userID,
		PowerShell: windowsPowerShellPath(),
		ScriptPath: scriptPath,
		WorkingDir: home,
	})
	if err := os.WriteFile(xmlPath, encodeUTF16LEWithBOM(definition), 0o600); err != nil {
		return err
	}
	output, err := runWindowsSchtasks("/Create", "/TN", taskName, "/XML", xmlPath, "/F")
	if err != nil {
		return windowsLogonTaskCreateError(taskName, xmlPath, output, err, windowsTaskCreationPolicy())
	}
	fmt.Fprintf(
		out,
		"Installed logon task %s for %s: runs in your interactive session without admin rights (logs: %s)\n",
		taskName, userID, logPath,
	)
	if taskName != windowsLegacyLogonTaskName {
		if warning := removeOwnLegacyWindowsTask(userID); warning != "" {
			fmt.Fprintln(out, warning)
		}
	}
	return nil
}

// windowsTaskBelongsTo reports whether a task definition is this user's:
// the principal matches (case-insensitively, as Windows compares account
// names) or the definition names no principal at all.
func windowsTaskBelongsTo(def windowsTaskDefinition, userID string) bool {
	return def.UserID == "" || strings.EqualFold(def.UserID, userID)
}

// removeOwnLegacyWindowsTask deletes the task 0.17.0 registered under the
// shared name when it belongs to userID: left in place it would start a
// second runner for this user at logon. Another account's task under that
// name (a co-worker on a shared machine) is never touched, even when this
// account could delete it. A failed delete is reported, not swallowed,
// because two runners would then start at the next logon.
func removeOwnLegacyWindowsTask(userID string) string {
	output, err := runWindowsSchtasks("/Query", "/TN", windowsLegacyLogonTaskName, "/XML", "ONE")
	if err != nil {
		// No legacy task, or none this account may see.
		return ""
	}
	def, parseErr := parseWindowsTaskXML([]byte(output))
	if parseErr != nil || !windowsTaskBelongsTo(def, userID) {
		return ""
	}
	if output, err := runWindowsSchtasks("/Delete", "/TN", windowsLegacyLogonTaskName, "/F"); err != nil {
		return fmt.Sprintf(
			"Warning: the task %s registered by an earlier CLI for this user could not be removed (%s); two runners would start at your next logon. Remove it with: schtasks /Delete /TN %s /F",
			windowsLegacyLogonTaskName, strings.TrimSpace(output), windowsLegacyLogonTaskName,
		)
	}
	return ""
}

// findWindowsRunnerTask locates this user's runner task: the per-user name
// first, then the 0.17.0 shared name when its principal is this user (a
// shared-name task owned by another user is not this runner).
func findWindowsRunnerTask() (string, windowsTaskDefinition, bool) {
	userID, taskName := windowsLogonTaskIdentity()
	names := []string{taskName}
	if taskName != windowsLegacyLogonTaskName {
		names = append(names, windowsLegacyLogonTaskName)
	}
	for _, name := range names {
		output, err := runWindowsSchtasks("/Query", "/TN", name, "/XML", "ONE")
		if err != nil {
			continue
		}
		def, err := parseWindowsTaskXML([]byte(output))
		if err != nil {
			// The task exists but the definition did not parse: it is still
			// a task under our name.
			def = windowsTaskDefinition{}
		}
		if name == windowsLegacyLogonTaskName && !windowsTaskBelongsTo(def, userID) {
			continue
		}
		return name, def, true
	}
	return taskName, windowsTaskDefinition{}, false
}

func removeWindowsScheduledTask() error {
	name, _, found := findWindowsRunnerTask()
	output, err := runWindowsSchtasks("/Delete", "/TN", name, "/F")
	if err != nil && found {
		err = fmt.Errorf("schtasks /Delete %s: %w (%s)", name, err, strings.TrimSpace(output))
	}
	if err != nil {
		return err
	}
	for _, pathFn := range []func() (string, error){windowsRunnerTaskScriptPath, windowsRunnerTaskXMLPath} {
		if path, pathErr := pathFn(); pathErr == nil {
			if removeErr := os.Remove(path); removeErr != nil && !os.IsNotExist(removeErr) {
				return removeErr
			}
		}
	}
	return nil
}

// windowsRunnerServiceState is the install: line on Windows plus the
// context lines: which user and session the runner runs in, and what lock,
// sign-out and a second user mean for it.
func windowsRunnerServiceState() string {
	name, def, found := findWindowsRunnerTask()
	if !found {
		return fmt.Sprintf("not installed (logon task %s; create it with preloop runner enable)", name)
	}
	status := ""
	if output, err := runWindowsSchtasks("/Query", "/TN", name, "/FO", "CSV", "/NH"); err == nil {
		status = windowsTaskStatusFromCSV(output)
	}
	first := "task " + name
	if status != "" {
		first += " (" + status + ")"
	}
	return strings.Join(append([]string{first}, windowsRunnerContextLines(name, def)...), "\n")
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
		name, _, _ := findWindowsRunnerTask()
		run := func(verb string) error {
			output, err := runWindowsSchtasks(verb, "/TN", name)
			if err != nil {
				return fmt.Errorf("schtasks %s %s: %w (%s)", verb, name, err, strings.TrimSpace(output))
			}
			return nil
		}
		switch action {
		case "start":
			return run("/Run")
		case "stop":
			return run("/End")
		case "restart":
			_ = run("/End")
			return run("/Run")
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
		return windowsRunnerServiceState()
	default:
		return "unsupported"
	}
}
