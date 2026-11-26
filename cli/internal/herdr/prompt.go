package herdr

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os/exec"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

const MaxPromptBytes = 16 << 10
const maxPromptOutputBytes = 64 << 10
const promptTimeout = 120 * time.Second

// PromptTarget binds a command to the exact native session and local pane.
type PromptTarget struct {
	PaneID           string
	HarnessKind      string
	HarnessSessionID string
}

// PromptResult distinguishes rejection from confirmed submission and ambiguous
// delivery. Unknown delivery must never be automatically retried.
type PromptResult struct {
	Code     string `json:"code"`
	Delivery string `json:"delivery"`
	Reason   string `json:"reason"`
	State    string `json:"state,omitempty"`
}

func promptResult(code, delivery, reason string) PromptResult {
	return PromptResult{Code: code, Delivery: delivery, Reason: reason}
}

func promptText(text string) (string, error) {
	if len(text) > MaxPromptBytes || !utf8.ValidString(text) {
		return "", errors.New("prompt must be valid UTF-8 and at most 16 KiB")
	}
	text = strings.Map(func(r rune) rune {
		if unicode.IsControl(r) {
			return -1
		}
		return r
	}, text)
	if strings.TrimSpace(text) == "" {
		return "", errors.New("prompt is empty after removing control characters")
	}
	return text, nil
}

// Prompt rechecks local consent and identity immediately before invoking the
// documented agent prompt command. It never reads terminal content or answers
// dialogs, and intentionally does not implement retries or remote routing.
func Prompt(ctx context.Context, configDir string, target PromptTarget, text string) PromptResult {
	settings, err := LoadSettings(configDir)
	if err != nil || !settings.Prompt {
		return promptResult("prompt_disabled", "not_sent", "Enable herdr prompting locally on this device before sending a prompt.")
	}
	text, err = promptText(text)
	if err != nil {
		return promptResult("invalid_prompt", "not_sent", err.Error())
	}
	harness := Harness(target.HarnessKind)
	if !validID(target.PaneID, 256) || !validID(target.HarnessSessionID, 512) || (harness != "claude_code" && harness != "codex_cli") {
		return promptResult("invalid_target", "not_sent", "A supported harness, exact native session id, and local pane id are required.")
	}
	ctx, cancel := context.WithTimeout(ctx, promptTimeout)
	defer cancel()
	report, err := Snapshot(ctx)
	if errors.Is(err, ErrUnsupported) {
		return promptResult("herdr_protocol_unsupported", "not_sent", "This herdr protocol is unsupported; update the adapter before prompting.")
	}
	if err != nil {
		return promptResult("snapshot_unavailable", "not_sent", "Could not verify the local pane; check herdr and try again.")
	}
	var found *Agent
	matches := 0
	for i := range report.Agents {
		a := &report.Agents[i]
		if a.HarnessKind == harness && a.HarnessSessionID == target.HarnessSessionID {
			matches++
		}
		if a.PaneID == target.PaneID {
			found = a
		}
	}
	if found == nil {
		return promptResult("pane_gone", "not_sent", "The target pane no longer contains an agent. Select a current session.")
	}
	if found.HarnessKind != harness || found.HarnessSessionID != target.HarnessSessionID || matches != 1 {
		return promptResult("session_changed", "not_sent", "The pane identity changed or is ambiguous. Select the current session before prompting.")
	}
	switch found.State {
	case "blocked":
		return promptResult("agent_blocked", "not_sent", "The agent is waiting at a terminal dialog. Handle it locally; this prompt cannot answer it.")
	case "working":
		return promptResult("note_required", "not_sent", "The agent is working; route this message as a note instead.")
	case "idle", "done":
	default:
		return promptResult("agent_not_ready", "not_sent", "The agent state is unknown. Check the pane locally before prompting.")
	}
	if !found.InteractiveReady || found.LaunchPending {
		return promptResult("agent_not_ready", "not_sent", "The agent is not ready for interactive input. Check the foreground process locally.")
	}
	// Re-read consent after the snapshot in case it changed during verification.
	settings, err = LoadSettings(configDir)
	if err != nil || !settings.Prompt {
		return promptResult("prompt_disabled", "not_sent", "Prompting was disabled locally before submission.")
	}
	if ctx.Err() != nil {
		return promptResult("timeout", "not_sent", "The command expired before submission.")
	}
	remaining := promptTimeout
	if deadline, ok := ctx.Deadline(); ok {
		remaining = time.Until(deadline)
	}
	cmd := exec.CommandContext(ctx, "herdr", "agent", "prompt", "--wait", "--timeout", strconv.FormatInt(max(1, remaining.Milliseconds()), 10), "--", target.PaneID, text)
	cmd.WaitDelay = 250 * time.Millisecond
	stdout, stderr := &promptOutput{}, &promptOutput{}
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	err = cmd.Run()
	if ctx.Err() != nil {
		return promptResult("timeout", "unknown", "Prompt delivery is uncertain after the timeout. Check the session before sending anything again.")
	}
	if stdout.overflow || stderr.overflow {
		return promptResult("invalid_response", "unknown", "Herdr returned too much data. Check the session before retrying.")
	}
	return decodePromptResult(stdout.Bytes(), stderr.Bytes(), err)
}

type promptOutput struct {
	buffer   bytes.Buffer
	overflow bool
}

func (b *promptOutput) Bytes() []byte { return b.buffer.Bytes() }
func (b *promptOutput) Write(p []byte) (int, error) {
	if b.buffer.Len()+len(p) > maxPromptOutputBytes {
		b.overflow = true
		return 0, errors.New("herdr response exceeds size limit")
	}
	return b.buffer.Write(p)
}

type promptResponse struct {
	Error *struct {
		Code string `json:"code"`
	} `json:"error"`
	Result struct {
		Type  string `json:"type"`
		Agent struct {
			State string `json:"agent_status"`
		} `json:"agent"`
	} `json:"result"`
}

func decodePromptResult(stdout, stderr []byte, runErr error) PromptResult {
	var response promptResponse
	if json.Unmarshal(stdout, &response) != nil || response.Error == nil && response.Result.Type == "" {
		_ = json.Unmarshal(stderr, &response)
	}
	if response.Error != nil {
		switch response.Error.Code {
		case "agent_blocked":
			return promptResult("agent_blocked", "not_sent", "Herdr refused a terminal dialog. Handle it locally; do not send this prompt as an answer.")
		case "agent_prompt_stalled":
			return promptResult("agent_prompt_stalled", "unknown", "No agent activity was observed after submission. Check the session before retrying.")
		case "timeout":
			return promptResult("timeout", "unknown", "The wait timed out; delivery is uncertain. Check the session before retrying.")
		case "pane_not_found", "agent_not_found", "pane_gone":
			return promptResult("pane_gone", "unknown", "The pane or agent disappeared during submission or waiting. Delivery is uncertain; check the session before retrying.")
		}
	}
	if runErr != nil || response.Result.Type != "agent_prompted" {
		return promptResult("delivery_unknown", "unknown", "Herdr did not confirm submission. Check the session before retrying.")
	}
	state := response.Result.Agent.State
	if state != "idle" && state != "done" && state != "blocked" {
		return promptResult("delivery_unknown", "unknown", "Herdr returned an unexpected completion state. Check the session before retrying.")
	}
	result := promptResult("delivered", "sent", "Prompt submitted; replies remain in the session's governed record.")
	result.State = state
	if state == "blocked" {
		result.Code = "agent_blocked"
		result.Reason = "Prompt submitted; the agent is now waiting at a terminal dialog. Handle it locally."
	}
	return result
}
