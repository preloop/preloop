package cmd

import (
	"encoding/json"
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
	hostExecMaxArgv            = 32
	hostExecMaxArgBytes        = 4096
	hostExecMaxPromptBytes     = 64 * 1024
	hostExecDefaultTimeout     = 30 * time.Minute
	hostExecCompletionProtocol = "host_exec"
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
	Name           string            `json:"name"`
	Executable     string            `json:"executable"`
	Argv           []string          `json:"argv"`
	WorkspaceRoot  string            `json:"workspace_root"`
	TimeoutSeconds int               `json:"timeout_seconds"`
	ForceWrites    bool              `json:"force_writes"`
	PassModel      bool              `json:"pass_model"`
	ModelMap       map[string]string `json:"model_map"`
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
	if !filepath.IsAbs(profile.WorkspaceRoot) {
		return hostExecProfile{}, fmt.Errorf("workspace_root must be an absolute path")
	}
	if profile.TimeoutSeconds < 0 {
		return hostExecProfile{}, fmt.Errorf("timeout_seconds must be >= 0")
	}
	if runtime.GOOS == "windows" {
		return hostExecProfile{}, fmt.Errorf("host execution requires Unix process-group ownership")
	}
	if !hostExecIsCursorBinary(profile.Executable) {
		return hostExecProfile{}, fmt.Errorf("host profile executable must be Cursor agent or cursor-agent")
	}
	if len(profile.ModelMap) > 64 {
		return hostExecProfile{}, fmt.Errorf("model_map supports at most 64 entries")
	}
	for requested, alias := range profile.ModelMap {
		if !hostExecModelRe.MatchString(requested) || !hostExecModelRe.MatchString(alias) {
			return hostExecProfile{}, fmt.Errorf("invalid model_map entry")
		}
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
		return nil
	}
	out := make([]hostExecAdvertisement, 0, len(profiles))
	for _, profile := range profiles {
		caps := []string{"host_exec", "stdout", "cancel"}
		if hostExecIsCursorBinary(profile.Executable) {
			caps = append(caps, "cursor_cli")
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
	base := strings.ToLower(filepath.Base(strings.TrimSpace(executable)))
	_, ok := hostExecCursorNames[base]
	return ok
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
		"resume_from", "launch", "launch_version", "script", "environment", "account_api_token", "custom_commands",
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
	if hostExecIsCursorBinary(cleaned) && base == cleaned {
		for _, name := range []string{"cursor-agent", "agent"} {
			path, err := resolveRuntimeExecutable(name)
			if err == nil {
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
		if info.IsDir() || info.Mode()&0111 == 0 {
			return "", fmt.Errorf("executable %q is not runnable", resolved)
		}
		return resolved, nil
	}
	if strings.Contains(cleaned, string(os.PathSeparator)) {
		return "", fmt.Errorf("executable must be a command name or an absolute path")
	}
	return resolveRuntimeExecutable(cleaned)
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
	if len(prompt) > hostExecMaxPromptBytes {
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

func buildHostExecArgs(profile hostExecProfile, job map[string]any, workspace string) ([]string, error) {
	args := ensureCursorCaptureArgs(append([]string{}, profile.Argv...))
	args = append(args, "--workspace", workspace)
	if profile.ForceWrites {
		args = append(args, "--force")
	}
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

func newHostExecJobCmd(job map[string]any) (*exec.Cmd, string, time.Duration, error) {
	if reason := jobRejectedHostExecInjection(job); reason != "" {
		return nil, "", 0, fmt.Errorf("%s", reason)
	}
	if job["agent_type"] != "cursor" || job["completion_protocol"] != hostExecCompletionProtocol {
		return nil, "", 0, fmt.Errorf("host job requires explicit Cursor host_exec protocol")
	}
	name := jobHostExecProfileName(job)
	profile, err := lookupHostExecProfile(name)
	if err != nil {
		return nil, "", 0, err
	}
	if err := enforceHostExecModel(profile, job); err != nil {
		return nil, "", 0, err
	}
	executionID, _ := job["execution_id"].(string)
	workspace, err := boundHostExecWorkspace(profile.WorkspaceRoot, executionID)
	if err != nil {
		return nil, "", 0, err
	}
	bin, err := resolveHostExecBinary(profile.Executable)
	if err != nil {
		return nil, "", 0, err
	}
	args, err := buildHostExecArgs(profile, job, workspace)
	if err != nil {
		return nil, "", 0, err
	}
	cmd := exec.Command(bin, args...)
	cmd.Dir = workspace
	cmd.Env = os.Environ()
	cmd.SysProcAttr = hostExecSysProcAttr()
	cmd.WaitDelay = 250 * time.Millisecond
	return cmd, bin, hostExecTimeout(profile, job), nil
}

func runnerHeartbeatMessage() map[string]any {
	msg := publicationHeartbeat()
	msg["host_exec_profiles"] = hostExecAdvertisements()
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

func beginHostExecJob(conn *websocket.Conn, job map[string]any, executionID string, runningCmd **exec.Cmd, runningExecID *string, jobDone *<-chan leasedJobOutcome, halted *atomic.Bool, lastComplete **leasedJobOutcome) error {
	cmd, _, timeout, err := newHostExecJobCmd(job)
	profile := jobHostExecProfileName(job)
	if err != nil {
		outcome := leasedJobOutcome{executionID: executionID, status: "FAILED", hostExec: true, profile: profile, errMsg: err.Error(), exitCode: -1}
		rememberOutcome(lastComplete, outcome)
		return writeJobOutcome(conn, outcome)
	}
	buffer := &runnerLogBuffer{native: true}
	cmd.Stdout, cmd.Stderr = buffer, buffer
	if err := cmd.Start(); err != nil {
		outcome := leasedJobOutcome{executionID: executionID, status: "FAILED", hostExec: true, profile: profile, errMsg: err.Error(), exitCode: -1}
		rememberOutcome(lastComplete, outcome)
		return writeJobOutcome(conn, outcome)
	}
	*runningCmd, *runningExecID = cmd, executionID
	done := make(chan leasedJobOutcome, 1)
	*jobDone = done
	go func() {
		outcome := waitHostExecJob(cmd, executionID, buffer, halted, timeout, profile)
		if outcome.result != nil {
			if requested := jobModelIdentifier(job); requested != "" {
				outcome.result["requested_model"] = requested
			}
		}
		done <- outcome
	}()
	return nil
}
