package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/version"
)

// Tool-end and wait-for-input signals for the console's agent state.
//
// PostToolUse and PostToolUseFailure say a tool call finished, and the
// Notification hook says Claude Code is waiting for the operator. The hook
// posts one small record to /agents/session-event so the console can show
// "Working" versus "Your turn" without guessing from recency. It fails open:
// any error is logged to stderr and the hook exits 0 within its 5 s budget.

const agentSessionEventPath = "/api/v1/agents/session-event"

// activityHookTimeoutSeconds is the host-side timeout written into settings.
const activityHookTimeoutSeconds = 5

// activityHookHTTPTimeout keeps the request inside the host timeout.
const activityHookHTTPTimeout = 3 * time.Second

// activityHookEvents are the Claude Code events wired to the activity hook,
// with their matcher.
var activityHookEvents = []struct{ event, matcher string }{
	{"PostToolUse", "*"},
	{"PostToolUseFailure", "*"},
	{"Notification", ""},
}

func activityHookEventNames() []string {
	names := make([]string, 0, len(activityHookEvents))
	for _, hook := range activityHookEvents {
		names = append(names, hook.event)
	}
	return names
}

// activityHookCommand carries the permission-hook ownership marker, so
// offboarding removes it with the others.
func activityHookCommand(source string) string {
	return fmt.Sprintf("%s agents permission-hook activity --source %s", executableForApprovalHookSource(source), source)
}

var agentsActivityHookCmd = &cobra.Command{
	Use:    "activity",
	Short:  "Report tool ends and waits for input (PostToolUse / Notification hook)",
	Hidden: true,
	Args:   cobra.NoArgs,
	RunE:   runAgentsActivityHook,
}

func init() {
	agentsPermissionHookCmd.AddCommand(agentsActivityHookCmd)
	agentsActivityHookCmd.Flags().String("source", permissionSourceClaudeCode, "agent source, e.g. claude_code")
}

type activityHookInput struct {
	HookEventName    string          `json:"hook_event_name"`
	ToolName         string          `json:"tool_name"`
	ToolUseID        string          `json:"tool_use_id"`
	ToolResponse     json.RawMessage `json:"tool_response"`
	DurationMS       *int64          `json:"duration_ms"`
	NotificationType string          `json:"notification_type"`
	Message          string          `json:"message"`
}

type sessionEventRequest struct {
	SessionID        string `json:"session_id"`
	HookEventName    string `json:"hook_event_name"`
	Source           string `json:"source,omitempty"`
	ToolUseID        string `json:"tool_use_id,omitempty"`
	ToolName         string `json:"tool_name,omitempty"`
	ExitCode         *int   `json:"exit_code,omitempty"`
	IsError          *bool  `json:"is_error,omitempty"`
	DurationMS       *int64 `json:"duration_ms,omitempty"`
	NotificationType string `json:"notification_type,omitempty"`
	Interrupted      *bool  `json:"interrupted,omitempty"`
}

// buildSessionEventRequest maps one hook payload to the session-event body.
// It returns false when the event carries nothing the server records.
func buildSessionEventRequest(source string, raw []byte) (sessionEventRequest, bool) {
	var in activityHookInput
	if json.Unmarshal(raw, &in) != nil {
		return sessionEventRequest{}, false
	}
	sessionID := strings.TrimSpace(hookEventSessionID(raw))
	if !sessionFileIDPattern.MatchString(sessionID) {
		return sessionEventRequest{}, false
	}
	req := sessionEventRequest{SessionID: sessionID, HookEventName: in.HookEventName, Source: source}
	switch in.HookEventName {
	case "PostToolUse", "PostToolUseFailure":
		req.ToolUseID = truncateUTF8(strings.TrimSpace(in.ToolUseID), 200)
		req.ToolName = truncateUTF8(strings.TrimSpace(in.ToolName), 100)
		req.DurationMS = in.DurationMS
		// No output text leaves the machine: only the exit code and whether
		// the call failed.
		exitCode, isError := toolResponseOutcome(in.ToolResponse)
		if in.HookEventName == "PostToolUseFailure" {
			failed := true
			isError = &failed
		}
		req.ExitCode = exitCode
		req.IsError = isError
	case "Notification":
		kind := strings.TrimSpace(in.NotificationType)
		if kind == "" && strings.Contains(strings.ToLower(in.Message), "waiting for your input") {
			kind = "idle_prompt"
		}
		if kind == "" {
			return sessionEventRequest{}, false
		}
		req.NotificationType = truncateUTF8(kind, 64)
	default:
		return sessionEventRequest{}, false
	}
	return req, true
}

// toolResponseOutcome reads a shell's exit code and an explicit error flag
// from the tool response. Unknown shapes yield nothing.
func toolResponseOutcome(raw json.RawMessage) (*int, *bool) {
	if len(raw) == 0 {
		return nil, nil
	}
	var obj map[string]interface{}
	if json.Unmarshal(raw, &obj) != nil {
		return nil, nil
	}
	var exitCode *int
	for _, key := range []string{"exit_code", "exitCode", "returncode"} {
		if value, ok := obj[key].(float64); ok {
			code := int(value)
			exitCode = &code
			break
		}
	}
	var isError *bool
	if value, ok := obj["is_error"].(bool); ok {
		isError = &value
	}
	return exitCode, isError
}

func postSessionEvent(cred permissionHookCredential, req sessionEventRequest) error {
	body, err := json.Marshal(req)
	if err != nil {
		return err
	}
	url := strings.TrimRight(cred.BaseURL, "/") + agentSessionEventPath
	httpReq, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	version.SetClientIdentityHeaders(httpReq.Header)
	httpReq.Header.Set("Authorization", "Bearer "+cred.Token)
	resp, err := (&http.Client{Timeout: activityHookHTTPTimeout}).Do(httpReq)
	if err != nil {
		return err
	}
	defer resp.Body.Close() //nolint:errcheck
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("API error (status %d)", resp.StatusCode)
	}
	return nil
}

func runAgentsActivityHook(cmd *cobra.Command, _ []string) error {
	errOut := cmd.ErrOrStderr()
	source := normalizePermissionSource(mustFlagString(cmd, "source"))
	if source == "" {
		source = permissionSourceClaudeCode
	}
	if source == permissionSourceClaudeCode && isCursorHostInvokingClaudeHook() {
		return nil
	}
	raw, err := io.ReadAll(io.LimitReader(cmd.InOrStdin(), 1<<20))
	if err != nil {
		return nil
	}
	req, ok := buildSessionEventRequest(source, raw)
	if !ok {
		return nil
	}
	cred, err := resolvePermissionHookCredential(source)
	if err != nil {
		return nil
	}
	if err := postSessionEvent(cred, req); err != nil {
		fmt.Fprintf(errOut, "preloop activity hook: %v\n", err)
	}
	return nil
}

// interruptTranscriptMarker is what Claude Code writes into the transcript
// when the operator interrupts a turn.
const interruptTranscriptMarker = "[Request interrupted by user"

// transcriptTailBytes bounds how much of the transcript the Stop hook reads.
const transcriptTailBytes = 16 * 1024

// transcriptShowsInterrupt reports whether the newest transcript lines say
// the operator interrupted the turn. Any read error means "no".
func transcriptShowsInterrupt(path string) bool {
	path = strings.TrimSpace(path)
	if path == "" {
		return false
	}
	file, err := os.Open(path)
	if err != nil {
		return false
	}
	defer file.Close() //nolint:errcheck
	info, err := file.Stat()
	if err != nil || info.IsDir() {
		return false
	}
	offset := info.Size() - transcriptTailBytes
	if offset < 0 {
		offset = 0
	}
	buf := make([]byte, info.Size()-offset)
	if _, err := file.ReadAt(buf, offset); err != nil && err != io.EOF {
		return false
	}
	lines := strings.Split(strings.TrimRight(string(buf), "\n"), "\n")
	// Only the last few records: an interrupt earlier in the session does not
	// make this turn interrupted.
	start := len(lines) - 3
	if start < 0 {
		start = 0
	}
	for _, line := range lines[start:] {
		if strings.Contains(line, interruptTranscriptMarker) {
			return true
		}
	}
	return false
}

// reportTurnEnd posts the Stop that ended a turn to /agents/session-event, so
// the console can show "Your turn" and the turn footer. Best effort: errors
// go to stderr and never change the hook's answer.
func reportTurnEnd(cmd *cobra.Command, raw []byte, hookEventName, sessionID string) {
	if !sessionFileIDPattern.MatchString(sessionID) {
		return
	}
	name := strings.TrimSpace(hookEventName)
	if name != "stop" {
		name = "Stop"
	}
	cred, ok := hookCredentialForCommand(cmd)
	if !ok {
		return
	}
	req := sessionEventRequest{SessionID: sessionID, HookEventName: name, Source: cred.Source}
	if cred.Source == permissionSourceClaudeCode && transcriptShowsInterrupt(hookEventFieldFromRaw(raw, "transcript_path")) {
		interrupted := true
		req.Interrupted = &interrupted
	}
	if err := postSessionEvent(cred, req); err != nil {
		fmt.Fprintf(cmd.ErrOrStderr(), "preloop stop hook: could not report the turn end: %v\n", err)
	}
}
