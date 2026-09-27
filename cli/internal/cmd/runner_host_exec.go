package cmd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/gorilla/websocket"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"sync/atomic"
	"time"
	"unicode/utf8"

	"github.com/preloop/preloop/cli/internal/config"
)

const (
	hostExecProfilesFileName   = "runner-host-profiles.json"
	hostExecProfilesEnv        = "PRELOOP_RUNNER_HOST_PROFILES"
	hostExecWorkspaceDir       = ".preloop-host-exec"
	hostExecWorkspacesDirName  = "host-workspaces"
	hostExecMaxArgv            = 32
	hostExecMaxArgBytes        = 4096
	hostExecMaxPassEnv         = 64
	hostExecMaxPromptBytes     = 64 * 1024
	hostExecDefaultTimeout     = 30 * time.Minute
	hostExecCompletionProtocol = "host_exec"
	// hostExecPromptPreambleKey marks a runner-local job copy whose prompt
	// carries the checkout preamble. It is set only in memory, after
	// jobRejectedHostExecInjection has run on the delivered job.
	hostExecPromptPreambleKey = "runner_prompt_preamble"
)

var (
	hostExecProfileNameRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
	hostExecModelRe       = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$`)
	hostExecCursorNames   = map[string]struct{}{
		"cursor-agent": {},
		"agent":        {},
	}
)

// hostExecProfile is a runner-local command template. The control plane
// never supplies the executable, argv, environment, or workspace.
type hostExecProfile struct {
	Name       string   `json:"name"`
	Executable string   `json:"executable"`
	Argv       []string `json:"argv"`
	// WorkspaceRoot is optional; empty means the runner's own data
	// directory (~/.preloop/host-workspaces).
	WorkspaceRoot  string            `json:"workspace_root"`
	TimeoutSeconds int               `json:"timeout_seconds"`
	ForceWrites    bool              `json:"force_writes"`
	PassModel      bool              `json:"pass_model"`
	ModelMap       map[string]string `json:"model_map"`
	// PassEnv names extra environment variables copied from the runner
	// process into the job. Everything not named here, in the per-OS
	// baseline, or in the harness's own variable namespace is withheld.
	PassEnv []string `json:"pass_env,omitempty"`
	// Copilot CLI only. AllowTools and DenyTools become --allow-tool and
	// --deny-tool; AllowAllTools is the operator's explicit opt-in to
	// --allow-all-tools and requires the Preloop preToolUse approval hook.
	AllowTools    []string `json:"allow_tools,omitempty"`
	DenyTools     []string `json:"deny_tools,omitempty"`
	AllowAllTools bool     `json:"allow_all_tools,omitempty"`
	// AllowCheckout is the operator's opt-in to cloning a flow's
	// repositories into the execution directory. Repository content is
	// untrusted input to the CLI, so a profile never clones by default.
	AllowCheckout bool `json:"allow_checkout,omitempty"`
}

type hostExecProfilesFile struct {
	Profiles []hostExecProfile `json:"profiles"`
}

type hostExecAdvertisement struct {
	Name         string   `json:"name"`
	Capabilities []string `json:"capabilities"`
	Models       []string `json:"models"`
}

func hostExecProfilesPath() (string, error) {
	if override := strings.TrimSpace(os.Getenv(hostExecProfilesEnv)); override != "" {
		if !filepath.IsAbs(override) {
			return "", fmt.Errorf("%s must be an absolute path", hostExecProfilesEnv)
		}
		return filepath.Clean(override), nil
	}
	dir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, hostExecProfilesFileName), nil
}

func loadHostExecProfiles() ([]hostExecProfile, error) {
	path, err := hostExecProfilesPath()
	if err != nil {
		return nil, err
	}
	file, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	defer file.Close()
	raw, err := io.ReadAll(io.LimitReader(file, 1024*1024+1))
	if len(raw) > 1024*1024 {
		return nil, fmt.Errorf("host profile file exceeds limit")
	}
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("read host execution profiles: %w", err)
	}
	var doc hostExecProfilesFile
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil, fmt.Errorf("parse host execution profiles: %w", err)
	}
	if len(doc.Profiles) > 64 {
		return nil, fmt.Errorf("at most 64 host profiles are supported")
	}
	out := make([]hostExecProfile, 0, len(doc.Profiles))
	seen := map[string]struct{}{}
	for i, profile := range doc.Profiles {
		normalized, err := normalizeHostExecProfile(profile)
		if err != nil {
			return nil, fmt.Errorf("profiles[%d]: %w", i, err)
		}
		key := strings.ToLower(normalized.Name)
		if _, ok := seen[key]; ok {
			return nil, fmt.Errorf("duplicate host execution profile %q", normalized.Name)
		}
		seen[key] = struct{}{}
		out = append(out, normalized)
	}
	return out, nil
}

func normalizeHostExecProfile(profile hostExecProfile) (hostExecProfile, error) {
	profile.Name = strings.TrimSpace(profile.Name)
	if !hostExecProfileNameRe.MatchString(profile.Name) {
		return hostExecProfile{}, fmt.Errorf("invalid profile name %q", profile.Name)
	}
	profile.Executable = strings.TrimSpace(profile.Executable)
	if profile.Executable == "" {
		return hostExecProfile{}, fmt.Errorf("executable is required")
	}
	if strings.ContainsRune(profile.Executable, 0) {
		return hostExecProfile{}, fmt.Errorf("executable contains NUL")
	}
	if err := validateHostExecArgv(profile.Argv); err != nil {
		return hostExecProfile{}, err
	}
	profile.WorkspaceRoot = strings.TrimSpace(profile.WorkspaceRoot)
	if profile.WorkspaceRoot != "" && !filepath.IsAbs(profile.WorkspaceRoot) {
		return hostExecProfile{}, fmt.Errorf("workspace_root must be an absolute path")
	}
	if profile.TimeoutSeconds < 0 {
		return hostExecProfile{}, fmt.Errorf("timeout_seconds must be >= 0")
	}
	if len(profile.PassEnv) > hostExecMaxPassEnv {
		return hostExecProfile{}, fmt.Errorf(
			"pass_env supports at most %d entries", hostExecMaxPassEnv,
		)
	}
	for i, name := range profile.PassEnv {
		if !hostExecEnvNameRe.MatchString(name) {
			return hostExecProfile{}, fmt.Errorf(
				"pass_env[%d] is not a valid environment variable name", i,
			)
		}
	}
	harness := hostExecProfileHarness(profile)
	if harness == "" {
		return hostExecProfile{}, fmt.Errorf("host profile executable must be Cursor agent, cursor-agent or copilot")
	}
	if len(profile.ModelMap) > 64 {
		return hostExecProfile{}, fmt.Errorf("model_map supports at most 64 entries")
	}
	for requested, alias := range profile.ModelMap {
		if !hostExecModelRe.MatchString(requested) || !hostExecModelRe.MatchString(alias) {
			return hostExecProfile{}, fmt.Errorf("invalid model_map entry")
		}
	}
	if harness == hostExecHarnessCopilot {
		if err := validateCopilotHostExecProfile(profile); err != nil {
			return hostExecProfile{}, err
		}
		return profile, nil
	}
	if len(profile.AllowTools) > 0 || len(profile.DenyTools) > 0 || profile.AllowAllTools {
		return hostExecProfile{}, fmt.Errorf("allow_tools, deny_tools and allow_all_tools apply only to copilot profiles")
	}
	for _, arg := range profile.Argv {
		flag := strings.SplitN(arg, "=", 2)[0]
		switch flag {
		case "--", "--workspace", "-w", "--model", "-m", "--resume", "-r", "--continue", "--session-id", "--api-key", "--force", "--yolo", "-f":
			return hostExecProfile{}, fmt.Errorf("profile argv cannot override managed flag %s", flag)
		}
	}
	return profile, nil
}

func validateHostExecArgv(argv []string) error {
	if len(argv) > hostExecMaxArgv {
		return fmt.Errorf("argv has %d entries; max %d", len(argv), hostExecMaxArgv)
	}
	for i, arg := range argv {
		if strings.ContainsRune(arg, 0) || !utf8.ValidString(arg) {
			return fmt.Errorf("argv[%d] is not valid UTF-8", i)
		}
		if len(arg) > hostExecMaxArgBytes {
			return fmt.Errorf("argv[%d] exceeds %d bytes", i, hostExecMaxArgBytes)
		}
	}
	return nil
}

func hostExecAdvertisements() []hostExecAdvertisement {
	profiles, err := loadHostExecProfiles()
	if err != nil || len(profiles) == 0 {
		// A non-nil empty slice marshals as [], not JSON null. Registration
		// and the WebSocket hello both send this key; the control plane
		// treats a missing key differently from an explicit empty list.
		return []hostExecAdvertisement{}
	}
	out := make([]hostExecAdvertisement, 0, len(profiles))
	for _, profile := range profiles {
		caps := []string{"host_exec", "stdout", "cancel"}
		if harness := hostExecProfileHarness(profile); harness != "" {
			caps = append(caps, harness)
		}
		models := make([]string, 0, len(profile.ModelMap))
		for requested := range profile.ModelMap {
			models = append(models, requested)
		}
		sort.Strings(models)
		out = append(out, hostExecAdvertisement{Name: profile.Name, Capabilities: caps, Models: models})
	}
	return out
}

func hostExecIsCursorBinary(executable string) bool {
	_, ok := hostExecCursorNames[hostExecBinaryBase(executable)]
	return ok
}

// hostExecProfileHarness names the local CLI a profile runs. The value is
// also the capability the runner advertises and the result "harness" field
// the control plane checks against the leased agent type.
func hostExecProfileHarness(profile hostExecProfile) string {
	switch {
	case hostExecIsCursorBinary(profile.Executable):
		return hostExecHarnessCursor
	case hostExecIsCopilotBinary(profile.Executable):
		return hostExecHarnessCopilot
	default:
		return ""
	}
}

func jobHostExecProfileName(job map[string]any) string {
	if job == nil {
		return ""
	}
	if name, ok := job["host_exec_profile"].(string); ok {
		return strings.TrimSpace(name)
	}
	return ""
}

func jobRejectedHostExecInjection(job map[string]any) string {
	if job == nil {
		return ""
	}
	for _, key := range []string{
		"executable", "argv", "env", "session_id", "cursor_api_key", "api_key",
		"copilot_github_token", "github_token", "gh_token", "allow_tools", "deny_tools", "allow_all_tools",
		"resume_from", "launch", "launch_version", "script", "environment", "account_api_token", "custom_commands",
		hostExecPromptPreambleKey,
	} {
		if _, ok := job[key]; ok {
			return "job must not supply " + key
		}
	}
	if env, ok := job["environment"].(map[string]any); ok {
		for key := range env {
			lower := strings.ToLower(key)
			if lower == "cursor_api_key" || strings.Contains(lower, "api_key") {
				return "job must not supply credential environment"
			}
		}
	}
	if cfg, ok := job["git_clone_config"].(map[string]any); ok {
		if pr, ok := cfg["create_pull_request"].(bool); ok && pr {
			return "host execution cannot publish pull requests"
		}
	}
	return ""
}

func lookupHostExecProfile(name string) (hostExecProfile, error) {
	want := strings.ToLower(strings.TrimSpace(name))
	if want == "" {
		return hostExecProfile{}, fmt.Errorf("host execution profile is required")
	}
	profiles, err := loadHostExecProfiles()
	if err != nil {
		return hostExecProfile{}, err
	}
	for _, profile := range profiles {
		if strings.ToLower(profile.Name) == want {
			return profile, nil
		}
	}
	return hostExecProfile{}, fmt.Errorf("unknown host execution profile %q", name)
}

func resolveHostExecBinary(executable string) (string, error) {
	cleaned := strings.TrimSpace(executable)
	base := filepath.Base(cleaned)
	if hostExecIsCopilotBinary(cleaned) && base == cleaned {
		for _, name := range hostExecCommandCandidates(cleaned, "copilot") {
			if path, err := resolveHostExecRuntimeExecutable(name); err == nil {
				return path, nil
			}
		}
		return "", fmt.Errorf(
			"copilot_not_installed: Copilot CLI (copilot) was not found on %s; install it with `npm install -g @github/copilot`",
			runtimeExecutableSearchDescription("copilot"),
		)
	}
	if hostExecIsCursorBinary(cleaned) && base == cleaned {
		for _, name := range hostExecCommandCandidates(cleaned, "cursor-agent", "agent") {
			if path, err := resolveHostExecRuntimeExecutable(name); err == nil {
				return path, nil
			}
		}
		return "", fmt.Errorf(
			"cursor CLI (%s) was not found on %s",
			cleaned,
			runtimeExecutableSearchDescription("cursor-agent"),
		)
	}
	if filepath.IsAbs(cleaned) {
		resolved, err := filepath.EvalSymlinks(filepath.Clean(cleaned))
		if err != nil {
			return "", fmt.Errorf("executable %q: %w", cleaned, err)
		}
		info, err := os.Stat(resolved)
		if err != nil {
			return "", err
		}
		if !isExecutableFileInfo(resolved, info) {
			return "", fmt.Errorf("executable %q is not runnable", resolved)
		}
		return resolved, nil
	}
	if strings.ContainsRune(cleaned, os.PathSeparator) ||
		(runtime.GOOS == "windows" && strings.ContainsRune(cleaned, '/')) {
		return "", fmt.Errorf("executable must be a command name or an absolute path")
	}
	return resolveHostExecRuntimeExecutable(cleaned)
}

// hostExecCommandCandidates puts the profile's own spelling first (it may
// carry an explicit Windows extension such as copilot.cmd) and then the
// canonical command names, without duplicates.
func hostExecCommandCandidates(cleaned string, names ...string) []string {
	out := make([]string, 0, len(names)+1)
	seen := map[string]struct{}{}
	for _, name := range append([]string{cleaned}, names...) {
		key := strings.ToLower(name)
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		out = append(out, name)
	}
	return out
}

func canonicalizeExistingDir(path string) (string, error) {
	if !filepath.IsAbs(path) {
		return "", fmt.Errorf("path must be absolute")
	}
	resolved, err := filepath.EvalSymlinks(filepath.Clean(path))
	if err != nil {
		return "", err
	}
	info, err := os.Stat(resolved)
	if err != nil {
		return "", err
	}
	if !info.IsDir() {
		return "", fmt.Errorf("%s is not a directory", resolved)
	}
	return resolved, nil
}

// hostExecWorkspaceRoot returns the profile's workspace root, defaulting to
// a directory under the runner's own data dir. The default is created with
// owner-only permissions; on Windows a directory under the user profile
// inherits the profile's user-only ACLs.
func hostExecWorkspaceRoot(profile hostExecProfile) (string, error) {
	if profile.WorkspaceRoot != "" {
		return profile.WorkspaceRoot, nil
	}
	dir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	root := filepath.Join(dir, hostExecWorkspacesDirName)
	if err := os.MkdirAll(root, 0o700); err != nil {
		return "", err
	}
	return root, nil
}

func boundHostExecWorkspace(root, executionID string) (string, error) {
	if !workspaceIDRe.MatchString(executionID) {
		return "", fmt.Errorf("invalid execution id")
	}
	canonicalRoot, err := canonicalizeExistingDir(root)
	if err != nil {
		return "", fmt.Errorf("workspace_root: %w", err)
	}
	parent := filepath.Join(canonicalRoot, hostExecWorkspaceDir)
	if err := os.Mkdir(parent, 0o700); err != nil && !os.IsExist(err) {
		return "", err
	}
	info, err := os.Lstat(parent)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return "", fmt.Errorf("managed workspace parent must be a real directory")
	}
	execDir := filepath.Join(parent, strings.ToLower(executionID))
	// Native resume is unsupported. Never reuse an existing directory or symlink.
	if err := os.Mkdir(execDir, 0o700); err != nil {
		return "", fmt.Errorf("create fresh execution workspace: %w", err)
	}
	return execDir, nil
}

func hostExecTimeout(profile hostExecProfile, job map[string]any) time.Duration {
	timeout := hostExecDefaultTimeout
	if profile.TimeoutSeconds > 0 {
		timeout = time.Duration(profile.TimeoutSeconds) * time.Second
	}
	if job != nil {
		switch raw := job["timeout_seconds"].(type) {
		case float64:
			if raw > 0 {
				flowTimeout := time.Duration(raw) * time.Second
				if flowTimeout < timeout {
					timeout = flowTimeout
				}
			}
		case json.Number:
			if n, err := raw.Int64(); err == nil && n > 0 {
				flowTimeout := time.Duration(n) * time.Second
				if flowTimeout < timeout {
					timeout = flowTimeout
				}
			}
		}
	}
	if timeout < time.Second {
		return time.Second
	}
	return timeout
}

func jobPromptText(job map[string]any) (string, error) {
	prompt, _ := job["prompt"].(string)
	if !utf8.ValidString(prompt) {
		return "", fmt.Errorf("prompt is not valid UTF-8")
	}
	limit := hostExecMaxPromptBytes
	if prefixed, _ := job[hostExecPromptPreambleKey].(bool); prefixed {
		limit += hostExecPreambleMaxBytes
	}
	if len(prompt) > limit {
		return "", fmt.Errorf("prompt exceeds %d bytes", hostExecMaxPromptBytes)
	}
	return prompt, nil
}

func cursorArgValue(args []string, name string) string {
	for i, arg := range args {
		if arg == "--" {
			return ""
		}
		if arg == name && i+1 < len(args) {
			return args[i+1]
		}
		if strings.HasPrefix(arg, name+"=") {
			return strings.TrimPrefix(arg, name+"=")
		}
	}
	return ""
}

func enforceHostExecModel(profile hostExecProfile, job map[string]any) error {
	raw, ok := job["model_identifier"].(string)
	if job["model_identifier"] != nil && !ok {
		return fmt.Errorf("model_identifier must be a string")
	}
	if raw == "" {
		return nil
	}
	if jobModelIdentifier(job) == "" || profile.ModelMap[raw] == "" {
		return fmt.Errorf("host execution model %q is not in the local model_map", raw)
	}
	return nil
}

func jobModelIdentifier(job map[string]any) string {
	if job == nil {
		return ""
	}
	value, _ := job["model_identifier"].(string)
	value = strings.TrimSpace(value)
	if value == "" || !hostExecModelRe.MatchString(value) {
		return ""
	}
	return value
}

func buildHostExecArgs(profile hostExecProfile, job map[string]any, workspace string, extra ...string) ([]string, error) {
	args := ensureCursorCaptureArgs(append([]string{}, profile.Argv...))
	args = append(args, "--workspace", workspace)
	if profile.ForceWrites {
		args = append(args, "--force")
	}
	args = append(args, extra...)
	if requested := jobModelIdentifier(job); requested != "" {
		alias := profile.ModelMap[requested]
		if alias == "" {
			return nil, fmt.Errorf("model not in local model_map")
		}
		args = append(args, "--model", alias)
	}
	// Template bounds apply only to the template, not the separately bounded prompt.
	prompt, err := jobPromptText(job)
	if err != nil {
		return nil, err
	}
	if strings.ContainsRune(prompt, 0) {
		return nil, fmt.Errorf("prompt contains NUL")
	}
	args = append(args, "--", prompt)
	return args, nil
}

// hostExecRun is a prepared host job: the CLI command plus the work that
// must happen before it starts (checkout) and after it ends (cleanup).
type hostExecRun struct {
	cmd       *exec.Cmd
	timeout   time.Duration
	workspace string
	checkout  *hostExecCheckout
	cleanup   func()
}

func newHostExecJobCmd(job map[string]any) (*exec.Cmd, string, time.Duration, error) {
	run, err := newHostExecJob(job)
	if err != nil {
		return nil, "", 0, err
	}
	return run.cmd, run.cmd.Path, run.timeout, nil
}

// newHostExecJob validates a host lease and prepares everything that does
// not block: profile, fresh execution directory, MCP config and argv. The
// checkout runs later on the job goroutine so the session loop keeps
// heartbeating while git works.
func newHostExecJob(job map[string]any) (*hostExecRun, error) {
	if reason, ok := job["launch_error"].(string); ok && reason != "" {
		if len(reason) > 512 {
			reason = "control plane could not prepare the host execution"
		}
		return nil, fmt.Errorf("host execution launch: %s", reason)
	}
	if reason := jobRejectedHostExecInjection(job); reason != "" {
		return nil, fmt.Errorf("%s", reason)
	}
	agentType, _ := job["agent_type"].(string)
	wantHarness := hostExecHarnessForAgentType(agentType)
	if wantHarness == "" || job["completion_protocol"] != hostExecCompletionProtocol {
		return nil, fmt.Errorf("host job requires explicit Cursor or Copilot host_exec protocol")
	}
	name := jobHostExecProfileName(job)
	profile, err := lookupHostExecProfile(name)
	if err != nil {
		return nil, err
	}
	if harness := hostExecProfileHarness(profile); harness != wantHarness {
		return nil, fmt.Errorf(
			"host execution profile %q runs %s, not the leased %s harness",
			profile.Name, harness, wantHarness,
		)
	}
	if err := enforceHostExecModel(profile, job); err != nil {
		return nil, err
	}
	mcpToken, err := jobHostExecMCPToken(job)
	if err != nil {
		return nil, err
	}
	checkout, err := jobHostExecCheckout(job)
	if err != nil {
		return nil, err
	}
	if checkout != nil && !profile.AllowCheckout {
		return nil, fmt.Errorf(
			"%s: the flow clones repositories but host profile %q does not set allow_checkout; set \"allow_checkout\": true in %s to let this profile clone flow repositories",
			hostExecCheckoutNotAllows, profile.Name, hostExecProfilesFileName,
		)
	}
	executionID, _ := job["execution_id"].(string)
	root, err := hostExecWorkspaceRoot(profile)
	if err != nil {
		return nil, err
	}
	workspace, err := boundHostExecWorkspace(root, executionID)
	if err != nil {
		return nil, err
	}
	bin, argvPrefix, err := resolveHostExecCommand(profile.Executable)
	if err != nil {
		return nil, err
	}
	if preamble := hostExecCheckoutPreamble(workspace, checkout); preamble != "" {
		prompt, err := jobPromptText(job)
		if err != nil {
			return nil, err
		}
		if len(preamble) > hostExecPreambleMaxBytes {
			return nil, fmt.Errorf("host checkout paths exceed %d bytes", hostExecPreambleMaxBytes)
		}
		job = cloneJobWithPrompt(job, preamble+prompt)
	}
	var files []string
	cleanup := func() {
		for _, path := range files {
			_ = os.Remove(path)
		}
	}
	var mcpArgs []string
	if mcpToken != "" {
		mcpArgs, files, err = hostExecMCPConfig(wantHarness, workspace, mcpToken)
		if err != nil {
			return nil, err
		}
	}
	var args []string
	env := hostExecFlowEnv(
		hostExecChildEnv(runtime.GOOS, wantHarness, profile, os.Environ()), job,
	)
	if wantHarness == hostExecHarnessCopilot {
		if err = prepareCopilotHostExecHooks(profile); err == nil {
			args, err = buildCopilotHostExecArgs(profile, job, mcpArgs...)
		}
		env = copilotHostExecEnv(env)
	} else {
		args, err = buildHostExecArgs(profile, job, workspace, mcpArgs...)
	}
	if err != nil {
		cleanup()
		return nil, err
	}
	args = append(argvPrefix, args...)
	if err := hostExecCommandLineError(runtime.GOOS, bin, args); err != nil {
		cleanup()
		return nil, err
	}
	cmd := exec.Command(bin, args...)
	cmd.Dir = workspace
	cmd.Env = env
	cmd.SysProcAttr = hostExecSysProcAttr()
	cmd.WaitDelay = 250 * time.Millisecond
	return &hostExecRun{
		cmd:       cmd,
		timeout:   hostExecTimeout(profile, job),
		workspace: workspace,
		checkout:  checkout,
		cleanup:   cleanup,
	}, nil
}

// cloneJobWithPrompt returns a shallow copy with a replaced prompt so the
// caller's job map (which may be retained for replay) is never modified.
// The prompt bound applies to the flow prompt, not the runner preamble.
func cloneJobWithPrompt(job map[string]any, prompt string) map[string]any {
	out := make(map[string]any, len(job)+1)
	for key, value := range job {
		out[key] = value
	}
	out["prompt"] = prompt
	out[hostExecPromptPreambleKey] = true
	return out
}

// runnerHeartbeatMessage reports what this process can do, including how
// many executions it is willing to hold. The server treats that as a
// ceiling it may lower, never raise.
func runnerHeartbeatMessage(concurrency int) map[string]any {
	msg := publicationHeartbeat()
	msg["host_exec_profiles"] = hostExecAdvertisements()
	// Re-assert ephemeral on every handshake and heartbeat. Registration
	// already set it, but a row that predates the flag (or a reconnect to a
	// replica that has not seen the register) must still be deletable when
	// this process disappears without unregistering.
	if runnerOnce.registersEphemeral() {
		msg["ephemeral"] = true
	}
	if concurrency > 0 {
		msg["concurrency"] = concurrency
	}
	return msg
}

func hostExecStructuredResult(raw []byte) (map[string]any, error) {
	buffer := &runnerLogBuffer{native: true}
	_, _ = buffer.Write(raw)
	buffer.finish()
	return nativeRunnerResult(buffer, nil)
}

func waitHostExecJob(cmd *exec.Cmd, executionID string, buf interface{ String() string }, halted *atomic.Bool, timeout time.Duration, profile string) leasedJobOutcome {
	// Every terminal path owns the process group, including an exit-0 parent
	// leaving detached pipe writers or children with redirected output.
	defer killRunnerJobProcess(cmd)
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	timer := time.NewTimer(timeout)
	if timeout <= 0 {
		timer.Stop()
	}
	defer timer.Stop()
	var err error
	timedOut := false
	select {
	case err = <-done:
	case <-timer.C:
		timedOut = true
		killRunnerJobProcess(cmd)
		err = <-done
	}
	outcome := leasedJobOutcome{executionID: executionID, status: "SUCCEEDED", hostExec: true, profile: profile, exitCode: -1}
	if cmd.ProcessState != nil {
		outcome.exitCode = cmd.ProcessState.ExitCode()
	}
	if buffer, ok := buf.(*runnerLogBuffer); ok {
		buffer.finish()
		outcome.logBuffer = buffer
		outcome.result, err = nativeRunnerResult(buffer, err)
	} else {
		outcome.lines = splitNonEmptyLines(buf.String())
		if err == nil {
			outcome.result, err = hostExecStructuredResult([]byte(buf.String()))
		}
	}
	switch {
	case halted != nil && halted.Load():
		outcome.status = "STOPPED"
	case timedOut:
		outcome.status = "TIMEOUT"
		outcome.errMsg = "host execution exceeded timeout"
	case err != nil:
		outcome.status = "FAILED"
		outcome.errMsg = err.Error()
	case outcome.result["status"] != "success":
		outcome.status = "FAILED"
		outcome.errMsg = "host execution reported a structured failure"
	}
	return outcome
}

func beginHostExecJob(
	conn *websocket.Conn,
	job map[string]any,
	executionID string,
	halted *atomic.Bool,
	jobs *runnerJobs,
) error {
	run, err := newHostExecJob(job)
	profile := jobHostExecProfileName(job)
	if err != nil {
		outcome := leasedJobOutcome{executionID: executionID, status: "FAILED", hostExec: true, profile: profile, errMsg: err.Error(), exitCode: -1}
		jobs.remember(outcome)
		return writeJobOutcome(conn, outcome)
	}
	cmd, timeout := run.cmd, run.timeout
	agentType, _ := job["agent_type"].(string)
	buffer := &runnerLogBuffer{native: true, harness: hostExecHarnessForAgentType(agentType)}
	cmd.Stdout, cmd.Stderr = buffer, buffer
	gate := &hostExecGate{}
	jobs.start(&runnerJob{executionID: executionID, cmd: cmd, halted: halted, hostGate: gate})
	done := jobs.outcomes
	go func() {
		defer run.cleanup()
		failed := func(status, msg string) {
			buffer.finish()
			done <- leasedJobOutcome{executionID: executionID, status: status, hostExec: true, profile: profile, errMsg: msg, exitCode: -1, logBuffer: buffer}
		}
		if run.checkout != nil {
			checkoutTimeout := hostExecCheckoutTimeout
			if timeout < checkoutTimeout {
				checkoutTimeout = timeout
			}
			ctx, cancel := context.WithTimeout(context.Background(), checkoutTimeout)
			if !gate.setCancel(cancel) {
				failed("STOPPED", "")
				return
			}
			err := runHostExecCheckout(ctx, run.workspace, run.checkout, buffer.note)
			cancel()
			switch {
			case halted.Load():
				failed("STOPPED", "")
				return
			case err != nil:
				failed("FAILED", err.Error())
				return
			}
		}
		if err := gate.start(cmd); err != nil {
			if errors.Is(err, errHostExecHaltedBeforeStart) {
				failed("STOPPED", "")
			} else {
				failed("FAILED", err.Error())
			}
			return
		}
		outcome := waitHostExecJob(cmd, executionID, buffer, halted, timeout, profile)
		if buffer.harness == hostExecHarnessCopilot && outcome.status == "FAILED" {
			outcome.errMsg = copilotHostExecFailure(buffer, profile, job, outcome.errMsg)
		}
		if outcome.result != nil {
			if requested := jobModelIdentifier(job); requested != "" {
				outcome.result["requested_model"] = requested
			}
		}
		done <- outcome
	}()
	return nil
}
