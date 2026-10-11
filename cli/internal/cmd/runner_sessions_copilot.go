package cmd

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"sync"
	"time"
)

// sessionHarnessAdapter hosts one harness for remote sessions. Start checks
// the host can run the harness and fixes the harness session identity; Turn
// runs one operator turn to completion (or until ctx is cancelled, which
// kills the harness process tree); Stop releases anything Start held.
type sessionHarnessAdapter interface {
	Harness() string
	Mode() string
	Start(s *runnerRemoteSession) error
	Turn(ctx context.Context, turn runnerSessionTurnSpec, emit func(kind string, payload map[string]any)) runnerSessionTurnResult
	Stop(s *runnerRemoteSession) error
}

type runnerSessionTurnSpec struct {
	HarnessSessionID string
	// Resume is true once the harness has created its session on disk.
	Resume bool
	Text   string
	Model  string
	Dir    string
}

type runnerSessionTurnResult struct {
	Status    string
	ErrorCode string
	Error     string
	Usage     map[string]any
	// SessionCreated reports that the harness now has this session on disk,
	// so the next turn resumes it.
	SessionCreated bool
}

// copilotSessionAdapter runs GitHub Copilot CLI in per-turn resume mode
// (feasibility verified against Copilot CLI 1.0.95 help on 2026-10-11):
//
//	turn 1: copilot --prompt=<text> -s --no-ask-user --output-format=json
//	        --no-remote [--model=<id>] <tool rules> --session-id=<uuid>
//	turn N: the same with --resume=<uuid>
//
// The uuid is minted by the runner, never by the server, so a server cannot
// point a session at an unrelated Copilot conversation on this host. The
// binary is the user's own installed copilot, the environment is the
// host-exec allowlist with BYOK and allow-all variables stripped, and the
// Preloop approval hook must be installed: every tool call goes through
// Preloop approvals.
type copilotSessionAdapter struct {
	loadProfile           func() (hostExecProfile, error)
	ensureUsageHooks      func() error
	approvalHookInstalled func() (bool, error)
	resolveCommand        func(executable string) (string, []string, error)
	environ               func() []string
	newID                 func() (string, error)
}

func newCopilotSessionAdapter() *copilotSessionAdapter {
	return &copilotSessionAdapter{
		loadProfile:           copilotSessionProfile,
		ensureUsageHooks:      ensureCopilotHostExecUsageHooks,
		approvalHookInstalled: copilotApprovalHookInstalled,
		resolveCommand:        resolveHostExecCommand,
		environ:               os.Environ,
		newID:                 newSessionUUID,
	}
}

func (a *copilotSessionAdapter) Harness() string { return hostExecHarnessCopilot }

// Mode is the contract A session_mode for this adapter.
func (a *copilotSessionAdapter) Mode() string { return "resume" }

// copilotSessionProfile is the host-exec profile named "copilot" from
// ~/.preloop/runner-host-profiles.json when present (hand-written, or the
// one the harness inventory generates), else a plain profile that runs
// `copilot` from PATH with no tool grants. Tool rules come only from the
// host profile, never from the server.
func copilotSessionProfile() (hostExecProfile, error) {
	profiles, err := loadHostExecProfiles()
	if err != nil {
		return hostExecProfile{}, err
	}
	for _, profile := range profiles {
		if strings.EqualFold(profile.Name, "copilot") {
			if hostExecProfileHarness(profile) != hostExecHarnessCopilot {
				return hostExecProfile{}, fmt.Errorf("host profile %q does not run the copilot binary", profile.Name)
			}
			return profile, nil
		}
	}
	return hostExecProfile{Name: "copilot", Executable: "copilot"}, nil
}

func (a *copilotSessionAdapter) profile() (hostExecProfile, error) {
	profile, err := a.loadProfile()
	if err != nil {
		return hostExecProfile{}, err
	}
	if err := validateCopilotHostExecProfile(profile); err != nil {
		return hostExecProfile{}, fmt.Errorf("host profile %q: %w", profile.Name, err)
	}
	return profile, nil
}

// Start refuses unless the copilot binary resolves and the Preloop approval
// hook is installed for this user (#1485 T7: always, not only with
// allow_all_tools), then mints the Copilot session uuid.
func (a *copilotSessionAdapter) Start(s *runnerRemoteSession) error {
	profile, err := a.profile()
	if err != nil {
		return err
	}
	if _, _, err := a.resolveCommand(profile.Executable); err != nil {
		return fmt.Errorf("copilot_not_found: %w", err)
	}
	if err := a.ensureUsageHooks(); err != nil {
		return fmt.Errorf("copilot_hooks_unavailable: install Preloop Copilot hooks: %w", err)
	}
	installed, err := a.approvalHookInstalled()
	if err != nil {
		return fmt.Errorf("copilot_hooks_unavailable: read Preloop Copilot hooks: %w", err)
	}
	if !installed {
		return fmt.Errorf("copilot_approval_hook_missing: remote sessions require the Preloop preToolUse approval hook; run `preloop agents onboard \"Copilot CLI\" --approvals` as the runner user")
	}
	id, err := a.newID()
	if err != nil {
		return err
	}
	s.HarnessSessionID = id
	return nil
}

func (a *copilotSessionAdapter) Stop(*runnerRemoteSession) error { return nil }

// copilotSessionArgs renders one turn. The prompt, tool rules and output
// flags come from buildCopilotHostExecArgs, the same builder flows use.
func copilotSessionArgs(profile hostExecProfile, turn runnerSessionTurnSpec) ([]string, error) {
	if !uuidRe.MatchString(turn.HarnessSessionID) {
		return nil, fmt.Errorf("invalid harness session id")
	}
	args, err := buildCopilotHostExecArgs(profile, map[string]any{"prompt": turn.Text})
	if err != nil {
		return nil, err
	}
	// GitHub's own remote control stays off so the session is steered only
	// through Preloop.
	args = append(args, "--no-remote")
	if model := strings.TrimSpace(turn.Model); model != "" && model != "auto" {
		alias := profile.ModelMap[model]
		if alias == "" {
			if !hostExecModelRe.MatchString(model) {
				return nil, fmt.Errorf("model_not_available: invalid model id")
			}
			alias = model
		}
		args = append(args, "--model="+alias)
	}
	if turn.Resume {
		args = append(args, "--resume="+turn.HarnessSessionID)
	} else {
		args = append(args, "--session-id="+turn.HarnessSessionID)
	}
	return args, nil
}

// copilotSessionEnv is the host-exec allowlist followed by the Copilot strip
// stage: a seat session cannot be moved to a BYOK provider or granted every
// tool by the runner's environment.
func copilotSessionEnv(profile hostExecProfile, environ []string, bin string) []string {
	env := hostExecChildEnv(runtime.GOOS, hostExecHarnessCopilot, profile, environ)
	env = copilotHostExecEnv(env)
	return hostExecPrependPath(runtime.GOOS, env, hostExecBinaryDirs(bin, profile.Executable)...)
}

func (a *copilotSessionAdapter) Turn(ctx context.Context, turn runnerSessionTurnSpec, emit func(string, map[string]any)) runnerSessionTurnResult {
	fail := func(code string, err error) runnerSessionTurnResult {
		return runnerSessionTurnResult{Status: "error", ErrorCode: code, Error: err.Error()}
	}
	if turn.Dir == "" {
		return fail(rejectWorkspaceNotAllowed, fmt.Errorf("no workspace"))
	}
	profile, err := a.profile()
	if err != nil {
		return fail("harness_unavailable", err)
	}
	installed, err := a.approvalHookInstalled()
	if err != nil || !installed {
		return fail("copilot_approval_hook_missing", fmt.Errorf("the Preloop approval hook is no longer installed"))
	}
	args, err := copilotSessionArgs(profile, turn)
	if err != nil {
		return fail("invalid_turn", err)
	}
	bin, prefix, err := a.resolveCommand(profile.Executable)
	if err != nil {
		return fail("harness_unavailable", err)
	}
	args = append(append([]string{}, prefix...), args...)
	if err := hostExecCommandLineError(runtime.GOOS, bin, args); err != nil {
		return fail("invalid_turn", err)
	}
	cmd := exec.Command(bin, args...)
	cmd.Dir = turn.Dir
	cmd.Env = copilotSessionEnv(profile, a.environ(), bin)
	cmd.SysProcAttr = hostExecSysProcAttr()
	cmd.WaitDelay = 250 * time.Millisecond
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return fail("harness_unavailable", err)
	}
	stderr := &boundedTail{max: copilotMaxErrorBytes * 4}
	cmd.Stderr = stderr
	if err := cmd.Start(); err != nil {
		return fail("harness_unavailable", err)
	}
	finished := make(chan struct{})
	var killed bool
	var killMu sync.Mutex
	go func() {
		select {
		case <-ctx.Done():
			killMu.Lock()
			killed = true
			killMu.Unlock()
			killRunnerJobProcess(cmd)
		case <-finished:
		}
	}()
	capture := copilotCapture{}
	reader := bufio.NewReaderSize(stdout, 64*1024)
	for {
		line, readErr := readBoundedLine(reader, 4*1024*1024)
		if line != "" {
			if kind, payload, ok := copilotSessionEvent(&capture, line); ok {
				emit(kind, payload)
			}
		}
		if readErr != nil {
			break
		}
	}
	waitErr := cmd.Wait()
	close(finished)
	killMu.Lock()
	wasKilled := killed
	killMu.Unlock()

	result := runnerSessionTurnResult{Status: "ok", SessionCreated: capture.SessionID != "" || turn.Resume}
	if capture.PremiumRequests != nil {
		result.Usage = map[string]any{"premium_requests": *capture.PremiumRequests}
		if capture.Model != "" {
			result.Usage["model"] = capture.Model
		}
	}
	if tail := strings.TrimSpace(stderr.String()); tail != "" && (waitErr != nil || wasKilled) {
		emit("stderr", map[string]any{"text": truncateUTF8(tail, copilotMaxErrorBytes*4)})
	}
	switch {
	case wasKilled:
		result.Status, result.ErrorCode, result.Error = "error", "killed", "turn stopped"
	default:
		if _, err := copilotRunnerResult(capture); err != nil || *capture.ExitCode != 0 || waitErr != nil {
			result.Status = "error"
			result.ErrorCode, result.Error = copilotSessionFailure(capture, waitErr)
		}
	}
	return result
}

// copilotSessionFailure names a failed turn from Copilot's plain-text
// startup errors, like copilotHostExecFailure does for flows.
func copilotSessionFailure(capture copilotCapture, waitErr error) (string, string) {
	line := capture.ErrorLine
	switch {
	case strings.Contains(line, "No authentication information found"):
		return rejectHarnessSignedOut, "copilot_not_logged_in: Copilot CLI on this runner has no login; run `copilot login` as the runner user"
	case strings.Contains(line, "--model") && strings.Contains(line, "not available"):
		return "model_not_available", line
	case line != "":
		return "harness_failed", line
	case waitErr != nil:
		return "harness_failed", waitErr.Error()
	default:
		return "harness_failed", "copilot exited without a valid structured result"
	}
}

// copilotSessionEvent maps one Copilot JSONL line to a session_event. Token
// deltas, echoes of the operator prompt and bookkeeping events are dropped;
// the console sees agent messages, tool calls and results, usage, and
// plain-text errors.
func copilotSessionEvent(capture *copilotCapture, line string) (string, map[string]any, bool) {
	if !applyCopilotLine(capture, line) {
		return "", nil, false
	}
	trimmed := strings.TrimSpace(line)
	if trimmed == "" {
		return "", nil, false
	}
	if trimmed[0] != '{' {
		return "stderr", map[string]any{"text": truncateUTF8(trimmed, copilotMaxErrorBytes*4)}, true
	}
	var event struct {
		Type  string         `json:"type"`
		Data  map[string]any `json:"data"`
		Usage map[string]any `json:"usage"`
	}
	if json.Unmarshal([]byte(trimmed), &event) != nil {
		return "", nil, false
	}
	switch {
	case event.Type == "assistant.message":
		payload := map[string]any{"event": event.Type}
		if text, ok := event.Data["content"].(string); ok {
			payload["text"] = text
		}
		if model, ok := event.Data["model"].(string); ok && hostExecModelRe.MatchString(model) {
			payload["model"] = model
		}
		return "agent_message", payload, true
	case strings.HasPrefix(event.Type, "tool."):
		kind := "tool_result"
		if strings.HasSuffix(event.Type, "start") || strings.HasSuffix(event.Type, "request") || strings.HasSuffix(event.Type, "requested") {
			kind = "tool_call"
		}
		return kind, map[string]any{"event": event.Type, "data": event.Data}, true
	case event.Type == "result":
		if event.Usage == nil {
			return "", nil, false
		}
		return "usage", map[string]any{"event": event.Type, "usage": event.Usage}, true
	default:
		return "", nil, false
	}
}

// readBoundedLine reads one line, discarding anything past max bytes of it.
func readBoundedLine(reader *bufio.Reader, max int) (string, error) {
	var b strings.Builder
	for {
		chunk, isPrefix, err := reader.ReadLine()
		if b.Len()+len(chunk) <= max {
			b.Write(chunk)
		}
		if err != nil {
			return b.String(), err
		}
		if !isPrefix {
			return b.String(), nil
		}
	}
}

// boundedTail keeps the last max bytes written to it.
type boundedTail struct {
	mu  sync.Mutex
	max int
	buf []byte
}

func (t *boundedTail) Write(p []byte) (int, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.buf = append(t.buf, p...)
	if len(t.buf) > t.max {
		t.buf = t.buf[len(t.buf)-t.max:]
	}
	return len(p), nil
}

func (t *boundedTail) String() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return string(t.buf)
}

var _ io.Writer = (*boundedTail)(nil)
