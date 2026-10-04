package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/version"
)

// Session lineage and spawn-time id handoff for hook-governed agents (#1045).
//
// A conductor that starts workers with `claude -p ... &` knows a pid and
// nothing else. This hook runs at SessionStart and on the first
// UserPromptSubmit and:
//
//   - registers the run with Preloop, naming the session that spawned it
//     (PRELOOP_PARENT_SESSION_ID, inherited through the shell), so the
//     default send_note scope reaches it;
//   - exports PRELOOP_PARENT_SESSION_ID=<this run's id> through
//     CLAUDE_ENV_FILE, so whatever this run spawns records it as parent;
//   - writes ~/.preloop/sessions/<external_session_id>.json with the
//     runtime session id, for a spawner that chose the id (claude
//     --session-id) or can read it from the transcript name;
//   - prints `preloop runtime_session_id=<id>` once to stderr in
//     non-interactive mode;
//   - sends the first prompt once, so the server can set a redacted title.
//
// Everything here is best effort. A failure is logged to stderr and the hook
// exits 0: lineage is a convenience, and it must never block a session.

// agentSessionStartPath is the backend endpoint the hook registers with.
const agentSessionStartPath = "/api/v1/agents/session-start"

// parentSessionEnvVar carries the runtime session id of the spawning run.
const parentSessionEnvVar = "PRELOOP_PARENT_SESSION_ID"

// sessionHookHTTPTimeout bounds the one request the hook makes.
const sessionHookHTTPTimeout = 5 * time.Second

// sessionHookTimeoutSeconds is the host-side timeout written into settings.
const sessionHookTimeoutSeconds = 10

// sessionHookEvents are the Claude Code events wired to the session hook.
var sessionHookEvents = []string{"SessionStart", "UserPromptSubmit"}

var sessionFileIDPattern = regexp.MustCompile(`^[A-Za-z0-9_.:\-]{1,200}$`)

var agentsSessionHookCmd = &cobra.Command{
	Use:    "session",
	Short:  "Record session lineage and the runtime session id (SessionStart hook)",
	Hidden: true,
	Args:   cobra.NoArgs,
	RunE:   runAgentsSessionHook,
}

func init() {
	agentsPermissionHookCmd.AddCommand(agentsSessionHookCmd)
	agentsSessionHookCmd.Flags().String("source", permissionSourceClaudeCode, "agent source, e.g. claude_code")
}

type sessionHookEvent struct {
	HookEventName string `json:"hook_event_name"`
	SessionID     string `json:"session_id"`
	Cwd           string `json:"cwd"`
	Prompt        string `json:"prompt"`
}

type sessionStartRequest struct {
	SessionID       string `json:"session_id"`
	Source          string `json:"source,omitempty"`
	Cwd             string `json:"cwd,omitempty"`
	ParentSessionID string `json:"parent_session_id,omitempty"`
	FirstPrompt     string `json:"first_prompt,omitempty"`
}

type sessionStartResponse struct {
	RuntimeSessionID string `json:"runtime_session_id"`
	ParentSessionID  string `json:"parent_session_id"`
	StartedAt        string `json:"started_at"`
}

// sessionHandoffFile is ~/.preloop/sessions/<external_session_id>.json.
type sessionHandoffFile struct {
	RuntimeSessionID  string `json:"runtime_session_id"`
	ExternalSessionID string `json:"external_session_id"`
	ParentSessionID   string `json:"parent_session_id,omitempty"`
	StartedAt         string `json:"started_at,omitempty"`
	AgentKind         string `json:"agent_kind"`
	Cwd               string `json:"cwd,omitempty"`
	TitleSent         bool   `json:"title_sent,omitempty"`
}

func sessionHandoffDir() (string, error) {
	base, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(base, "sessions"), nil
}

func sessionHandoffPath(externalID string) (string, error) {
	if !sessionFileIDPattern.MatchString(externalID) || strings.Contains(externalID, "..") {
		return "", fmt.Errorf("unusable session id")
	}
	dir, err := sessionHandoffDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, externalID+".json"), nil
}

func readSessionHandoff(externalID string) (sessionHandoffFile, bool) {
	path, err := sessionHandoffPath(externalID)
	if err != nil {
		return sessionHandoffFile{}, false
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return sessionHandoffFile{}, false
	}
	var file sessionHandoffFile
	if json.Unmarshal(raw, &file) != nil {
		return sessionHandoffFile{}, false
	}
	return file, true
}

func writeSessionHandoff(file sessionHandoffFile) error {
	path, err := sessionHandoffPath(file.ExternalSessionID)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(file, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(raw, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// sessionHookNonInteractive reports whether the harness runs headless
// (`claude -p` sets CLAUDE_CODE_ENTRYPOINT=sdk-cli; Codex exec has no TTY).
func sessionHookNonInteractive() bool {
	switch strings.TrimSpace(os.Getenv("CLAUDE_CODE_ENTRYPOINT")) {
	case "sdk-cli", "sdk-ts", "sdk-py":
		return true
	}
	return false
}

// appendClaudeEnvExport exports the run's own id to its child processes.
// Claude Code sources CLAUDE_ENV_FILE before every Bash command it runs.
func appendClaudeEnvExport(runtimeSessionID string) error {
	path := strings.TrimSpace(os.Getenv("CLAUDE_ENV_FILE"))
	if path == "" {
		return nil
	}
	if !sessionFileIDPattern.MatchString(runtimeSessionID) {
		return fmt.Errorf("unusable runtime session id")
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer f.Close() //nolint:errcheck
	_, err = fmt.Fprintf(f, "export %s=%s\n", parentSessionEnvVar, runtimeSessionID)
	return err
}

func postSessionStart(cred permissionHookCredential, req sessionStartRequest) (sessionStartResponse, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return sessionStartResponse{}, err
	}
	url := strings.TrimRight(cred.BaseURL, "/") + agentSessionStartPath
	httpReq, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return sessionStartResponse{}, err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "application/json")
	version.SetClientIdentityHeaders(httpReq.Header)
	httpReq.Header.Set("Authorization", "Bearer "+cred.Token)
	resp, err := (&http.Client{Timeout: sessionHookHTTPTimeout}).Do(httpReq)
	if err != nil {
		return sessionStartResponse{}, err
	}
	defer resp.Body.Close() //nolint:errcheck
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if err != nil {
		return sessionStartResponse{}, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return sessionStartResponse{}, fmt.Errorf("API error (status %d)", resp.StatusCode)
	}
	var decoded sessionStartResponse
	if err := json.Unmarshal(raw, &decoded); err != nil {
		return sessionStartResponse{}, err
	}
	if decoded.RuntimeSessionID == "" {
		return sessionStartResponse{}, fmt.Errorf("response carried no runtime_session_id")
	}
	return decoded, nil
}

func runAgentsSessionHook(cmd *cobra.Command, _ []string) error {
	errOut := cmd.ErrOrStderr()
	source := normalizePermissionSource(mustFlagString(cmd, "source"))
	if source == "" {
		source = permissionSourceClaudeCode
	}
	raw, err := io.ReadAll(io.LimitReader(cmd.InOrStdin(), 1<<20))
	if err != nil {
		fmt.Fprintf(errOut, "preloop session hook: %v\n", err)
		return nil
	}
	var event sessionHookEvent
	if json.Unmarshal(raw, &event) != nil || strings.TrimSpace(event.SessionID) == "" {
		return nil
	}
	externalID := strings.TrimSpace(event.SessionID)
	if !sessionFileIDPattern.MatchString(externalID) {
		return nil
	}
	isPrompt := strings.EqualFold(event.HookEventName, "UserPromptSubmit")
	existing, haveFile := readSessionHandoff(externalID)
	if isPrompt && (strings.TrimSpace(event.Prompt) == "" || (haveFile && existing.TitleSent)) {
		// The common case: every prompt after the first costs one stat.
		return nil
	}

	cred, err := resolvePermissionHookCredential(source)
	if err != nil {
		return nil
	}
	req := sessionStartRequest{
		SessionID:       externalID,
		Source:          source,
		Cwd:             strings.TrimSpace(event.Cwd),
		ParentSessionID: strings.TrimSpace(os.Getenv(parentSessionEnvVar)),
	}
	if isPrompt {
		req.FirstPrompt = event.Prompt
	}
	resp, err := postSessionStart(cred, req)
	if err != nil {
		fmt.Fprintf(errOut, "preloop session hook: could not register the session: %v\n", err)
		return nil
	}

	file := sessionHandoffFile{
		RuntimeSessionID:  resp.RuntimeSessionID,
		ExternalSessionID: externalID,
		ParentSessionID:   resp.ParentSessionID,
		StartedAt:         resp.StartedAt,
		AgentKind:         source,
		Cwd:               req.Cwd,
		TitleSent:         isPrompt || (haveFile && existing.TitleSent),
	}
	if err := writeSessionHandoff(file); err != nil {
		fmt.Fprintf(errOut, "preloop session hook: could not write the session file: %v\n", err)
	}
	if !isPrompt {
		if err := appendClaudeEnvExport(resp.RuntimeSessionID); err != nil {
			fmt.Fprintf(errOut, "preloop session hook: could not export %s: %v\n", parentSessionEnvVar, err)
		}
		if sessionHookNonInteractive() && !(haveFile && existing.RuntimeSessionID == resp.RuntimeSessionID) {
			fmt.Fprintf(errOut, "preloop runtime_session_id=%s\n", resp.RuntimeSessionID)
		}
	}
	return nil
}
