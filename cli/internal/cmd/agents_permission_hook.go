package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/version"
)

// Permission sources understood by the shared hook subcommand. These map onto
// the `source` field of the frozen permission-check API contract.
const (
	permissionSourceClaudeCode = "claude_code"
	permissionSourceCodexCLI   = "codex_cli"
	permissionSourceCursor     = "cursor"
	// permissionSourceOpenCode is sent by the OpenCode runtime plugin's
	// tool.execute.before gate, not by this CLI's permission-hook command:
	// onboarding registers the plugin instead of writing a command hook.
	permissionSourceOpenCode = "opencode"
)

// defaultApprovalHookTimeoutSeconds is the legacy OpenCode onboarding fallback.
// Command hooks use the maximum supported workflow budget below.
const defaultApprovalHookTimeoutSeconds = 1800

// A native rule can select a workflow that differs from the account default.
// Keep the client alive for the supported maximum; the server owns expiry.
const maxApprovalWorkflowTimeoutSeconds = 86400

// The host must give the HTTP client time to receive and render its result.
const approvalHookProcessHeadroomSeconds = 30

// permissionCheckHTTPHeadroom is added on top of the workflow timeout so the
// server-side deny-on-expiry fires before the HTTP client gives up.
const permissionCheckHTTPHeadroom = 15 * time.Second

// permissionCheckPath is the validated backend endpoint that resolves a native
// tool-permission prompt into an allow/deny decision (blocking for human
// approval when required).
const permissionCheckPath = "/api/v1/agents/permission-check"

// permissionCheckRequest is the request body for permissionCheckPath. Only
// tool_name is required by the backend; everything else is optional context.
type permissionCheckRequest struct {
	Source          string                 `json:"source"`
	ToolName        string                 `json:"tool_name"`
	ToolInput       map[string]interface{} `json:"tool_input,omitempty"`
	SessionID       string                 `json:"session_id,omitempty"`
	Cwd             string                 `json:"cwd,omitempty"`
	AgentReasoning  string                 `json:"agent_reasoning,omitempty"`
	ClientDecision  string                 `json:"client_decision,omitempty"`
	EvaluationPhase string                 `json:"evaluation_phase,omitempty"`
}

// permissionCheckResponse is the (blocking) response from permissionCheckPath.
type permissionCheckResponse struct {
	Decision     string  `json:"decision"`
	Reason       string  `json:"reason"`
	RequestID    string  `json:"request_id"`
	OperatorNote *string `json:"operator_note,omitempty"`
	// TimedOut distinguishes unanswered approval expiry from explicit denial.
	// Both remain deny: a local prompt cannot replace required central approval.
	TimedOut bool `json:"timed_out,omitempty"`
}

// hookDecision is the normalized outcome the adapter maps into each agent's
// native hook response format.
//
// Central enforcement returns allow or deny. The renderer also understands
// native "ask" envelopes for callers that explicitly need a local prompt.
type hookDecision struct {
	Behavior string // "allow", "deny", or "ask"
	Reason   string
}

// permissionHookCredential is the small per-agent file written at onboarding
// time so the hook can authenticate without re-deriving the durable token.
type permissionHookCredential struct {
	BaseURL          string   `json:"base_url"`
	Token            string   `json:"token"`
	Source           string   `json:"source"`
	RuntimePrincipal string   `json:"runtime_principal,omitempty"`
	ConfigPath       string   `json:"config_path,omitempty"`
	TimeoutSeconds   int      `json:"timeout_seconds,omitempty"`
	PolicyPaths      []string `json:"policy_paths,omitempty"`
	WorkspaceRoot    string   `json:"workspace_root,omitempty"`
	// SafeReadAutoAllow gates the built-in read-only shell command allowlist
	// (isSafeReadShellCommand) that auto-allows would-ask calls like "ls" or
	// "git status". Written by onboarding; a missing field means enabled so
	// pre-existing credential files keep the fatigue fix.
	SafeReadAutoAllow *bool `json:"safe_read_auto_allow,omitempty"`
	// StoreTranscript opts the Cursor usage hook into shipping transcript
	// text (as runtime session activities) in addition to the token counts,
	// titles and short summaries it ships by default. Written by
	// `preloop agents onboard Cursor --store-transcript`; the hook command's
	// own --store-transcript flag has the same effect.
	StoreTranscript *bool `json:"store_transcript,omitempty"`
}

// safeReadAutoAllowEnabled reports whether the built-in read-only command
// allowlist should auto-allow would-ask shell calls.
//
// The default (credential absent or field unset) is per adapter, decided at
// the call site: ON for Cursor, whose real allowlist lives in opaque IDE
// state — without the fallback an absent permissions.json would turn every
// "ls" into a blocking approval. OFF for Claude Code: its permission policy
// is fully readable from settings files, so the hook mirrors it exactly — a
// read-only command the agent would have prompted for must reach Preloop as
// an approval request, not be silently widened into an allow.
func (cred permissionHookCredential) safeReadAutoAllowEnabled(defaultOn bool) bool {
	if cred.SafeReadAutoAllow != nil {
		return *cred.SafeReadAutoAllow
	}
	return defaultOn
}

var agentsPermissionHookCmd = &cobra.Command{
	Use:    "permission-hook",
	Short:  "Bridge a native agent tool-permission prompt to Preloop approvals",
	Hidden: true,
	Long: `Reads a native agent permission hook event on STDIN, asks Preloop whether the
tool call should be allowed (blocking for human approval on mobile/watch when
required), and writes the agent-specific decision JSON to STDOUT.

This command is installed automatically by 'preloop agents onboard --approvals'
and is not intended to be run by hand.`,
	RunE: runAgentsPermissionHook,
}

func init() {
	agentsCmd.AddCommand(agentsPermissionHookCmd)
	agentsPermissionHookCmd.Flags().String(
		"source",
		"",
		"agent source: claude_code, codex_cli, or cursor",
	)
	agentsPermissionHookCmd.Flags().String("hook-event", "", "Codex event: PreToolUse or PermissionRequest (default PermissionRequest)")
	agentsPermissionHookCmd.Flags().Bool(
		"fail-open",
		false,
		"allow only transport/timeouts or HTTP 5xx failures; never authentication, invalid responses, or returned denies",
	)
}

func runAgentsPermissionHook(cmd *cobra.Command, args []string) error {
	source := normalizePermissionSource(mustFlagString(cmd, "source"))
	failOpen, _ := cmd.Flags().GetBool("fail-open")
	if source == "" {
		return fmt.Errorf("--source must be one of claude_code, codex_cli, cursor")
	}

	hookEvent := mustFlagString(cmd, "hook-event")
	if hookEvent != "" && (source != permissionSourceCodexCLI || (hookEvent != "PreToolUse" && hookEvent != "PermissionRequest")) {
		return fmt.Errorf("--hook-event is only supported for Codex PreToolUse or PermissionRequest")
	}
	writeDecision := func(decision hookDecision) error {
		if source == permissionSourceCodexCLI && hookEvent == "PreToolUse" {
			return writeCodexPreToolUseDecision(cmd.OutOrStdout(), decision)
		}
		return writeHookDecision(cmd.OutOrStdout(), source, decision)
	}
	raw, err := io.ReadAll(cmd.InOrStdin())
	if err != nil {
		return writeDecision(failureDecision(source, false, "failed to read hook event from stdin"))
	}
	if source == permissionSourceCodexCLI {
		var event map[string]interface{}
		if err := json.Unmarshal(raw, &event); err != nil {
			return writeDecision(failureDecision(source, false, "invalid hook event JSON"))
		}
		if event == nil {
			return writeDecision(failureDecision(source, false, "hook event must be an object"))
		}
		actual := firstStringField(event, "hook_event_name")
		expected := firstNonEmptyString(hookEvent, "PermissionRequest")
		if actual != "" && actual != expected {
			return writeDecision(failureDecision(source, false, "hook event does not match installed handler"))
		}
		// The installed handler fixes the phase even if an event omits its name.
		event["hook_event_name"] = expected
		raw, err = json.Marshal(event)
		if err != nil {
			return writeDecision(failureDecision(source, false, "invalid hook event"))
		}
	}
	return writeDecision(resolvePermissionDecision(source, raw, failOpen))
}

func mustFlagString(cmd *cobra.Command, name string) string {
	value, _ := cmd.Flags().GetString(name)
	return value
}

// permissionHookGetenv is the env lookup used by host detection. Tests swap
// this to inject Cursor/Claude runtime signals without mutating the process.
var permissionHookGetenv = os.Getenv

// isCursorHostInvokingClaudeHook reports whether this process is Cursor's hook
// runner executing a Claude Code PreToolUse entry loaded as a third-party
// hook from ~/.claude/settings.json.
//
// Cursor documents that hook scripts receive CURSOR_PROJECT_DIR / CURSOR_VERSION
// (and often CURSOR_TRACE_ID). Cursor Agent also sets CURSOR_AGENT=1. Claude
// Code's own hook runner does not set those variables, so their presence is a
// reliable "this is Cursor, not Claude Code" signal.
func isCursorHostInvokingClaudeHook() bool {
	if envTruthy(permissionHookGetenv("CURSOR_AGENT")) {
		return true
	}
	for _, key := range []string{
		"CURSOR_VERSION",
		"CURSOR_PROJECT_DIR",
		"CURSOR_TRACE_ID",
		"CURSOR_CONVERSATION_ID",
	} {
		if strings.TrimSpace(permissionHookGetenv(key)) != "" {
			return true
		}
	}
	return false
}

func envTruthy(value string) bool {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}

// resolvePermissionDecision maps the stdin event to a permission-check request,
// calls the endpoint, and returns the normalized decision. On any hard failure
// it returns the configured safe default (deny, or allow when --fail-open),
// except that local deny remains terminal without a network request.
func resolvePermissionDecision(source string, raw []byte, failOpen bool) hookDecision {
	// Cursor loads ~/.claude/settings.json PreToolUse hooks as third-party
	// hooks (Settings → Rules → Include third-party configs). A Claude Code
	// --approvals install must not gate Cursor Agent; Cursor is governed only
	// via `preloop agents onboard Cursor --approvals` (~/.cursor/hooks.json).
	if source == permissionSourceClaudeCode && isCursorHostInvokingClaudeHook() {
		return hookDecision{
			Behavior: "allow",
			Reason: "Skipped Claude Code approval hook under Cursor; " +
				"onboard Cursor with --approvals to govern Cursor tool calls.",
		}
	}

	// Cursor's preToolUse fires for every tool, including Shell and MCP tools
	// that beforeShellExecution / beforeMCPExecution already gate. Answer the
	// duplicate locally so one command raises exactly one approval request.
	if source == permissionSourceCursor {
		if decision, ok := cursorPreToolUseDuplicateDecision(raw); ok {
			return decision
		}
	}

	req, err := buildPermissionRequest(source, raw, permissionHookCredential{})
	if err != nil {
		return failureDecision(source, false, err.Error())
	}
	if strings.TrimSpace(req.ToolName) == "" {
		return failureDecision(source, false, "hook event did not identify a tool")
	}

	cred, err := resolvePermissionHookCredential(source)
	if err != nil || strings.TrimSpace(cred.Token) == "" {
		// Re-evaluate with empty cred for Cursor (still honors sandbox/allowlist
		// from local policy files) before falling back.
		req, _ = buildPermissionRequest(source, raw, permissionHookCredential{})
		return clientFallbackDecision(req, source, false, fmt.Sprintf(
			"no Preloop credential found for %s (expected under ~/.preloop/agents/). %s",
			permissionSourceDisplayName(source),
			permissionHookRemediation(source),
		))
	}

	// Rebuild with credential so Cursor can detect Preloop MCP by base URL and
	// prefer onboarded policy paths when present.
	req, err = buildPermissionRequest(source, raw, cred)
	if err != nil {
		return failureDecision(source, false, err.Error())
	}

	// A local deny is terminal. Local allow is context for central native rules,
	// which may deny or require approval even when the host would allow.
	switch strings.ToLower(strings.TrimSpace(req.ClientDecision)) {
	case "deny":
		return clientFallbackDecision(req, source, failOpen, "")
	}

	baseURL := strings.TrimSpace(cred.BaseURL)
	if baseURL == "" {
		baseURL = config.DefaultAPIURL
	}

	resp, err := callPermissionCheck(baseURL, cred.Token, req, permissionCheckTimeoutFor(cred))
	if err != nil {
		var unavailable *permissionCheckUnavailableError
		return clientFallbackDecision(req, source, failOpen && errors.As(err, &unavailable), fmt.Sprintf(
			"could not reach Preloop at %s: %v. %s",
			strings.TrimRight(baseURL, "/")+permissionCheckPath,
			err,
			permissionHookRemediation(source),
		))
	}

	behavior := "deny"
	if strings.EqualFold(strings.TrimSpace(resp.Decision), "allow") {
		behavior = "allow"
	}
	reason := strings.TrimSpace(resp.Reason)
	// Returned denials, including approval expiry, are never downgraded to
	// local ask or widened by --fail-open.
	if reason == "" {
		if behavior == "allow" {
			reason = "Approved via Preloop."
		} else {
			reason = "Denied via Preloop."
		}
	}
	return hookDecision{Behavior: behavior, Reason: reason}
}

// cursorPreToolUseDuplicateReason is the allow reason for a Cursor preToolUse
// event whose tool a dedicated before* hook already gates.
const cursorPreToolUseDuplicateReason = "Handled by beforeShellExecution/beforeMCPExecution"

// cursorPreToolUseDuplicateDecision returns an immediate local allow when a
// Cursor preToolUse event targets a tool that beforeShellExecution or
// beforeMCPExecution already gates (Shell, or any MCP tool), and that
// before* hook is still present in ~/.cursor/hooks.json. If the operator
// removed the before* entries, this returns false so preToolUse posts
// normally instead of fail-opening. Onboarding installs all three together.
func cursorPreToolUseDuplicateDecision(raw []byte) (hookDecision, bool) {
	if len(bytes.TrimSpace(raw)) == 0 {
		return hookDecision{}, false
	}
	var event map[string]interface{}
	if err := json.Unmarshal(raw, &event); err != nil {
		return hookDecision{}, false
	}
	if !strings.EqualFold(firstStringField(event, "hook_event_name"), "preToolUse") {
		return hookDecision{}, false
	}
	if !cursorToolHasDedicatedBeforeHook(event, firstStringField(event, "tool_name")) {
		return hookDecision{}, false
	}
	if !cursorPreloopBeforeHookInstalled(cursorDedicatedBeforeHookEvent(event, firstStringField(event, "tool_name"))) {
		return hookDecision{}, false
	}
	return hookDecision{Behavior: "allow", Reason: cursorPreToolUseDuplicateReason}, true
}

// cursorDedicatedBeforeHookEvent is the Cursor before* event that already
// gates this tool, or empty when preToolUse is the only hook for it.
func cursorDedicatedBeforeHookEvent(event map[string]interface{}, toolName string) string {
	trimmed := strings.TrimSpace(toolName)
	if strings.EqualFold(trimmed, "Shell") {
		return "beforeShellExecution"
	}
	if strings.HasPrefix(strings.ToLower(trimmed), "mcp:") {
		return "beforeMCPExecution"
	}
	if firstStringField(event, cursorMCPServerNameKeys...) != "" {
		return "beforeMCPExecution"
	}
	if isPreloopMCPTool(event, trimmed, "") {
		return "beforeMCPExecution"
	}
	return ""
}

// cursorPreloopBeforeHookInstalled is true when ~/.cursor/hooks.json still
// has a Preloop command for eventKey. Without that hook, answering preToolUse
// locally would skip gating entirely.
func cursorPreloopBeforeHookInstalled(eventKey string) bool {
	if eventKey == "" {
		return false
	}
	path, err := approvalHookConfigPath(permissionSourceCursor)
	if err != nil {
		return false
	}
	doc, existed, err := loadJSONDocumentIfExists(path)
	if err != nil || !existed {
		return false
	}
	hooks, ok := asObjectMap(doc["hooks"])
	if !ok {
		return false
	}
	for _, item := range asArrayValue(hooks[eventKey]) {
		entry, ok := asObjectMap(item)
		if !ok {
			continue
		}
		command, _ := entry["command"].(string)
		if strings.Contains(command, permissionHookCommandMarker) {
			return true
		}
	}
	return false
}

// cursorToolHasDedicatedBeforeHook reports whether Cursor fires a dedicated
// before* hook for this tool: beforeShellExecution for Shell, and
// beforeMCPExecution for MCP tools. An MCP tool is recognized when the event
// names an MCP server (mcp_server_name and its aliases), when the tool uses
// Cursor's MCP:<tool_name> matcher form, or when it targets the Preloop MCP.
func cursorToolHasDedicatedBeforeHook(event map[string]interface{}, toolName string) bool {
	return cursorDedicatedBeforeHookEvent(event, toolName) != ""
}

// permissionCheckTimeoutFor returns the HTTP client timeout for a blocking
// permission-check call: workflow timeout + headroom.
func permissionCheckTimeoutFor(cred permissionHookCredential) time.Duration {
	seconds := cred.TimeoutSeconds
	if seconds <= 0 || seconds > maxApprovalWorkflowTimeoutSeconds {
		seconds = maxApprovalWorkflowTimeoutSeconds
	}
	return time.Duration(seconds)*time.Second + permissionCheckHTTPHeadroom
}

// clientFallbackDecision preserves local deny on any failure; otherwise the
// default is closed unless the operator explicitly enabled --fail-open.
func clientFallbackDecision(
	req permissionCheckRequest, source string, failOpen bool, reason string,
) hookDecision {
	switch strings.ToLower(strings.TrimSpace(req.ClientDecision)) {
	case "deny":
		return hookDecision{Behavior: "deny", Reason: "Denied by the agent's own configuration."}
	default:
		return failureDecision(source, failOpen, reason)
	}
}

// failureDecision fails closed when Preloop cannot decide. --fail-open is an
// explicit availability-failure opt-out, never an override of a returned deny.
// Callers pass true only after classifying an eligible transport/HTTP 5xx error.
func failureDecision(source string, failOpen bool, reason string) hookDecision {
	if failOpen {
		return hookDecision{
			Behavior: "allow",
			Reason:   "Preloop approval hook fail-open enabled: " + reason,
		}
	}

	return hookDecision{
		Behavior: "deny",
		Reason:   "Preloop approval hook denied by default: " + reason,
	}
}

// permissionSourceDisplayName maps a permission source onto the agent name the
// onboard/offboard commands accept, for actionable error messages.
func permissionSourceDisplayName(source string) string {
	switch source {
	case permissionSourceClaudeCode:
		return "Claude Code"
	case permissionSourceCodexCLI:
		return "Codex CLI"
	case permissionSourceCursor:
		return "Cursor"
	case permissionSourceOpenCode:
		return "OpenCode"
	default:
		return source
	}
}

// permissionHookRemediation tells the user exactly how to repair the approval
// hook or disable it, for use in fail-closed deny reasons.
func permissionHookRemediation(source string) string {
	remediation := fmt.Sprintf(
		"Re-run `preloop agents onboard %q --approvals` to repair it",
		permissionSourceDisplayName(source),
	)
	if configPath, err := approvalHookConfigPath(source); err == nil {
		remediation += fmt.Sprintf(
			", or remove the Preloop hook from %s to disable approvals",
			configPath,
		)
	}
	return remediation + "."
}

// buildPermissionRequest maps an agent-specific stdin event onto the shared
// permission-check request. cred is optional but lets Cursor detect Preloop MCP
// by base URL and prefer onboarded policy paths.
func buildPermissionRequest(
	source string,
	raw []byte,
	cred permissionHookCredential,
) (permissionCheckRequest, error) {
	var event map[string]interface{}
	if len(bytes.TrimSpace(raw)) > 0 {
		if err := json.Unmarshal(raw, &event); err != nil {
			return permissionCheckRequest{}, fmt.Errorf("invalid hook event JSON: %w", err)
		}
	}

	req := permissionCheckRequest{Source: source}
	req.SessionID = firstStringField(event, "session_id", "conversation_id", "turn_id")
	req.Cwd = firstStringField(event, "cwd")

	switch source {
	case permissionSourceClaudeCode:
		req.ToolName = firstStringField(event, "tool_name")
		req.ToolInput = coerceToolInput(event["tool_input"])
		// Claude's PreToolUse fires on *every* call, so we honor the user's own
		// config and only escalate the would-ask cases to Preloop.
		req.ClientDecision = claudePermissionClientDecision(event, req, cred)
	case permissionSourceCodexCLI:
		req.ToolName = firstStringField(event, "tool_name")
		req.ToolInput = coerceToolInput(event["tool_input"])
		// PreToolUse is a central rules gate before Codex makes its own
		// permission decision. Never claim client allow from its invocation.
		// PermissionRequest retains automatic remote escalation separately.
		if firstStringField(event, "hook_event_name") == "PreToolUse" {
			req.EvaluationPhase = "pre_tool_use"
		}
	case permissionSourceCursor:
		// beforeMCPExecution carries tool_name + tool_input; beforeShellExecution
		// carries a bare shell command with no tool name.
		if toolName := firstStringField(event, "tool_name"); toolName != "" {
			req.ToolName = toolName
			req.ToolInput = coerceToolInput(event["tool_input"])
		} else if command := firstStringField(event, "command"); command != "" {
			req.ToolName = "Shell"
			req.ToolInput = map[string]interface{}{"command": command}
		}
		// Cursor before* hooks fire on every shell/MCP call; evaluate the
		// agent's native permissions.json (+ sandbox) so only would-prompt
		// calls escalate to Preloop.
		req.ClientDecision = cursorPermissionClientDecision(event, req, cred)
	}

	req.AgentReasoning = firstNonEmptyString(
		stringField(req.ToolInput, "description"),
		firstStringField(event, "agent_reasoning"),
	)
	return req, nil
}

// cursorPermissionClientDecision computes allow/ask from Cursor's permissions
// files and the hook event (sandbox, MCP URL). Errors loading policy fall
// through to "ask".
func cursorPermissionClientDecision(
	event map[string]interface{},
	req permissionCheckRequest,
	cred permissionHookCredential,
) string {
	roots := workspaceRootsFromEvent(event)
	if cred.WorkspaceRoot != "" {
		roots = append([]string{cred.WorkspaceRoot}, roots...)
	}
	paths := cred.PolicyPaths
	if len(paths) == 0 {
		paths = discoverCursorPermissionPolicyPaths(roots)
	}
	policy, err := loadCursorPermissionPolicy(paths)
	if err != nil {
		return "ask"
	}
	decision := evaluateCursorPermissionPolicy(
		policy,
		req.ToolName,
		req.ToolInput,
		event,
		cred.BaseURL,
	)
	// Cursor keeps its real allowlist in IDE state, so an absent/empty
	// permissions.json would otherwise turn every "ls" into a blocking
	// approval. Auto-allow obviously read-only shell commands unless
	// onboarding disabled it (default ON for Cursor). Shell calls only — an
	// MCP tool_input may carry an unrelated "command" field.
	if decision == "ask" && cred.safeReadAutoAllowEnabled(true) &&
		strings.EqualFold(strings.TrimSpace(req.ToolName), "Shell") {
		if command := stringField(req.ToolInput, "command"); isSafeReadShellCommand(command) {
			return "allow"
		}
	}
	return decision
}

// claudePermissionClientDecision computes the client_decision Claude Code's own
// config would reach for this call, merging user, project (from the hook
// event's cwd), and enterprise managed settings. Errors loading the policy fall
// back to "ask" (escalate), which is the safe choice.
func claudePermissionClientDecision(
	event map[string]interface{},
	req permissionCheckRequest,
	cred permissionHookCredential,
) string {
	policy, err := loadClaudePermissionPolicy(req.Cwd)
	if err != nil {
		return "ask"
	}
	mode := firstStringField(event, "permission_mode")
	decision := evaluateClaudePermissionPolicy(policy, mode, req.ToolName, req.ToolInput)
	// Claude Code's own policy is fully readable, so the mirror defaults to
	// exact fidelity (default OFF): a read-only Bash command the agent would
	// have prompted for is routed to Preloop as an approval request instead
	// of being widened into a hook-side allow. Operators who prefer fewer
	// approvals can set safe_read_auto_allow=true in permission_hook.json.
	if decision == "ask" && cred.safeReadAutoAllowEnabled(false) &&
		strings.EqualFold(strings.TrimSpace(req.ToolName), "Bash") {
		if command := stringField(req.ToolInput, "command"); isSafeReadShellCommand(command) {
			return "allow"
		}
	}
	return decision
}

// coerceToolInput normalizes a hook event's tool_input into an object map.
// Cursor encodes tool_input as a JSON string, so we attempt to decode it; a
// non-object value is wrapped under a "value" key.
func coerceToolInput(value interface{}) map[string]interface{} {
	switch typed := value.(type) {
	case nil:
		return nil
	case map[string]interface{}:
		return typed
	case string:
		trimmed := strings.TrimSpace(typed)
		if trimmed == "" {
			return nil
		}
		var decoded map[string]interface{}
		if err := json.Unmarshal([]byte(trimmed), &decoded); err == nil {
			return decoded
		}
		return map[string]interface{}{"value": typed}
	default:
		return map[string]interface{}{"value": typed}
	}
}

func firstStringField(event map[string]interface{}, keys ...string) string {
	for _, key := range keys {
		if value, ok := event[key].(string); ok {
			if trimmed := strings.TrimSpace(value); trimmed != "" {
				return trimmed
			}
		}
	}
	return ""
}

// permissionCheckUnavailableError is the only failure eligible for fail-open.
// Authentication, validation, protocol and malformed response errors stay closed.
type permissionCheckUnavailableError struct{ err error }

func (e *permissionCheckUnavailableError) Error() string { return e.err.Error() }
func (e *permissionCheckUnavailableError) Unwrap() error { return e.err }
func permissionTransportError(err error) error {
	original := err
	var requestError *url.Error
	if errors.As(err, &requestError) {
		err = requestError.Err
	}
	var networkError net.Error
	if errors.As(err, &networkError) || errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
		return &permissionCheckUnavailableError{err: original}
	}
	return original
}

// callPermissionCheck POSTs the request to the permission-check endpoint with a
// long timeout (the endpoint blocks for human approval). A dedicated client is
// used because the shared api.Client enforces a short 30s timeout.
func callPermissionCheck(
	baseURL, token string,
	req permissionCheckRequest,
	timeout time.Duration,
) (permissionCheckResponse, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return permissionCheckResponse{}, fmt.Errorf("failed to encode request: %w", err)
	}
	url := strings.TrimRight(baseURL, "/") + permissionCheckPath
	httpReq, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return permissionCheckResponse{}, fmt.Errorf("failed to create request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "application/json")
	version.SetClientIdentityHeaders(httpReq.Header)
	httpReq.Header.Set("Authorization", "Bearer "+token)

	if timeout <= 0 {
		timeout = permissionCheckTimeoutFor(permissionHookCredential{})
	}
	client := &http.Client{Timeout: timeout}
	resp, err := client.Do(httpReq)
	if err != nil {
		return permissionCheckResponse{}, fmt.Errorf("request failed: %w", permissionTransportError(err))
	}
	defer resp.Body.Close() //nolint:errcheck

	// A returned HTTP denial is authoritative even when its body is broken.
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		statusErr := fmt.Errorf("API error (status %d)", resp.StatusCode)
		if resp.StatusCode >= 500 && resp.StatusCode < 600 {
			return permissionCheckResponse{}, &permissionCheckUnavailableError{err: statusErr}
		}
		return permissionCheckResponse{}, statusErr
	}
	responseBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return permissionCheckResponse{}, fmt.Errorf("failed to read permission response: %w", err)
	}

	// Decode only canonical documented fields. Unknown fields remain forward
	// compatible; aliases must not overwrite an authoritative canonical deny.
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(responseBody, &fields); err != nil {
		return permissionCheckResponse{}, fmt.Errorf("failed to decode response: %w", err)
	}
	var decoded permissionCheckResponse
	destinations := map[string]interface{}{
		"decision": &decoded.Decision, "reason": &decoded.Reason, "timed_out": &decoded.TimedOut,
		"request_id": &decoded.RequestID, "operator_note": &decoded.OperatorNote,
	}
	for name, destination := range destinations {
		raw, present := fields[name]
		if !present {
			continue
		}
		if (name == "decision" || name == "reason" || name == "timed_out") && bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
			return permissionCheckResponse{}, fmt.Errorf("invalid null permission response field: %s", name)
		}
		if err := json.Unmarshal(raw, destination); err != nil {
			return permissionCheckResponse{}, fmt.Errorf("invalid permission response field %s: %w", name, err)
		}
	}
	if decoded.Decision != "allow" && decoded.Decision != "deny" {
		return permissionCheckResponse{}, fmt.Errorf("invalid permission response decision")
	}
	if decoded.TimedOut && decoded.Decision == "allow" {
		return permissionCheckResponse{}, fmt.Errorf("invalid permission response: expired approval cannot allow")
	}
	return decoded, nil
}

// A central allow clears only our veto. Empty output leaves Codex's native
// permissions intact, unlike a PermissionRequest allow that approves its prompt.
func writeCodexPreToolUseDecision(out io.Writer, decision hookDecision) error {
	payload := map[string]interface{}{}
	if decision.Behavior != "allow" {
		payload["hookSpecificOutput"] = map[string]interface{}{
			"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": decision.Reason,
		}
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintln(out, string(data))
	return err
}

// writeHookDecision renders the normalized decision into the agent-specific
// hook response format and writes it to out.
func writeHookDecision(out io.Writer, source string, decision hookDecision) error {
	payload := renderHookDecision(source, decision)
	data, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("failed to encode hook decision: %w", err)
	}
	_, err = fmt.Fprintln(out, string(data))
	return err
}

// renderHookDecision builds the agent-specific decision object.
//
//   - Claude Code: PreToolUse hookSpecificOutput with permissionDecision.
//     Supports "ask" natively: the agent shows its own local prompt.
//   - Codex CLI:   PermissionRequest hookSpecificOutput with decision.behavior
//     (deny carries a message). This follows the current official Codex hooks
//     schema; see the deviation note in the onboarding code. No "ask" verdict
//     exists, so a normalized "ask" degrades to deny with the reason attached.
//   - Cursor: {"permission": ...} accepting allow/deny/ask. Cursor reliably
//     enforces "deny" as a veto; "allow" is best-effort because Cursor's
//     in-app allowlist can still override it.
func renderHookDecision(source string, decision hookDecision) map[string]interface{} {
	allow := decision.Behavior == "allow"
	ask := decision.Behavior == "ask"
	switch source {
	case permissionSourceClaudeCode:
		behavior := "deny"
		if allow {
			behavior = "allow"
		}
		if ask {
			behavior = "ask"
		}
		return map[string]interface{}{
			"hookSpecificOutput": map[string]interface{}{
				"hookEventName":            "PreToolUse",
				"permissionDecision":       behavior,
				"permissionDecisionReason": decision.Reason,
			},
		}
	case permissionSourceCodexCLI:
		inner := map[string]interface{}{}
		if allow {
			inner["behavior"] = "allow"
		} else {
			inner["behavior"] = "deny"
			inner["message"] = decision.Reason
		}
		return map[string]interface{}{
			"hookSpecificOutput": map[string]interface{}{
				"hookEventName": "PermissionRequest",
				"decision":      inner,
			},
		}
	case permissionSourceCursor:
		if allow {
			// Best-effort: Cursor may still override an allow via its own
			// in-app allowlist. Deny is the only reliably enforced verdict.
			return map[string]interface{}{"permission": "allow"}
		}
		if ask {
			// Cursor's hook schema accepts "ask": surface Cursor's own
			// permission prompt instead of hard-denying.
			return map[string]interface{}{
				"permission":   "ask",
				"user_message": decision.Reason,
			}
		}
		return map[string]interface{}{
			"permission":    "deny",
			"agent_message": decision.Reason,
			"user_message":  decision.Reason,
		}
	default:
		return map[string]interface{}{"permission": "deny"}
	}
}

func normalizePermissionSource(source string) string {
	switch strings.ToLower(strings.TrimSpace(source)) {
	case permissionSourceClaudeCode, "claude code", "claude-code":
		return permissionSourceClaudeCode
	case permissionSourceCodexCLI, "codex", "codex cli", "codex-cli":
		return permissionSourceCodexCLI
	case permissionSourceCursor:
		return permissionSourceCursor
	default:
		return ""
	}
}

// resolvePermissionHookCredential locates the per-agent credential file for the
// given source. When multiple agents of the same source are onboarded the most
// recently written file wins. For Claude Code, if no per-agent file is found we
// fall back to the durable token stored in ~/.claude/settings.json.
func resolvePermissionHookCredential(source string) (permissionHookCredential, error) {
	creds, err := loadPermissionHookCredentials(source)
	if err != nil {
		return permissionHookCredential{}, err
	}
	if len(creds) > 0 {
		return creds[0], nil
	}
	if source == permissionSourceClaudeCode {
		if token, ok := claudeSettingsBearerToken(); ok {
			baseURL, baseErr := resolveConfiguredAPIURL()
			if baseErr != nil || strings.TrimSpace(baseURL) == "" {
				baseURL = config.DefaultAPIURL
			}
			return permissionHookCredential{
				BaseURL: baseURL,
				Token:   token,
				Source:  source,
			}, nil
		}
	}
	return permissionHookCredential{}, fmt.Errorf("no permission hook credential found for source %q", source)
}

// loadPermissionHookCredentials reads every per-agent permission_hook.json under
// ~/.preloop/agents/*/ matching the requested source, newest first.
func loadPermissionHookCredentials(source string) ([]permissionHookCredential, error) {
	dir, err := permissionHookAgentsDir()
	if err != nil {
		return nil, err
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}

	type scored struct {
		cred    permissionHookCredential
		modTime time.Time
	}
	var matches []scored
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		path := filepath.Join(dir, entry.Name(), permissionHookCredentialFileName)
		info, statErr := os.Stat(path)
		if statErr != nil {
			continue
		}
		data, readErr := os.ReadFile(path)
		if readErr != nil {
			continue
		}
		var cred permissionHookCredential
		if json.Unmarshal(data, &cred) != nil {
			continue
		}
		if !strings.EqualFold(strings.TrimSpace(cred.Source), source) {
			continue
		}
		if strings.TrimSpace(cred.Token) == "" {
			continue
		}
		matches = append(matches, scored{cred: cred, modTime: info.ModTime()})
	}
	sort.SliceStable(matches, func(i, j int) bool {
		return matches[i].modTime.After(matches[j].modTime)
	})
	out := make([]permissionHookCredential, 0, len(matches))
	for _, m := range matches {
		out = append(out, m.cred)
	}
	return out, nil
}

// claudeSettingsBearerToken reads the durable managed token from
// ~/.claude/settings.json. It looks under servers.preloop.headers.Authorization
// first (the documented location) and falls back to mcpServers.preloop.
func claudeSettingsBearerToken() (string, bool) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", false
	}
	doc, err := loadJSONDocument(filepath.Join(home, ".claude", "settings.json"))
	if err != nil {
		return "", false
	}
	for _, containerKey := range []string{"servers", "mcpServers"} {
		container, ok := asObjectMap(doc[containerKey])
		if !ok {
			continue
		}
		server, ok := asObjectMap(container["preloop"])
		if !ok {
			continue
		}
		headers, ok := asObjectMap(server["headers"])
		if !ok {
			continue
		}
		for key, value := range headers {
			if !strings.EqualFold(key, "authorization") {
				continue
			}
			text, ok := value.(string)
			if !ok {
				continue
			}
			trimmed := strings.TrimSpace(text)
			if strings.HasPrefix(strings.ToLower(trimmed), "bearer ") {
				trimmed = strings.TrimSpace(trimmed[7:])
			}
			if trimmed != "" {
				return trimmed, true
			}
		}
	}
	return "", false
}
