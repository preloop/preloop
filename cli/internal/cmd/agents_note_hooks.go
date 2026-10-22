package cmd

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/version"
)

// Operator-note delivery for headless runs (#1551).
//
// The PreToolUse hook is the ordinary delivery point, but a `claude -p`
// worker that finishes without another tool call never reaches it: a note
// sent after its last tool call, or before its first, is lost. Two more
// lifecycle hooks close that gap, both fail open:
//
//   - SessionStart (and the first UserPromptSubmit) claims notes for the run
//     as soon as the session is registered. This is where a note sent before
//     the child starts lands, before the first tool call.
//   - Stop asks once more as the turn ends. If something is pending it blocks
//     the stop with the rendered block as the continuation reason, so the
//     model reads it and may act. At most `stopBlockSessionMax` blocks per
//     session, so a stream of notes cannot pin a worker alive forever, and
//     the harness's own `stop_hook_active` guard is respected.
//   - SessionEnd tells the server the conversation is over, so a note whose
//     only session ended is marked undeliverable instead of sitting pending
//     for 24h.
//
// Delivery happens by pulling `/agents/notes/pending`, which marks the note
// delivered exactly once. A hook that cannot render a block must therefore
// not pull it: the cap is checked before the pull, not after.

const (
	agentNotesPendingPath = "/api/v1/agents/notes/pending"
	agentSessionEndPath   = "/api/v1/agents/session-end"

	// notePullChannelHook is the transport these hooks carry notes on.
	notePullChannelHook = "hook"

	// noteEventSessionStart is the lifecycle point recorded on the delivery.
	noteEventSessionStart = "session_start"
	// noteEventStop is the Stop rescue that continues a headless run.
	noteEventStop = "stop"

	// stopBlockSessionMax caps how many Stop blocks one session may cause. A
	// sender must not be able to keep a worker alive in a loop.
	stopBlockSessionMax = 3

	// noteHookHTTPTimeout bounds each hook's one request.
	noteHookHTTPTimeout = 10 * time.Second
)

// notePendingRequest mirrors the backend OperatorNotePendingRequest.
type notePendingRequest struct {
	Channel   string `json:"channel"`
	Event     string `json:"event"`
	SessionID string `json:"session_id,omitempty"`
}

// notePendingResponse is the part of the pull this CLI reads: the rendered
// block, null when nothing was pending.
type notePendingResponse struct {
	Text *string `json:"text"`
}

// hookCredentialForCommand resolves the source flag and the per-agent
// credential for a note hook. A missing credential is not an error: lineage
// and notes are conveniences, and a hook must never block the harness.
func hookCredentialForCommand(cmd *cobra.Command) (permissionHookCredential, bool) {
	source := normalizePermissionSource(mustFlagString(cmd, "source"))
	if source == "" {
		source = permissionSourceClaudeCode
	}
	if source == permissionSourceClaudeCode && isCursorHostInvokingClaudeHook() {
		return permissionHookCredential{}, false
	}
	cred, err := resolvePermissionHookCredential(source)
	if err != nil {
		return permissionHookCredential{}, false
	}
	return cred, true
}

// postNoteHookJSON posts one hook request with the runtime bearer and decodes
// the JSON body into result when one is given.
func postNoteHookJSON(
	cred permissionHookCredential,
	path string,
	payload interface{},
	result interface{},
) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	url := strings.TrimRight(cred.BaseURL, "/") + path
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	version.SetClientIdentityHeaders(req.Header)
	req.Header.Set("Authorization", "Bearer "+cred.Token)
	resp, err := (&http.Client{Timeout: noteHookHTTPTimeout}).Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close() //nolint:errcheck
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 512*1024))
	if err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("API error (status %d)", resp.StatusCode)
	}
	if result == nil {
		return nil
	}
	return json.Unmarshal(raw, result)
}

// claimNotesForHook pulls this session's pending notes at one lifecycle point
// and returns the rendered block, or "" when none arrived or the pull failed.
func claimNotesForHook(cred permissionHookCredential, sessionID, event string) string {
	var decoded notePendingResponse
	err := postNoteHookJSON(cred, agentNotesPendingPath, notePendingRequest{
		Channel:   notePullChannelHook,
		Event:     event,
		SessionID: strings.TrimSpace(sessionID),
	}, &decoded)
	if err != nil || decoded.Text == nil {
		return ""
	}
	return strings.TrimSpace(*decoded.Text)
}

// --- Stop block cap ---------------------------------------------------------

// stopBlockState is the per-session count of Stop blocks already spent. It is
// durable because every hook invocation is a separate process.
type stopBlockState struct {
	Blocks int `json:"blocks"`
}

func stopBlockPath(sessionID string) (string, error) {
	dir, err := permissionHookAgentsDir()
	if err != nil {
		return "", err
	}
	key := sha256.Sum256([]byte(strings.TrimSpace(sessionID)))
	name := hex.EncodeToString(key[:])[:16] + ".json"
	return filepath.Join(dir, "stop-blocks", name), nil
}

func readStopBlockCount(sessionID string) int {
	path, err := stopBlockPath(sessionID)
	if err != nil {
		return 0
	}
	data, err := os.ReadFile(path) //nolint:gosec
	if err != nil {
		return 0
	}
	var state stopBlockState
	if json.Unmarshal(data, &state) != nil {
		return 0
	}
	return state.Blocks
}

// recordStopBlock spends one block and returns the new count. Best effort: a
// write failure only means the cap is not enforced across processes.
func recordStopBlock(sessionID string) int {
	count := readStopBlockCount(sessionID) + 1
	path, err := stopBlockPath(sessionID)
	if err != nil {
		return count
	}
	data, err := json.Marshal(stopBlockState{Blocks: count})
	if err != nil {
		return count
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return count
	}
	_ = os.WriteFile(path, data, 0o600)
	return count
}

// resetStopBlocks drops a session's cap state; called when the session ends
// (or starts again under the same harness id) so it cannot leak into the next.
func resetStopBlocks(sessionID string) {
	if strings.TrimSpace(sessionID) == "" {
		return
	}
	path, err := stopBlockPath(sessionID)
	if err != nil {
		return
	}
	_ = os.Remove(path)
}

// --- Stop hook --------------------------------------------------------------

type stopHookEvent struct {
	SessionID      string `json:"session_id"`
	HookEventName  string `json:"hook_event_name"`
	StopHookActive bool   `json:"stop_hook_active"`
	LoopCount      int    `json:"loop_count"`
	Status         string `json:"status"`
}

var agentsStopHookCmd = &cobra.Command{
	Use:    "stop",
	Short:  "Deliver a pending operator note before the agent stops (Stop hook)",
	Hidden: true,
	Args:   cobra.NoArgs,
	RunE:   runAgentsStopHook,
}

func init() {
	agentsPermissionHookCmd.AddCommand(agentsStopHookCmd)
	agentsStopHookCmd.Flags().String("source", permissionSourceClaudeCode, "agent source, e.g. claude_code")
}

func runAgentsStopHook(cmd *cobra.Command, _ []string) error {
	errOut := cmd.ErrOrStderr()
	raw, err := io.ReadAll(io.LimitReader(cmd.InOrStdin(), 1<<20))
	if err != nil {
		return nil
	}
	var event stopHookEvent
	if json.Unmarshal(raw, &event) != nil {
		return nil
	}
	sessionID := strings.TrimSpace(hookEventSessionID(raw))
	if sessionID == "" {
		return nil
	}
	// Unless a note continues the turn, this Stop ends it: tell the console.
	blocked := false
	defer func() {
		if !blocked {
			reportTurnEnd(cmd, raw, event.HookEventName, sessionID)
		}
	}()
	// Respect the harness's own loop guard: `stop_hook_active` means this
	// Stop already follows a block, so blocking again would spin.
	if event.StopHookActive || event.LoopCount >= stopBlockSessionMax {
		return nil
	}
	// Check the cap before the pull: the pull marks a note delivered, so a
	// block we then refuse would lose it. Past the cap the note stays pending
	// and SessionEnd marks it undeliverable.
	if readStopBlockCount(sessionID) >= stopBlockSessionMax {
		return nil
	}
	cred, ok := hookCredentialForCommand(cmd)
	if !ok {
		return nil
	}
	if cred.Source != permissionSourceClaudeCode && cred.Source != permissionSourceCodexCLI && cred.Source != permissionSourceCursor {
		return nil
	}
	if cred.Source == permissionSourceCursor && event.Status != "" && event.Status != "completed" {
		return nil
	}
	text := claimNotesForHook(cred, sessionID, noteEventStop)
	if text == "" {
		return nil
	}
	recordStopBlock(sessionID)
	blocked = true
	payload := map[string]interface{}{"decision": "block", "reason": text}
	if cred.Source == permissionSourceCursor {
		payload = map[string]interface{}{"followup_message": text}
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return nil
	}
	if _, err := fmt.Fprintln(cmd.OutOrStdout(), string(data)); err != nil {
		fmt.Fprintf(errOut, "preloop stop hook: %v\n", err)
	}
	return nil
}

// --- SessionEnd hook --------------------------------------------------------

type sessionEndHookEvent struct {
	SessionID     string `json:"session_id"`
	HookEventName string `json:"hook_event_name"`
	Reason        string `json:"reason"`
}

var agentsSessionEndHookCmd = &cobra.Command{
	Use:    "session-end",
	Short:  "Tell Preloop the harness session ended (SessionEnd hook)",
	Hidden: true,
	Args:   cobra.NoArgs,
	RunE:   runAgentsSessionEndHook,
}

func init() {
	agentsPermissionHookCmd.AddCommand(agentsSessionEndHookCmd)
	agentsSessionEndHookCmd.Flags().String("source", permissionSourceClaudeCode, "agent source, e.g. claude_code")
}

func runAgentsSessionEndHook(cmd *cobra.Command, _ []string) error {
	raw, err := io.ReadAll(io.LimitReader(cmd.InOrStdin(), 1<<20))
	if err != nil {
		return nil
	}
	var event sessionEndHookEvent
	if json.Unmarshal(raw, &event) != nil {
		return nil
	}
	sessionID := strings.TrimSpace(hookEventSessionID(raw))
	if sessionID == "" {
		return nil
	}
	// The conversation is over either way: its Stop-cap state is stale.
	resetStopBlocks(sessionID)
	cred, ok := hookCredentialForCommand(cmd)
	if !ok {
		return nil
	}
	payload := map[string]string{"external_session_id": sessionID}
	if reason := strings.TrimSpace(event.Reason); reason != "" {
		payload["reason"] = reason
	}
	// Best effort: a note left pending only outlives its session by the TTL.
	if err := postNoteHookJSON(cred, agentSessionEndPath, payload, nil); err != nil {
		fmt.Fprintf(cmd.ErrOrStderr(), "preloop session-end hook: %v\n", err)
	}
	return nil
}
