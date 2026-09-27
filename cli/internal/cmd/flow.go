package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/signal"
	"regexp"
	"strings"
	"syscall"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

const (
	flowsPath              = "/api/v1/flows"
	defaultFlowWaitTimeout = 60 * time.Minute
	flowPollInitial        = time.Second
	flowPollMax            = 5 * time.Second
	flowLogPageSize        = 500
	flowLogMaxPages        = 100
	flowListPageSize       = 1000
	flowListMaxPages       = 10
	// flowStopTimeout bounds the stop request sent on interrupt. CI runners
	// escalate a cancelled job to SIGKILL within seconds (GitHub Actions
	// sends SIGINT, then SIGTERM after 7.5s, then SIGKILL), so the stop has
	// to finish well inside that window.
	flowStopTimeout = 5 * time.Second
)

var uuidPattern = regexp.MustCompile(
	`(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`,
)

var (
	flowSleep = time.Sleep
	flowNow   = time.Now
	flowAfter = time.After
	// flowNotifyInterrupts subscribes to the signals that end a wait. Tests
	// swap it for a channel they control.
	flowNotifyInterrupts = defaultFlowNotifyInterrupts
)

func defaultFlowNotifyInterrupts() (<-chan os.Signal, func()) {
	ch := make(chan os.Signal, 2)
	signal.Notify(ch, os.Interrupt, syscall.SIGTERM)
	return ch, func() { signal.Stop(ch) }
}

var terminalFailureStatuses = map[string]bool{
	"FAILED":  true,
	"STOPPED": true,
	"TIMEOUT": true,
}

// flowCmd is the parent for flow operations.
var flowCmd = &cobra.Command{
	Use:   "flow",
	Short: "Trigger and inspect Preloop flows",
	Long:  `Trigger flow executions from CI or the command line.`,
}

// flowTriggerCmd implements `preloop flow trigger`.
var flowTriggerCmd = &cobra.Command{
	Use:   "trigger <flow-id-or-name>",
	Short: "Trigger a flow execution",
	Long: `Trigger a flow by id or name via POST /api/v1/flows/{flow_id}/trigger.

In CI (stdin is not a TTY) the command waits for a terminal status by default
and streams execution logs to stdout. The same logs remain visible in the
console execution view. Exit status is non-zero on FAILED, STOPPED, or TIMEOUT.

With --stop-on-interrupt (default on when stdin is not a TTY), SIGINT or
SIGTERM during --wait stops the execution on the server, prints its id and
final status, and exits non-zero. A cancelled CI job therefore does not leave
the run going. Without it, an interrupt only ends the CLI and the execution
keeps running.

Examples:
  preloop flow trigger pull-request-reviewer
  preloop flow trigger 11111111-2222-4333-8444-555555555555 --payload '{"ref":"main"}'
  cat event.json | preloop flow trigger pull-request-reviewer --payload -`,
	Args: cobra.ExactArgs(1),
	RunE: runFlowTrigger,
}

func init() {
	flowCmd.AddCommand(flowTriggerCmd)
	flowTriggerCmd.Flags().String("payload", "", "JSON trigger payload, or - to read stdin")
	flowTriggerCmd.Flags().Bool("wait", false, "stream logs until the execution finishes (default on when stdin is not a TTY)")
	flowTriggerCmd.Flags().String("runner", "", "pin the execution to a self-hosted runner id, name, or label")
	flowTriggerCmd.Flags().Duration("timeout", defaultFlowWaitTimeout, "how long --wait will poll before exiting")
	flowTriggerCmd.Flags().Bool("stop-on-interrupt", false, "stop the execution when --wait is interrupted by SIGINT or SIGTERM (default on when stdin is not a TTY)")
}

func runFlowTrigger(cmd *cobra.Command, args []string) error {
	runner, err := cmd.Flags().GetString("runner")
	if err != nil {
		return err
	}
	payloadFlag, err := cmd.Flags().GetString("payload")
	if err != nil {
		return err
	}
	payload, err := parseTriggerPayload(payloadFlag, os.Stdin)
	if err != nil {
		return err
	}
	if strings.TrimSpace(runner) != "" {
		if payload == nil {
			payload = map[string]any{}
		}
		payload["_runner"] = strings.TrimSpace(runner)
	}

	waitFlag, err := cmd.Flags().GetBool("wait")
	if err != nil {
		return err
	}
	isTTY := stdinIsTerminal()
	wait := shouldWaitDefault(cmd.Flags().Changed("wait"), waitFlag, isTTY)
	stopFlag, err := cmd.Flags().GetBool("stop-on-interrupt")
	if err != nil {
		return err
	}
	stopOnInterrupt := shouldWaitDefault(cmd.Flags().Changed("stop-on-interrupt"), stopFlag, isTTY)
	timeout, err := cmd.Flags().GetDuration("timeout")
	if err != nil {
		return err
	}

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}

	flowID, err := resolveFlowID(client, args[0])
	if err != nil {
		return err
	}

	var result flowTriggerResult
	if err := client.Post(flowsPath+"/"+flowID+"/trigger", payload, &result); err != nil {
		return fmt.Errorf("failed to trigger flow: %w", err)
	}
	if result.ID == "" {
		return fmt.Errorf("trigger response did not include an execution id")
	}

	fmt.Fprintf(cmd.OutOrStdout(), "Triggered flow %s (execution %s, status %s)\n",
		flowID, result.ID, result.Status)

	if !wait {
		return nil
	}
	if !stopOnInterrupt {
		return waitForExecution(client, result.ID, timeout, cmd.OutOrStdout())
	}
	interrupts, release := flowNotifyInterrupts()
	defer release()
	return waitForExecutionUntil(client, result.ID, timeout, cmd.OutOrStdout(), interrupts)
}

type flowTriggerResult struct {
	ID     string `json:"id"`
	Status string `json:"status"`
	FlowID string `json:"flow_id"`
}

type flowStopResult struct {
	Status          string `json:"status"`
	ExecutionStatus string `json:"execution_status"`
}

type flowExecutionStatus struct {
	ID     string `json:"id"`
	Status string `json:"status"`
}

type flowLogsResponse struct {
	Logs    []flowLogEntry `json:"logs"`
	Source  string         `json:"source"`
	HasMore bool           `json:"has_more"`
}

type flowLogEntry struct {
	Type    string         `json:"type"`
	Payload map[string]any `json:"payload"`
}

func parseTriggerPayload(raw string, stdin io.Reader) (map[string]any, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	var r io.Reader
	if raw == "-" {
		r = stdin
	} else {
		r = strings.NewReader(raw)
	}
	data, err := io.ReadAll(r)
	if err != nil {
		return nil, fmt.Errorf("read payload: %w", err)
	}
	data = bytesTrimSpace(data)
	if len(data) == 0 {
		return nil, nil
	}
	var payload map[string]any
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("payload must be a JSON object: %w", err)
	}
	return payload, nil
}

func bytesTrimSpace(data []byte) []byte {
	return []byte(strings.TrimSpace(string(data)))
}

func resolveFlowID(client *api.Client, nameOrID string) (string, error) {
	nameOrID = strings.TrimSpace(nameOrID)
	if nameOrID == "" {
		return "", fmt.Errorf("flow id or name is required")
	}
	if uuidPattern.MatchString(nameOrID) {
		var flow flowSummaryResponse
		if err := client.Get(flowsPath+"/"+nameOrID, &flow); err == nil && flow.ID != "" {
			return flow.ID, nil
		}
	}

	var flows []flowSummaryResponse
	for page := 0; page < flowListMaxPages; page++ {
		var batch []flowSummaryResponse
		path := fmt.Sprintf("%s?skip=%d&limit=%d", flowsPath, page*flowListPageSize, flowListPageSize)
		if err := client.Get(path, &batch); err != nil {
			return "", fmt.Errorf("failed to list flows: %w", err)
		}
		flows = append(flows, batch...)
		if len(batch) < flowListPageSize {
			break
		}
	}
	var matches []flowSummaryResponse
	for _, flow := range flows {
		if strings.EqualFold(flow.ID, nameOrID) || strings.EqualFold(flow.Name, nameOrID) {
			matches = append(matches, flow)
		}
	}
	if len(matches) == 1 {
		return matches[0].ID, nil
	}
	if len(matches) > 1 {
		return "", fmt.Errorf("multiple flows match %q", nameOrID)
	}
	return "", fmt.Errorf("flow %q not found", nameOrID)
}

func shouldWaitDefault(flagChanged, flagValue, isTTY bool) bool {
	if flagChanged {
		return flagValue
	}
	return !isTTY
}

func extractLogLine(entry flowLogEntry) string {
	if entry.Payload == nil {
		return ""
	}
	if line, ok := entry.Payload["line"].(string); ok && line != "" {
		return line
	}
	if msg, ok := entry.Payload["message"].(string); ok && msg != "" {
		return msg
	}
	return ""
}

func newLogLines(source string, alreadyPrinted int, entries []flowLogEntry) []string {
	lines := make([]string, 0, len(entries))
	for _, entry := range entries {
		if line := extractLogLine(entry); line != "" {
			lines = append(lines, line)
		}
	}
	if strings.EqualFold(source, "container") && alreadyPrinted > 0 && len(lines) >= alreadyPrinted {
		return lines[alreadyPrinted:]
	}
	return lines
}

func drainExecutionLogs(client *api.Client, executionID string, printed int, out io.Writer) (int, error) {
	for page := 0; page < flowLogMaxPages; page++ {
		var logs flowLogsResponse
		path := fmt.Sprintf("/api/v1/flows/executions/%s/logs?skip=%d&limit=%d", executionID, printed, flowLogPageSize)
		if err := client.Get(path, &logs); err != nil {
			return printed, fmt.Errorf("failed to read execution logs: %w", err)
		}
		for _, line := range newLogLines(logs.Source, printed, logs.Logs) {
			fmt.Fprintln(out, line)
			printed++
		}
		if !logs.HasMore {
			return printed, nil
		}
	}
	return printed, nil
}

func waitForExecution(client *api.Client, executionID string, timeout time.Duration, out io.Writer) error {
	return waitForExecutionUntil(client, executionID, timeout, out, nil)
}

// waitForExecutionUntil polls like waitForExecution. When interrupts is not
// nil, a signal on it stops the execution on the server (once) and ends the
// wait with a non-zero exit instead of leaving the run going.
func waitForExecutionUntil(
	client *api.Client,
	executionID string,
	timeout time.Duration,
	out io.Writer,
	interrupts <-chan os.Signal,
) error {
	deadline := flowNow().Add(timeout)
	printed := 0
	backoff := flowPollInitial

	for {
		if sig := pendingInterrupt(interrupts); sig != nil {
			return stopInterruptedExecution(client, executionID, sig, out)
		}

		var exec flowExecutionStatus
		if err := client.Get("/api/v1/flows/executions/"+executionID, &exec); err != nil {
			return fmt.Errorf("failed to read execution: %w", err)
		}

		nextPrinted, err := drainExecutionLogs(client, executionID, printed, out)
		if err != nil {
			return err
		}
		printed = nextPrinted

		status := strings.ToUpper(strings.TrimSpace(exec.Status))
		if status == "SUCCEEDED" {
			return nil
		}
		if terminalFailureStatuses[status] {
			return fmt.Errorf("execution %s %s", executionID, status)
		}
		if flowNow().After(deadline) {
			return fmt.Errorf("execution %s timed out after %s (last status %s)", executionID, timeout, exec.Status)
		}

		if sig := sleepOrInterrupt(backoff, interrupts); sig != nil {
			return stopInterruptedExecution(client, executionID, sig, out)
		}
		if backoff < flowPollMax {
			backoff *= 2
			if backoff > flowPollMax {
				backoff = flowPollMax
			}
		}
	}
}

func pendingInterrupt(interrupts <-chan os.Signal) os.Signal {
	if interrupts == nil {
		return nil
	}
	select {
	case sig := <-interrupts:
		return sig
	default:
		return nil
	}
}

func sleepOrInterrupt(d time.Duration, interrupts <-chan os.Signal) os.Signal {
	if interrupts == nil {
		flowSleep(d)
		return nil
	}
	select {
	case sig := <-interrupts:
		return sig
	case <-flowAfter(d):
		return nil
	}
}

// stopInterruptedExecution sends exactly one stop for executionID, reports
// the final status, and returns an error carrying the conventional exit code
// for the signal (130 for SIGINT, 143 for SIGTERM). Further signals that
// arrive while the stop is in flight are ignored so a runner's SIGINT then
// SIGTERM escalation does not cut the stop short.
func stopInterruptedExecution(client *api.Client, executionID string, sig os.Signal, out io.Writer) error {
	code := 130
	if sig == syscall.SIGTERM {
		code = 143
	}
	fmt.Fprintf(out, "Received %s, stopping execution %s\n", sig, executionID)

	client.SetTimeout(flowStopTimeout)
	var stopped flowStopResult
	path := "/api/v1/flows/executions/" + executionID + "/command"
	if err := client.Post(path, map[string]any{"command": "stop"}, &stopped); err != nil {
		return &processExitError{
			code: code,
			err: fmt.Errorf(
				"interrupted; failed to stop execution %s, it may still be running: %w",
				executionID, err,
			),
		}
	}

	final := strings.ToUpper(strings.TrimSpace(stopped.ExecutionStatus))
	if final == "" {
		var exec flowExecutionStatus
		if err := client.Get("/api/v1/flows/executions/"+executionID, &exec); err == nil {
			final = strings.ToUpper(strings.TrimSpace(exec.Status))
		}
	}
	if final == "" {
		final = "UNKNOWN"
	}
	if stopped.Status == "not_running" {
		fmt.Fprintf(out, "Execution %s had already finished (final status %s)\n", executionID, final)
	} else {
		fmt.Fprintf(out, "Stopped execution %s (final status %s)\n", executionID, final)
	}
	return &processExitError{
		code: code,
		err:  fmt.Errorf("interrupted by %s; execution %s final status %s", sig, executionID, final),
	}
}
