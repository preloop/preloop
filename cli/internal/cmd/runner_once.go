package cmd

// One-shot ephemeral runner mode: `preloop runner fg --once --ephemeral`.
//
// A CI job wants exactly one execution and nothing left behind. The mode
// state lives in a package variable rather than in the foreground loop's
// signature: `runner fg` is a single-process command, and threading a mode
// argument through runnerForegroundLoop, runRunnerSession, beginLeasedJob
// and writeJobOutcome would rewrite every call site for a feature that is
// off by default. Every method is nil safe, so the ordinary runner path
// behaves exactly as before.

import (
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"sync/atomic"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

const (
	// defaultRunnerWaitForJob bounds how long --once waits for a lease.
	defaultRunnerWaitForJob = 15 * time.Minute

	// runnerOnceExitFailed is the status for an execution that did not
	// succeed (FAILED, STOPPED, TIMEOUT, or a runner stopped mid-job).
	runnerOnceExitFailed = 1

	// runnerOnceExitNoJob is the status for "nothing was leased in time".
	// A CI job has to tell an empty queue apart from a failed review.
	runnerOnceExitNoJob = 2
)

// errRunnerOnceDone unwinds the session loop once the single execution of
// --once has reported its terminal outcome. It is never shown to a user:
// runnerForegroundLoop turns it into a clean return.
var errRunnerOnceDone = errors.New("runner one-shot execution finished")

// runnerOnce carries the flags of the current `runner fg` process. nil
// until runRunnerFg sets it, which is what tests rely on.
var runnerOnce *runnerOnceMode

// runnerOnceMode is the --once/--ephemeral state of one foreground run.
type runnerOnceMode struct {
	once       bool
	ephemeral  bool
	waitForJob time.Duration
	out        io.Writer

	leased      atomic.Bool
	done        atomic.Bool
	warnedOld   atomic.Bool
	status      atomic.Value
	executionID atomic.Value
}

// runnerOnceFromFlags reads the one-shot flags off `runner fg`.
func runnerOnceFromFlags(cmd *cobra.Command) (*runnerOnceMode, error) {
	once, err := cmd.Flags().GetBool("once")
	if err != nil {
		return nil, err
	}
	ephemeral, err := cmd.Flags().GetBool("ephemeral")
	if err != nil {
		return nil, err
	}
	waitForJob, err := cmd.Flags().GetDuration("wait-for-job")
	if err != nil {
		return nil, err
	}
	// A --wait-for-job without --once would silently do nothing: the loop
	// it bounds only exists in one-shot mode. Say so instead.
	if cmd.Flags().Changed("wait-for-job") && !once {
		return nil, errors.New("--wait-for-job requires --once")
	}
	if waitForJob <= 0 {
		return nil, errors.New("--wait-for-job must be positive")
	}
	return &runnerOnceMode{
		once:       once,
		ephemeral:  ephemeral,
		waitForJob: waitForJob,
		out:        cmd.OutOrStdout(),
	}, nil
}

// registersEphemeral reports whether registration is for this process only.
func (m *runnerOnceMode) registersEphemeral() bool {
	return m != nil && m.ephemeral
}

// stopsAfterOneJob reports whether the process exits after one execution.
func (m *runnerOnceMode) stopsAfterOneJob() bool {
	return m != nil && m.once
}

// noteHelloEphemeral consumes the control plane's echo of the ephemeral
// flag. A control plane that predates one-shot mode accepts the extra
// field on register, ignores it, and then keeps the row forever as an
// offline phantom. The job itself still runs, so this warns once on the
// first hello instead of failing: a CI log that ends green while leaving
// a dead runner on the Runners page is the outcome worth naming.
func (m *runnerOnceMode) noteHelloEphemeral(echoed bool) {
	if !m.registersEphemeral() || echoed {
		return
	}
	if m.warnedOld.Swap(true) || m.out == nil {
		return
	}
	fmt.Fprintln(
		m.out,
		"Warning: this control plane did not confirm ephemeral registration. "+
			"It is older than one-shot runner support, so this runner will "+
			"stay listed after the job. Remove it by hand, or upgrade the "+
			"control plane.",
	)
}

// markLeased records that the single execution started and prints its
// console URL, so a CI log links to the run before any agent output.
func (m *runnerOnceMode) markLeased(executionID string) {
	if m == nil || executionID == "" {
		return
	}
	m.executionID.Store(executionID)
	if m.leased.Swap(true) || m.out == nil {
		return
	}
	if url := runnerExecutionURL(executionID); url != "" {
		fmt.Fprintf(m.out, "Execution URL: %s\n", url)
	}
}

// record stores the terminal outcome of the one execution. It returns true
// when the caller should stop the foreground loop, which is only ever the
// first terminal report in --once mode.
func (m *runnerOnceMode) record(outcome leasedJobOutcome) bool {
	if !m.stopsAfterOneJob() || outcome.executionID == "" {
		return false
	}
	if m.done.Swap(true) {
		return false
	}
	m.executionID.Store(outcome.executionID)
	m.status.Store(strings.ToUpper(strings.TrimSpace(outcome.status)))
	return true
}

// armWaitForJob stops an idle one-shot runner after --wait-for-job. It
// reuses the interrupt channel so the wait ends on exactly the shutdown
// path a SIGTERM takes: halt, unregister, return.
func (m *runnerOnceMode) armWaitForJob(interrupt chan<- os.Signal) func() {
	if !m.stopsAfterOneJob() {
		return func() {}
	}
	timer := time.AfterFunc(m.waitForJob, func() {
		if m.leased.Load() {
			return
		}
		select {
		case interrupt <- os.Interrupt:
		default:
		}
	})
	return func() { timer.Stop() }
}

// result is the process status of a one-shot run.
func (m *runnerOnceMode) result() error {
	if !m.stopsAfterOneJob() {
		return nil
	}
	executionID, _ := m.executionID.Load().(string)
	if !m.done.Load() {
		if m.leased.Load() {
			return &exitCodeError{
				code: runnerOnceExitFailed,
				message: fmt.Sprintf(
					"runner stopped before execution %s finished", executionID,
				),
			}
		}
		return &exitCodeError{
			code: runnerOnceExitNoJob,
			message: fmt.Sprintf(
				"no execution was leased within %s", m.waitForJob,
			),
		}
	}
	status, _ := m.status.Load().(string)
	if status == "SUCCEEDED" {
		if m.out != nil {
			fmt.Fprintf(m.out, "Execution %s SUCCEEDED\n", executionID)
		}
		return nil
	}
	if status == "" {
		status = "UNKNOWN"
	}
	return &exitCodeError{
		code:    runnerOnceExitFailed,
		message: fmt.Sprintf("execution %s %s", executionID, status),
	}
}

// ephemeralRunnerLabels defaults an ephemeral runner to a label nothing
// else can match, so a flow pinned to `ci-<host>-<pid>` reaches this
// process and no other. Explicit --labels win.
func ephemeralRunnerLabels(labels []string, hostname string, pid int) []string {
	for _, label := range labels {
		if strings.TrimSpace(label) != "" {
			return labels
		}
	}
	host := strings.TrimSpace(hostname)
	if host == "" {
		host = "runner"
	}
	return []string{fmt.Sprintf("ci-%s-%d", host, pid)}
}

// registerEphemeralRunner registers a runner that belongs to this process
// alone: it never resumes a stored id and never writes runner.json, so an
// ephemeral CI run cannot take over or overwrite a persistent runner's
// identity on the same machine.
func registerEphemeralRunner(
	client *api.Client, req map[string]any,
) (*runnerState, error) {
	req["ephemeral"] = true
	delete(req, "runner_id")
	var created runnerAPIRecord
	if err := client.Post(api.RESTPrefix+"/runners/register", req, &created); err != nil {
		return nil, fmt.Errorf("register runner: %w", err)
	}
	if created.ID == "" || created.Token == "" {
		return nil, fmt.Errorf("register runner: server returned no runner identity")
	}
	return &runnerState{
		ID: created.ID, Token: created.Token, Name: created.Name,
	}, nil
}

// runnerExecutionURL is the console page for one execution on the control
// plane this runner is connected to. Empty when the URL cannot be resolved,
// which must never stop a job from running.
func runnerExecutionURL(executionID string) string {
	apiURL, err := runnerControlPlaneURL()
	if err != nil || apiURL == "" {
		return ""
	}
	return strings.TrimRight(apiURL, "/") + "/console/flows/executions/" + executionID
}
