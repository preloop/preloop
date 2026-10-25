package cmd

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/config"
)

// Pausing approvals is a local, time-limited escape hatch for when the
// approval service is unreachable. It never edits an agent's own config: the
// hooks stay installed and consult this marker on every call.
const (
	approvalsPauseFile   = "approvals-pause.json"
	approvalsPauseAllKey = "all"
	defaultPauseDuration = time.Hour
	// defaultPauseLabel is defaultPauseDuration as shown in deny messages.
	defaultPauseLabel     = "1h"
	maximumPauseDuration  = 24 * time.Hour
	hookBehaviorPass      = "pass"
	approvalsPauseCommand = "preloop agents pause"
)

// approvalsPauseNow is the clock for pause expiry; tests replace it.
var approvalsPauseNow = time.Now

// permissionHookStderr receives the short note a paused hook prints.
var permissionHookStderr io.Writer = os.Stderr

type approvalsPauseEntry struct {
	Until    time.Time `json:"until"`
	PausedAt time.Time `json:"paused_at"`
}

type approvalsPauseState struct {
	Pauses map[string]approvalsPauseEntry `json:"pauses"`
}

var agentsPauseCmd = &cobra.Command{
	Use:   "pause [agent]",
	Short: "Temporarily pause Preloop approvals on this machine",
	Long: `Pause Preloop approval hooks locally so agents keep working while the
approval service is unavailable. Without an agent, every hooked agent is
paused. The pause expires on its own (default 1h, maximum 24h); run
"preloop agents resume" to end it early. Agent config files are not changed.`,
	Args: cobra.MaximumNArgs(1),
	RunE: runAgentsPause,
}

var agentsResumeCmd = &cobra.Command{
	Use:   "resume [agent]",
	Short: "End a local pause of Preloop approvals",
	Args:  cobra.MaximumNArgs(1),
	RunE:  runAgentsResume,
}

func init() {
	agentsCmd.AddCommand(agentsPauseCmd)
	agentsCmd.AddCommand(agentsResumeCmd)
	agentsPauseCmd.Flags().Duration(
		"for", defaultPauseDuration, "how long to pause approvals (maximum 24h)",
	)
}

func runAgentsPause(cmd *cobra.Command, args []string) error {
	source, err := resolvePauseTarget(firstArg(args))
	if err != nil {
		return err
	}
	value := ""
	if flag := cmd.Flags().Lookup("for"); flag != nil {
		value = flag.Value.String()
	}
	duration, err := parsePauseDuration(value)
	if err != nil {
		return err
	}
	until, err := setApprovalsPause(source, duration)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(
		cmd.OutOrStdout(),
		"Preloop approvals paused for %s until %s. Tool calls are not sent to "+
			"Preloop and are not audited until then.\nRun `preloop agents resume%s` "+
			"to resume now.\n",
		pauseTargetLabel(source),
		formatPauseUntil(until),
		pauseTargetArg(source),
	)
	return err
}

func runAgentsResume(cmd *cobra.Command, args []string) error {
	source, err := resolvePauseTarget(firstArg(args))
	if err != nil {
		return err
	}
	if err := clearApprovalsPause(source); err != nil {
		return err
	}
	msg := fmt.Sprintf("Preloop approvals resumed for %s.\n", pauseTargetLabel(source))
	if source != "" {
		if until, ok := activeApprovalsPause(source); ok {
			msg += fmt.Sprintf(
				"A pause for all agents is still active until %s; run "+
					"`preloop agents resume` to end it.\n",
				formatPauseUntil(until),
			)
		}
	}
	_, err = fmt.Fprint(cmd.OutOrStdout(), msg)
	return err
}

func firstArg(args []string) string {
	if len(args) == 0 {
		return ""
	}
	return args[0]
}

// pauseAwareSources are the hook sources whose CLI permission hook reads the
// pause marker. Runtime-plugin harnesses call permission-check themselves.
var pauseAwareSources = []string{
	permissionSourceClaudeCode,
	permissionSourceCodexCLI,
	permissionSourceCursor,
	permissionSourceCopilotCLI,
	permissionSourceDevinCLI,
}

func isPauseAwareSource(source string) bool {
	for _, candidate := range pauseAwareSources {
		if candidate == source {
			return true
		}
	}
	return false
}

// resolvePauseTarget maps an agent argument onto its permission source. An
// empty argument means every agent and resolves to "".
func resolvePauseTarget(arg string) (string, error) {
	arg = strings.TrimSpace(arg)
	if arg == "" {
		return "", nil
	}
	if source := normalizePermissionSource(arg); source != "" {
		return source, nil
	}
	for _, source := range pauseAwareSources {
		if strings.EqualFold(arg, permissionSourceDisplayName(source)) {
			return source, nil
		}
	}
	return "", fmt.Errorf(
		"unknown agent %q: use Claude Code, Codex CLI, Cursor, Copilot CLI or Devin, "+
			"or omit the agent to pause every agent",
		arg,
	)
}

func parsePauseDuration(value string) (time.Duration, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return defaultPauseDuration, nil
	}
	duration, err := time.ParseDuration(value)
	if err != nil {
		return 0, fmt.Errorf("invalid --for duration %q: %w", value, err)
	}
	if duration <= 0 {
		return 0, fmt.Errorf("--for must be positive")
	}
	if duration > maximumPauseDuration {
		return 0, fmt.Errorf("--for may not exceed %s", maximumPauseDuration)
	}
	return duration, nil
}

func pauseTargetLabel(source string) string {
	if source == "" {
		return "all CLI approval hooks (Claude Code, Codex CLI, Cursor, Copilot CLI, Devin)"
	}
	return permissionSourceDisplayName(source)
}

func pauseTargetArg(source string) string {
	if source == "" {
		return ""
	}
	return fmt.Sprintf(" %q", permissionSourceDisplayName(source))
}

func formatPauseUntil(until time.Time) string {
	return until.Local().Format("15:04 MST (2006-01-02)")
}

func approvalsPausePath() (string, error) {
	dir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, approvalsPauseFile), nil
}

func loadApprovalsPauseState() (approvalsPauseState, error) {
	state := approvalsPauseState{Pauses: map[string]approvalsPauseEntry{}}
	path, err := approvalsPausePath()
	if err != nil {
		return state, err
	}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return state, nil
	}
	if err != nil {
		return state, err
	}
	if err := json.Unmarshal(data, &state); err != nil {
		return approvalsPauseState{Pauses: map[string]approvalsPauseEntry{}}, err
	}
	if state.Pauses == nil {
		state.Pauses = map[string]approvalsPauseEntry{}
	}
	return state, nil
}

func saveApprovalsPauseState(state approvalsPauseState) error {
	path, err := approvalsPausePath()
	if err != nil {
		return err
	}
	now := approvalsPauseNow()
	for key, entry := range state.Pauses {
		if !entry.Until.After(now) {
			delete(state.Pauses, key)
		}
	}
	if len(state.Pauses) == 0 {
		if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("failed to clear approvals pause: %w", err)
		}
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("failed to create config directory: %w", err)
	}
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return fmt.Errorf("failed to write approvals pause: %w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("failed to write approvals pause: %w", err)
	}
	return nil
}

// setApprovalsPause pauses approvals for source ("" for every agent) and
// returns when the pause ends.
func setApprovalsPause(source string, duration time.Duration) (time.Time, error) {
	state, err := loadApprovalsPauseState()
	if err != nil {
		// A corrupt marker is replaced rather than blocking the escape hatch.
		state = approvalsPauseState{Pauses: map[string]approvalsPauseEntry{}}
	}
	now := approvalsPauseNow()
	until := now.Add(duration)
	key := source
	if key == "" {
		key = approvalsPauseAllKey
	}
	state.Pauses[key] = approvalsPauseEntry{Until: until, PausedAt: now}
	return until, saveApprovalsPauseState(state)
}

// clearApprovalsPause ends the pause for source; "" ends every pause.
func clearApprovalsPause(source string) error {
	if source == "" {
		return saveApprovalsPauseState(approvalsPauseState{})
	}
	state, err := loadApprovalsPauseState()
	if err != nil {
		return saveApprovalsPauseState(approvalsPauseState{})
	}
	delete(state.Pauses, source)
	return saveApprovalsPauseState(state)
}

// activeApprovalsPause reports whether approvals are paused for source and
// until when. An unreadable marker is treated as no pause (fail closed).
func activeApprovalsPause(source string) (time.Time, bool) {
	state, err := loadApprovalsPauseState()
	if err != nil {
		return time.Time{}, false
	}
	now := approvalsPauseNow()
	var until time.Time
	for _, key := range []string{approvalsPauseAllKey, source} {
		if key == "" {
			continue
		}
		entry, ok := state.Pauses[key]
		if ok && entry.Until.After(now) && entry.Until.After(until) {
			until = entry.Until
		}
	}
	return until, !until.IsZero()
}

// pausedPermissionDecision returns the no-decision response the hook gives
// while approvals are paused, and prints a short note to stderr.
func pausedPermissionDecision(source string) (hookDecision, bool) {
	if os.Getenv("PRELOOP_FLOW_CREDENTIAL_REQUIRED") == "1" {
		// Flow runs are unattended; a local pause must not ungovern them.
		return hookDecision{}, false
	}
	until, ok := activeApprovalsPause(source)
	if !ok {
		return hookDecision{}, false
	}
	note := fmt.Sprintf(
		"Preloop approvals are paused until %s; run `preloop agents resume` to resume.",
		formatPauseUntil(until),
	)
	_, _ = fmt.Fprintln(permissionHookStderr, note)
	return hookDecision{Behavior: hookBehaviorPass, Reason: note}, true
}

// approvalsPauseStatusLine is the `agents status` line for an active pause.
func approvalsPauseStatusLine(source string) string {
	if !isPauseAwareSource(source) {
		return ""
	}
	until, ok := activeApprovalsPause(source)
	if !ok {
		return ""
	}
	return fmt.Sprintf(
		"Approvals: paused until %s (run `preloop agents resume` to resume now)",
		formatPauseUntil(until),
	)
}
