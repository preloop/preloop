package cmd

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

const (
	cursorAgentCommand  = "cursor-agent"
	cursorDefaultSource = "cursor"

	// cursorIngestTimeout bounds the usage POST after a headless run. The
	// child has already finished, so this is not on Cursor's editor hook
	// path; it still fail-opens rather than hanging the operator.
	cursorIngestTimeout = 10 * time.Second

	cursorInstallHint = "curl https://cursor.com/install -fsS | bash"
)

// cursorCmd launches the Cursor Agent CLI under Preloop.
//
// v1 is intentionally smaller than `preloop claude`: spawn, optional
// headless capture, ship estimated usage. Sidecar RPC, remote takeover,
// session modes, and pairing are out of scope.
var cursorCmd = &cobra.Command{
	Use:   "cursor [cursor-agent-args...]",
	Short: "Run the Cursor Agent CLI, optionally capturing estimated usage",
	Long: `Run the Cursor Agent CLI (cursor-agent) under Preloop.

Interactive mode is a TTY passthrough: Preloop does not observe token
usage, because cursor-agent only emits structured events in print mode.
Headless capture is a separate subcommand:

  preloop cursor                  # interactive TTY, no capture
  preloop cursor run "prompt"     # --print stream-json, capture, ship usage

Runs bill the user's own Cursor account. Preloop records estimated
usage, not Cursor billing. Quantities cursor-agent did not report are
omitted (shown as "not reported"), never invented as 0.

cursor-agent flags after 'cursor' or 'cursor run' are passed through.
Global Preloop flags (--token, --url) belong before 'cursor'.`,
	Args:               cobra.ArbitraryArgs,
	DisableFlagParsing: true,
	SilenceErrors:      true,
	RunE:               runCursorPassthrough,
}

var cursorRunCmd = &cobra.Command{
	Use:   "run [cursor-agent-args...]",
	Short: "Run cursor-agent headlessly and ship estimated usage",
	Long: `Run cursor-agent in print mode, capture structured output, and POST
estimated usage to /api/v1/usage/ingest.

Preloop injects --print --output-format stream-json unless those flags
are already present. stdout is still shown so scripts can consume the
JSON stream. Ingest failures print a warning and do not change the
child's exit status.

  preloop cursor run "summarize this repo"
  preloop cursor run --agent-id <uuid> --model gpt-5 "fix the tests"

--agent-id, --source, and --parent-conversation-id are Preloop flags;
everything else is passed to cursor-agent.`,
	Args:               cobra.ArbitraryArgs,
	DisableFlagParsing: true,
	SilenceErrors:      true,
	RunE:               runCursorCapture,
}

func init() {
	cursorRunCmd.Flags().String("agent-id", "", "managed agent UUID to attribute the events to (default: the onboarded Cursor agent)")
	cursorRunCmd.Flags().String("source", cursorDefaultSource, "origin label stored on each record")
	cursorRunCmd.Flags().String(
		"parent-conversation-id",
		"",
		"conversation this chat was spawned from (also PRELOOP_PARENT_CONVERSATION_ID)",
	)
	cursorCmd.AddCommand(cursorRunCmd)
	rootCmd.AddCommand(cursorCmd)
}

func runCursorPassthrough(cmd *cobra.Command, args []string) error {
	if len(args) == 1 && (args[0] == "--help" || args[0] == "-h") {
		return cmd.Help()
	}
	bin, err := findCursorAgent()
	if err != nil {
		fmt.Fprintln(cmd.ErrOrStderr(), err)
		return err
	}
	return printCursorPreloopError(cmd.ErrOrStderr(), runCursorAgent(bin, args, os.Stdin, os.Stdout, os.Stderr))
}

func runCursorCapture(cmd *cobra.Command, args []string) error {
	opts, err := parseCursorRunArgs(args)
	if err != nil {
		if err == errCursorRunHelp {
			return cmd.Help()
		}
		fmt.Fprintln(cmd.ErrOrStderr(), err)
		return err
	}
	err = runCursorCaptureWithIO(
		cmd.ErrOrStderr(),
		opts,
		os.Stdin,
		os.Stdout,
		os.Stderr,
		time.Now().UTC(),
		postUsageIngest,
	)
	return printCursorPreloopError(cmd.ErrOrStderr(), err)
}

// printCursorPreloopError writes Preloop-side failures (missing binary,
// start errors) to stderr. Child wrapProcessExit errors stay silent:
// cursor-agent already wrote its own diagnostics.
func printCursorPreloopError(w io.Writer, err error) error {
	if err == nil {
		return nil
	}
	var coded *processExitError
	if errors.As(err, &coded) {
		return err
	}
	fmt.Fprintln(w, err)
	return err
}

func runCursorCaptureWithIO(
	warn io.Writer,
	opts cursorRunOptions,
	stdin io.Reader,
	stdout, stderr io.Writer,
	started time.Time,
	ship func(source, agentID string, records []map[string]interface{}, timeout time.Duration) error,
) error {
	bin, err := findCursorAgent()
	if err != nil {
		return err
	}

	childArgs := ensureCursorCaptureArgs(opts.args)
	var captured bytes.Buffer
	waitErr := runCursorAgent(
		bin,
		childArgs,
		stdin,
		io.MultiWriter(stdout, &captured),
		stderr,
	)
	ended := time.Now().UTC()

	capture, parseErr := parseCursorAgentOutput(captured.Bytes())
	if parseErr != nil {
		fmt.Fprintf(warn, "preloop cursor: cursor-agent output truncated (usage not recorded)\n")
		return waitErr
	}
	records := buildCursorIngestRecords(
		capture,
		opts.parent,
		started,
		ended,
	)
	if len(records) == 0 {
		if captured.Len() > 0 {
			fmt.Fprintf(warn, "preloop cursor: no session_id in cursor-agent output (usage not recorded)\n")
		}
	} else if shipErr := ship(
		opts.source, opts.agentID, records, cursorIngestTimeout,
	); shipErr != nil {
		fmt.Fprintf(warn, "preloop cursor: %v (usage not recorded)\n", shipErr)
	}

	return waitErr
}

func findCursorAgent() (string, error) {
	path, err := resolveRuntimeExecutable(cursorAgentCommand)
	if err != nil {
		return "", fmt.Errorf(
			"cursor-agent was not found on %s; install the Cursor Agent CLI with: %s",
			runtimeExecutableSearchDescription(cursorAgentCommand),
			cursorInstallHint,
		)
	}
	return path, nil
}

func runCursorAgent(
	bin string,
	args []string,
	stdin io.Reader,
	stdout, stderr io.Writer,
) error {
	child := exec.Command(bin, args...)
	child.Stdin = stdin
	child.Stdout = stdout
	child.Stderr = stderr
	// No SysProcAttr: the child must inherit the launcher's process group
	// so an interactive TTY stays in the foreground (see startClaudeTUI).
	return wrapProcessExit(child.Run())
}

// errCursorRunHelp is returned by parseCursorRunArgs when the user asked
// for Preloop's help rather than cursor-agent's.
var errCursorRunHelp = fmt.Errorf("cursor run help")

type cursorRunOptions struct {
	agentID string
	source  string
	parent  string
	args    []string
}

func parseCursorRunArgs(args []string) (cursorRunOptions, error) {
	opts := cursorRunOptions{source: cursorDefaultSource}
	passthrough := make([]string, 0, len(args))
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if arg == "--" {
			passthrough = append(passthrough, args[i+1:]...)
			break
		}
		if arg == "--help" || arg == "-h" {
			return opts, errCursorRunHelp
		}
		name, value, hasValue := splitCursorRunFlag(arg)
		switch name {
		case "--agent-id", "--source", "--parent-conversation-id":
			if !hasValue {
				i++
				if i >= len(args) {
					return opts, fmt.Errorf("%s requires a value", name)
				}
				value = args[i]
			}
			switch name {
			case "--agent-id":
				opts.agentID = value
			case "--source":
				opts.source = value
			case "--parent-conversation-id":
				opts.parent = value
			}
		default:
			passthrough = append(passthrough, arg)
		}
	}
	if opts.parent == "" {
		opts.parent = os.Getenv("PRELOOP_PARENT_CONVERSATION_ID")
	}
	if opts.source == "" {
		opts.source = cursorDefaultSource
	}
	opts.args = passthrough
	return opts, nil
}

func splitCursorRunFlag(arg string) (name, value string, hasValue bool) {
	if !strings.HasPrefix(arg, "--") {
		return "", "", false
	}
	if eq := strings.IndexByte(arg, '='); eq >= 0 {
		return arg[:eq], arg[eq+1:], true
	}
	return arg, "", false
}

func ensureCursorCaptureArgs(args []string) []string {
	hasPrint := cursorArgsHasFlag(args, "--print", "-p")
	hasFormat := cursorArgsHasFlag(args, "--output-format")
	injected := make([]string, 0, 3+len(args))
	if !hasPrint {
		injected = append(injected, "--print")
	}
	if !hasFormat {
		injected = append(injected, "--output-format", "stream-json")
	}
	return append(injected, args...)
}

func cursorArgsHasFlag(args []string, names ...string) bool {
	for _, arg := range args {
		if arg == "--" {
			return false
		}
		for _, name := range names {
			if arg == name || strings.HasPrefix(arg, name+"=") {
				return true
			}
		}
	}
	return false
}

// cursorStreamUsage is the optional token object third-party captures of
// cursor-agent 2026.07.20 observed on the terminal result event
// (camelCase). Official CLI docs for json/stream-json do not list it; the
// parser accepts it when present and omits token fields when absent.
type cursorStreamUsage struct {
	InputTokens      *int `json:"inputTokens"`
	OutputTokens     *int `json:"outputTokens"`
	CacheReadTokens  *int `json:"cacheReadTokens"`
	CacheWriteTokens *int `json:"cacheWriteTokens"`
}

type cursorStreamEvent struct {
	Type       string             `json:"type"`
	Subtype    string             `json:"subtype"`
	SessionID  string             `json:"session_id"`
	Model      string             `json:"model"`
	RequestID  string             `json:"request_id"`
	IsError    bool               `json:"is_error"`
	DurationMS *int               `json:"duration_ms"`
	Usage      *cursorStreamUsage `json:"usage"`
}

type cursorCapture struct {
	SessionID  string
	Model      string
	RequestID  string
	HasInit    bool
	HasResult  bool
	ResultErr  bool
	DurationMS *int
	Usage      *cursorStreamUsage
}

func parseCursorAgentOutput(raw []byte) (cursorCapture, error) {
	var capture cursorCapture
	sawObject := false

	scanner := bufio.NewScanner(bytes.NewReader(raw))
	scanner.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
	for scanner.Scan() {
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 || line[0] != '{' {
			continue
		}
		var event cursorStreamEvent
		if err := json.Unmarshal(line, &event); err != nil {
			continue
		}
		sawObject = true
		applyCursorEvent(&capture, event)
	}
	if err := scanner.Err(); err != nil {
		// Token-too-long and other scan failures must not look like a
		// successful partial capture: the dropped line may have been the
		// result event with the only token counts.
		return cursorCapture{}, err
	}

	if sawObject {
		return capture, nil
	}

	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 || trimmed[0] != '{' {
		return capture, nil
	}
	var event cursorStreamEvent
	if err := json.Unmarshal(trimmed, &event); err != nil || event.Type == "" {
		return capture, nil
	}
	applyCursorEvent(&capture, event)
	return capture, nil
}

func applyCursorEvent(capture *cursorCapture, event cursorStreamEvent) {
	if event.SessionID != "" {
		capture.SessionID = event.SessionID
	}
	switch event.Type {
	case "system":
		if event.Subtype == "init" {
			capture.HasInit = true
			if event.Model != "" {
				capture.Model = event.Model
			}
		}
	case "result":
		capture.HasResult = true
		capture.ResultErr = event.IsError || event.Subtype == "error"
		if event.RequestID != "" {
			capture.RequestID = event.RequestID
		}
		if event.DurationMS != nil {
			capture.DurationMS = event.DurationMS
		}
		if event.Usage != nil {
			capture.Usage = event.Usage
		}
		if event.Model != "" {
			capture.Model = event.Model
		}
	}
}

func buildCursorIngestRecords(
	capture cursorCapture,
	parent string,
	started, ended time.Time,
) []map[string]interface{} {
	if capture.SessionID == "" {
		return nil
	}

	records := make([]map[string]interface{}, 0, 2)
	if capture.HasInit {
		records = append(records, cursorLifecycleRecord(
			"session_start",
			"sessionStart:"+capture.SessionID,
			capture,
			parent,
			started,
			false,
		))
	}
	if capture.HasResult {
		records = append(records, cursorResultRecord(capture, parent, ended))
	}
	if len(records) == 0 {
		// session_id observed on a non-init, non-result event (e.g. a
		// lone assistant line). Record a response so the conversation
		// still appears; no tokens.
		records = append(records, cursorLifecycleRecord(
			"response",
			cursorResultExternalID(capture, ended),
			capture,
			parent,
			ended,
			false,
		))
	}
	return records
}

func cursorResultRecord(
	capture cursorCapture,
	parent string,
	now time.Time,
) map[string]interface{} {
	hasMeasurement := cursorUsageHasMeasurement(capture.Usage)
	eventType := "response"
	includeUsage := hasMeasurement
	if hasMeasurement && capture.Model != "" {
		eventType = "usage"
	}
	record := cursorLifecycleRecord(
		eventType,
		cursorResultExternalID(capture, now),
		capture,
		parent,
		now,
		includeUsage,
	)
	return record
}

func cursorLifecycleRecord(
	eventType, externalID string,
	capture cursorCapture,
	parent string,
	now time.Time,
	includeUsage bool,
) map[string]interface{} {
	metadata := map[string]interface{}{
		"launcher": "preloop cursor run",
	}
	if capture.RequestID != "" {
		metadata["request_id"] = capture.RequestID
	}
	if capture.DurationMS != nil {
		metadata["duration_ms"] = *capture.DurationMS
	}
	if capture.ResultErr {
		metadata["cli_is_error"] = true
	}
	if capture.Usage != nil && capture.Usage.CacheWriteTokens != nil {
		// Ingest has no cache-write column; keep the observed value
		// without folding it into input_tokens.
		metadata["cache_write_tokens"] = *capture.Usage.CacheWriteTokens
	}

	record := map[string]interface{}{
		"external_id":     externalID,
		"conversation_id": capture.SessionID,
		"timestamp":       now.Format(time.RFC3339Nano),
		"event_type":      eventType,
		"cost_basis":      "estimated",
		"metadata":        metadata,
	}
	if parent != "" {
		record["parent_conversation_id"] = parent
	}
	if capture.Model != "" {
		record["model"] = capture.Model
	}
	if includeUsage && capture.Usage != nil {
		if capture.Usage.InputTokens != nil {
			record["input_tokens"] = *capture.Usage.InputTokens
		}
		if capture.Usage.OutputTokens != nil {
			record["output_tokens"] = *capture.Usage.OutputTokens
		}
		if capture.Usage.CacheReadTokens != nil {
			record["cache_read_tokens"] = *capture.Usage.CacheReadTokens
		}
	}
	return record
}

func cursorUsageHasMeasurement(usage *cursorStreamUsage) bool {
	if usage == nil {
		return false
	}
	return usage.InputTokens != nil ||
		usage.OutputTokens != nil ||
		usage.CacheReadTokens != nil
}

func cursorResultExternalID(capture cursorCapture, now time.Time) string {
	if capture.RequestID != "" {
		return "cursor-cli:result:" + capture.SessionID + ":" + capture.RequestID
	}
	return fmt.Sprintf(
		"cursor-cli:result:%s:%d",
		capture.SessionID,
		now.UnixNano(),
	)
}
