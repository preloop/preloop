//go:build windows

package cmd

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// installFakeHostCmdCLI writes a batch fake for the named agent CLI and puts
// its directory on PATH. Batch is the Windows analogue of the #!/bin/sh stub
// the POSIX tests use.
func installFakeHostCmdCLI(t *testing.T, name, body string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, name+".cmd")
	script := "@echo off\r\n" + body + "\r\n"
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return path
}

// TestNewHostExecJobCmdRunsOnWindows leases a Cursor host job against a
// batch fake and checks the structured-completion protocol end to end:
// profile normalization no longer rejects Windows, the workspace is created
// under the profile root, and the stream-json result is parsed from stdout.
func TestNewHostExecJobCmdRunsOnWindows(t *testing.T) {
	testenv.SetTempHome(t)
	root := t.TempDir()
	installFakeHostCmdCLI(
		t, "cursor-agent",
		`echo {"type":"result","subtype":"success","session_id":"ses-ok","is_error":false}`,
	)
	writeHostExecProfiles(t, []hostExecProfile{{
		Name:          "cursor-ask",
		Executable:    "cursor-agent",
		Argv:          []string{"--mode=ask"},
		WorkspaceRoot: root,
	}})
	execID := "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
	cmd, binary, timeout, err := newHostExecJobCmd(map[string]any{
		"agent_type": "cursor", "completion_protocol": "host_exec",
		"host_exec_profile": "cursor-ask",
		"execution_id":      execID,
		"prompt":            "summarize this repository",
	})
	if err != nil {
		t.Fatal(err)
	}
	if timeout <= 0 {
		t.Fatalf("timeout = %s", timeout)
	}
	if hostExecBinaryBase(binary) != "cursor-agent" {
		t.Fatalf("binary = %s", binary)
	}
	if cmd.Dir == "" || !strings.Contains(strings.ToLower(cmd.Dir), execID) {
		t.Fatalf("workspace dir = %s", cmd.Dir)
	}
	var buf bytes.Buffer
	cmd.Stdout, cmd.Stderr = &buf, &buf
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	outcome := waitHostExecJob(cmd, execID, &buf, nil, timeout, "cursor-ask")
	if outcome.status != "SUCCEEDED" {
		t.Fatalf("status=%s err=%s out=%s", outcome.status, outcome.errMsg, buf.String())
	}
	if outcome.result["status"] != "success" || outcome.result["harness"] != "cursor_cli" {
		t.Fatalf("result=%v", outcome.result)
	}
}

// TestNewHostExecJobCmdUnwrapsNpmShimOnWindows installs an npm-style .cmd
// shim plus the Node script it wraps and checks the job runs node.exe on the
// script directly, keeping the prompt out of cmd.exe.
func TestNewHostExecJobCmdUnwrapsNpmShimOnWindows(t *testing.T) {
	if _, err := exec.LookPath("node"); err != nil {
		t.Skip("node.exe not on PATH; npm shim unwrapping needs it")
	}
	testenv.SetTempHome(t)
	root := t.TempDir()
	dir := t.TempDir()
	script := filepath.Join(dir, "node_modules", "@github", "copilot", "index.js")
	if err := os.MkdirAll(filepath.Dir(script), 0o755); err != nil {
		t.Fatal(err)
	}
	js := `console.log(JSON.stringify({type: "result", sessionId: "ses-1", exitCode: 0}));`
	if err := os.WriteFile(script, []byte(js), 0o644); err != nil {
		t.Fatal(err)
	}
	shim := filepath.Join(dir, "copilot.cmd")
	body := "@ECHO off\r\n" +
		`"%_prog%"  "%dp0%\node_modules\@github\copilot\index.js" %*` + "\r\n"
	if err := os.WriteFile(shim, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	t.Setenv("COPILOT_HOME", t.TempDir())
	writeHostExecProfiles(t, []hostExecProfile{{
		Name:          "copilot-seat",
		Executable:    "copilot",
		WorkspaceRoot: root,
	}})
	cmd, binary, timeout, err := newHostExecJobCmd(map[string]any{
		"agent_type": "copilot", "completion_protocol": "host_exec",
		"host_exec_profile": "copilot-seat",
		"execution_id":      "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
		"prompt":            `a "quoted" prompt with 100% special text`,
	})
	if err != nil {
		t.Fatal(err)
	}
	if hostExecBinaryBase(binary) != "node" {
		t.Fatalf("binary = %s, want the unwrapped node.exe", binary)
	}
	buffer := &runnerLogBuffer{native: true, harness: hostExecHarnessCopilot}
	cmd.Stdout, cmd.Stderr = buffer, buffer
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	outcome := waitHostExecJob(
		cmd, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", buffer, nil, timeout, "copilot-seat",
	)
	if outcome.status != "SUCCEEDED" {
		t.Fatalf("status=%s err=%s", outcome.status, outcome.errMsg)
	}
}

// TestHostExecBatchPromptWithQuotesFailsClosed checks that a prompt cmd.exe
// cannot carry safely is rejected before the process starts when the profile
// resolves to a bare batch script (no unwrappable shim).
func TestHostExecBatchPromptWithQuotesFailsClosed(t *testing.T) {
	testenv.SetTempHome(t)
	root := t.TempDir()
	installFakeHostCmdCLI(t, "cursor-agent", "echo unused")
	writeHostExecProfiles(t, []hostExecProfile{{
		Name:          "cursor-ask",
		Executable:    "cursor-agent",
		WorkspaceRoot: root,
	}})
	_, _, _, err := newHostExecJobCmd(map[string]any{
		"agent_type": "cursor", "completion_protocol": "host_exec",
		"host_exec_profile": "cursor-ask",
		"execution_id":      "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
		"prompt":            `contains "quotes" and %PATH%`,
	})
	if err == nil || !strings.Contains(err.Error(), "host_exec_batch_argument_unsafe") {
		t.Fatalf("err = %v", err)
	}
}

// TestKillRunnerJobProcessKillsTreeOnWindows starts a fake agent whose child
// outlives it unless the whole tree is killed, then checks halt semantics:
// killRunnerJobProcess must take down descendants, not just the leased
// process.
func TestKillRunnerJobProcessKillsTreeOnWindows(t *testing.T) {
	testenv.SetTempHome(t)
	probe := t.TempDir()
	root := t.TempDir()
	installFakeHostCmdCLI(t, "cursor-agent",
		`powershell -NoProfile -Command "$p = Start-Process ping -ArgumentList '-n','120','127.0.0.1' -PassThru -WindowStyle Hidden; Set-Content -Path (Join-Path $env:PRELOOP_HOST_EXEC_PROBE 'child.txt') -Value $p.Id; Wait-Process -Id $p.Id"`,
	)
	t.Setenv("PRELOOP_HOST_EXEC_PROBE", probe)
	writeHostExecProfiles(t, []hostExecProfile{{
		Name:          "cursor-ask",
		Executable:    "cursor-agent",
		WorkspaceRoot: root,
		PassEnv:       []string{"PRELOOP_HOST_EXEC_PROBE"},
	}})
	cmd, _, _, err := newHostExecJobCmd(map[string]any{
		"agent_type": "cursor", "completion_protocol": "host_exec",
		"host_exec_profile": "cursor-ask",
		"execution_id":      "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
		"prompt":            "hang",
	})
	if err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	cmd.Stdout, cmd.Stderr = &buf, &buf
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	childPid := 0
	deadline := time.Now().Add(30 * time.Second)
	for childPid == 0 {
		if raw, readErr := os.ReadFile(filepath.Join(probe, "child.txt")); readErr == nil {
			if pid, convErr := strconv.Atoi(strings.TrimSpace(string(raw))); convErr == nil {
				childPid = pid
			}
		}
		if time.Now().After(deadline) {
			killRunnerJobProcess(cmd)
			_ = cmd.Wait()
			t.Fatalf("fake agent never reported its child (out=%s)", buf.String())
		}
		time.Sleep(100 * time.Millisecond)
	}
	killRunnerJobProcess(cmd)
	_ = cmd.Wait()
	deadline = time.Now().Add(10 * time.Second)
	for isProcessAlive(childPid) {
		if time.Now().After(deadline) {
			t.Fatalf("descendant %d still alive after tree kill", childPid)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

// TestWindowsEscapedArgMatchesSyscall pins the portable escaper used for the
// command-line limit to the standard library's own Windows implementation.
func TestWindowsEscapedArgMatchesSyscall(t *testing.T) {
	for _, arg := range []string{
		"", "plain", "has space", "tab\there", `say "hi"`, `a\"b`, `trail\`,
		`trail\ x\`, `C:\Program Files\x\`, `"`, `\\"\\`, "caf\u00e9 ok",
	} {
		var b strings.Builder
		writeWindowsEscapedArg(&b, arg)
		if got, want := b.String(), syscall.EscapeArg(arg); got != want {
			t.Errorf("escape(%q) = %q, syscall.EscapeArg = %q", arg, got, want)
		}
	}
}

// TestWindowsTaskkillPathIsSystemDirectory checks the halt path never
// resolves taskkill through PATH.
func TestWindowsTaskkillPathIsSystemDirectory(t *testing.T) {
	path := windowsTaskkillPath()
	if !filepath.IsAbs(path) ||
		!strings.EqualFold(filepath.Base(filepath.Dir(path)), "System32") {
		t.Fatalf("taskkill path = %q", path)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("taskkill not found at %q: %v", path, err)
	}
}
