package cmd

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

var errCodexHooksNotReady = errors.New("codex_approval_hook_inactive: Codex must expose an enabled, trusted synchronous PreToolUse Preloop hook for this workspace; enable hooks and use Codex /hooks as the runner user to trust the current Preloop hook (or use administrator-managed policy); upgrade Codex if hooks/list is unsupported")

// codexApprovalRuntimeReady asks Codex to resolve its effective hook config.
// hooks/list returns no hooks when the hooks feature is disabled. Trust hashes
// belong to Codex: do not reproduce its normalization or automatically trust a
// declaration. No thread, turn, login, or model request is sent.
func codexApprovalRuntimeReady(ctx context.Context, executable, workspace string) error {
	bin, prefix, err := resolveHostExecCommand(executable)
	if err != nil {
		return errCodexHooksNotReady
	}
	args := append(append([]string{}, prefix...), "app-server")
	if hostExecCommandLineError(runtime.GOOS, bin, args) != nil {
		return errCodexHooksNotReady
	}
	cmd := exec.Command(bin, args...)
	cmd.Dir = workspace
	cmd.Env = hostExecChildEnv(runtime.GOOS, hostExecHarnessCodex, hostExecProfile{}, os.Environ())
	cmd.SysProcAttr = hostExecSysProcAttr()
	return probeCodexHookRuntime(ctx, cmd, workspace)
}

func probeCodexHookRuntime(ctx context.Context, cmd *exec.Cmd, workspace string) error {
	if ctx.Err() != nil {
		return errCodexHooksNotReady
	}
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return errCodexHooksNotReady
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		_ = stdin.Close()
		return errCodexHooksNotReady
	}
	cmd.Stderr = io.Discard
	if err := cmd.Start(); err != nil {
		_ = stdin.Close()
		return errCodexHooksNotReady
	}
	// Cancellation owns killing until Wait has reaped the process; never race
	// a stale PID against a later process. Close stdout to unblock Scanner too.
	stopped := make(chan struct{})
	watcherDone := make(chan struct{})
	go func() {
		defer close(watcherDone)
		select {
		case <-ctx.Done():
			killRunnerJobProcess(cmd)
			_ = stdout.Close()
			_ = stdin.Close()
		case <-stopped:
		}
	}()
	defer func() {
		close(stopped)
		<-watcherDone
		killRunnerJobProcess(cmd)
		_ = stdin.Close()
		_ = cmd.Wait()
	}()
	encoder := json.NewEncoder(stdin)
	if encoder.Encode(map[string]any{"id": 1, "method": "initialize", "params": map[string]any{"clientInfo": map[string]any{"name": "preloop-runner-hook-check", "version": "1"}, "capabilities": map[string]any{"experimentalApi": true}}}) != nil {
		return errCodexHooksNotReady
	}
	scanner := bufio.NewScanner(io.LimitReader(stdout, 4*1024*1024))
	scanner.Buffer(make([]byte, 4096), 1024*1024)
	initialized := false
	for messages := 0; messages < 128 && scanner.Scan(); messages++ {
		var message struct {
			ID     *int            `json:"id"`
			Result json.RawMessage `json:"result"`
			Error  json.RawMessage `json:"error"`
		}
		if json.Unmarshal(scanner.Bytes(), &message) != nil {
			return errCodexHooksNotReady
		}
		if message.ID == nil {
			continue
		}
		if len(message.Error) != 0 && string(message.Error) != "null" {
			return errCodexHooksNotReady
		}
		switch *message.ID {
		case 1:
			if initialized {
				return errCodexHooksNotReady
			}
			initialized = true
			if len(message.Result) == 0 || string(message.Result) == "null" {
				return errCodexHooksNotReady
			}
			if encoder.Encode(map[string]any{"method": "initialized"}) != nil {
				return errCodexHooksNotReady
			}
			if encoder.Encode(map[string]any{"id": 2, "method": "hooks/list", "params": map[string]any{"cwds": []string{workspace}}}) != nil {
				return errCodexHooksNotReady
			}
		case 2:
			if !initialized || ctx.Err() != nil || !codexHookMetadataReady(message.Result, workspace) {
				return errCodexHooksNotReady
			}
			return nil
		default:
			return errCodexHooksNotReady
		}
	}
	return errCodexHooksNotReady
}

func codexHookMetadataReady(result json.RawMessage, workspace string) bool {
	var response struct {
		Data []struct {
			Cwd    string            `json:"cwd"`
			Errors []json.RawMessage `json:"errors"`
			Hooks  []struct {
				Event   string  `json:"eventName"`
				Handler string  `json:"handlerType"`
				Command string  `json:"command"`
				Async   bool    `json:"async"`
				Matcher *string `json:"matcher"`
				Enabled bool    `json:"enabled"`
				Trust   string  `json:"trustStatus"`
			} `json:"hooks"`
		} `json:"data"`
	}
	if json.Unmarshal(result, &response) != nil || len(response.Data) != 1 {
		return false
	}
	entry := response.Data[0]
	if filepath.Clean(entry.Cwd) != filepath.Clean(workspace) || len(entry.Errors) != 0 {
		return false
	}
	for _, hook := range entry.Hooks {
		// Codex reports the event as "PreToolUse" in older releases and
		// "preToolUse" from 0.164.
		if !strings.EqualFold(hook.Event, "PreToolUse") || hook.Handler != "command" || hook.Async || !hook.Enabled || (hook.Trust != "trusted" && hook.Trust != "managed") {
			continue
		}
		if hook.Matcher != nil && *hook.Matcher != "" && *hook.Matcher != "*" && *hook.Matcher != ".*" {
			continue
		}
		if codexPreloopApprovalCommand(hook.Command) {
			return true
		}
	}
	return false
}

// Accept the command onboarding writes, not shell text merely mentioning it.
func codexPreloopApprovalCommand(command string) bool {
	command = strings.TrimSuffix(command, " --hook-event PreToolUse")
	const suffix = " agents permission-hook --source codex_cli"
	if !strings.HasSuffix(command, suffix) {
		return false
	}
	executable := strings.TrimSuffix(command, suffix)
	if strings.ContainsAny(executable, "\r\n;&|`$<>%!^") {
		return false
	}
	if strings.HasPrefix(executable, "\"") || strings.HasPrefix(executable, "'") {
		quote := executable[:1]
		if len(executable) < 2 || !strings.HasSuffix(executable, quote) {
			return false
		}
		executable = executable[1 : len(executable)-1]
		if strings.Contains(executable, quote) {
			return false
		}
	} else if strings.ContainsAny(executable, " \t\"'") {
		return false
	}
	executable = strings.ReplaceAll(executable, "\\", "/")
	return filepath.Base(executable) == "preloop" || filepath.Base(executable) == "preloop.exe"
}
