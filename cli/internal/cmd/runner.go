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
	runnerFgCmd.Flags().Int(
		"concurrency",
		0,
		"executions to run at once (default: runner.concurrency in config, else 2)",
	)
}

type runnerState struct {
	ID    string `json:"id"`
	Token string `json:"token"`
	Name  string `json:"name"`
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

	Type string `json:"type"`
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
	labels, _ := cmd.Flags().GetStringSlice("labels")
	name, _ := cmd.Flags().GetString("name")
	requested, _ := cmd.Flags().GetInt("concurrency")
	hostname, _ := os.Hostname()
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
	fmt.Fprintf(
		cmd.OutOrStdout(),
		"Runner %s (%s) connecting with %d slots...\n",
		state.Name, state.ID, concurrency,
	)

	reapOrphanedPublicationRuntimes()

	interrupt := make(chan os.Signal, 1)
	signal.Notify(interrupt, os.Interrupt, syscall.SIGTERM)
	return runnerForegroundLoop(state, interrupt, cmd.OutOrStdout(), concurrency)
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
		"host_exec_profiles": hostExecAdvertisements(),
		"name":               name,
		"hostname":           hostname,
		"os":                 runtime.GOOS,
		"arch":               runtime.GOARCH,
		"labels":             labels,
		"concurrency":        concurrency,
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
	return writeRunnerJSON(conn, message)
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
	backoff := runnerReconnectMin
	connectedOnce := false

	for {
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
		err = runRunnerSession(conn, interrupt, out, jobs)
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
	_ = conn.WriteJSON(map[string]any{"type": "unregister"})
}

func runRunnerSession(
	conn *websocket.Conn,
	interrupt <-chan os.Signal,
	out io.Writer,
	jobs *runnerJobs,
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

	if err := writeRunnerJSON(conn, runnerHeartbeatMessage(jobs.concurrency)); err != nil {
		return fmt.Errorf("initial heartbeat: %w", err)
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
		case <-ticker.C:
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
			if msg.Type == "hello" {
				logAcknowledgements = msg.LogAcknowledgements
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
		if hostDir, persistErr := preparePersistWorkspace(executionID, resumeFrom); persistErr == nil {
			opts.WorkspaceHostDir = hostDir
			_ = touchWorkspaceLease(executionID)
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
