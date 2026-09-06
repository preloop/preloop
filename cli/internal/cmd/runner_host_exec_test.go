package cmd

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func writeHostExecProfiles(t *testing.T, profiles []hostExecProfile) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "runner-host-profiles.json")
	raw, err := json.Marshal(hostExecProfilesFile{Profiles: profiles})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(hostExecProfilesEnv, path)
}

func installFakeHostCLI(t *testing.T, body string) string {
	t.Helper()
	skipNoShebangOnWindows(t, "host execution fake CLI")
	dir := t.TempDir()
	path := filepath.Join(dir, "cursor-agent")
	script := "#!/bin/sh\n" + body + "\n"
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return path
}

func TestLeasedJobFailureReasonHostExecSkipsDocker(t *testing.T) {
	job := map[string]any{"agent_type": "cursor", "completion_protocol": "host_exec",
		"host_exec_profile": "cursor-ask"}
	if got := leasedJobFailureReason(job, false); got != "" {
		t.Fatalf("host exec should not require docker, got %q", got)
	}
}

func TestJobRejectedHostExecInjection(t *testing.T) {
	if got := jobRejectedHostExecInjection(map[string]any{
		"agent_type": "cursor", "completion_protocol": "host_exec",
		"host_exec_profile": "cursor-ask",
		"executable":        "/bin/sh",
	}); !strings.Contains(got, "executable") {
		t.Fatalf("got %q", got)
	}
	if got := jobRejectedHostExecInjection(map[string]any{
		"argv": []any{"-c", "id"},
	}); !strings.Contains(got, "argv") {
		t.Fatalf("got %q", got)
	}
	if got := jobRejectedHostExecInjection(map[string]any{
		"env": map[string]any{"CURSOR_API_KEY": "secret"},
	}); !strings.Contains(got, "env") {
		t.Fatalf("got %q", got)
	}
}

func TestNormalizeHostExecProfileRejectsRelativeRoot(t *testing.T) {
	_, err := normalizeHostExecProfile(hostExecProfile{
		Name:          "bad",
		Executable:    "cursor-agent",
		WorkspaceRoot: "relative/path",
	})
	if err == nil || !strings.Contains(err.Error(), "absolute") {
		t.Fatalf("err = %v", err)
	}
}

func TestBoundHostExecWorkspaceStaysUnderRoot(t *testing.T) {
	root := t.TempDir()
	canonicalRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		t.Fatal(err)
	}
	execID := "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
	got, err := boundHostExecWorkspace(root, execID)
	if err != nil {
		t.Fatal(err)
	}
	rel, err := filepath.Rel(canonicalRoot, got)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		t.Fatalf("escaped %s -> %s", canonicalRoot, got)
	}
}

func TestNewHostExecJobCmdRunsWithoutDocker(t *testing.T) {
	skipNoShebangOnWindows(t, "host execution spawn")
	probe := t.TempDir()
	root := t.TempDir()
	installFakeHostCLI(t, `
printf '%s\0' "$@" > "$PRELOOP_HOST_EXEC_PROBE/argv"
pwd > "$PRELOOP_HOST_EXEC_PROBE/cwd"
echo '{"type":"system","subtype":"init","session_id":"ses-test"}'
echo '{"type":"result","session_id":"ses-test"}'
`)
	t.Setenv("PRELOOP_HOST_EXEC_PROBE", probe)
	writeHostExecProfiles(t, []hostExecProfile{{
		Name:          "cursor-ask",
		Executable:    "cursor-agent",
		Argv:          []string{"--print", "--output-format", "stream-json", "--mode=ask"},
		WorkspaceRoot: root,
		ModelMap:      map[string]string{"composer-2.5": "composer-local"},
	}})
	execID := "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
	cmd, binary, timeout, err := newHostExecJobCmd(map[string]any{
		"agent_type": "cursor", "completion_protocol": "host_exec",
		"host_exec_profile": "cursor-ask",
		"execution_id":      execID,
		"prompt":            "summarize this repository",
		"model_identifier":  "composer-2.5",
	})
	if err != nil {
		t.Fatal(err)
	}
	if timeout <= 0 {
		t.Fatalf("timeout = %s", timeout)
	}
	if filepath.Base(binary) != "cursor-agent" {
		t.Fatalf("binary = %s", binary)
	}
	var buf strings.Builder
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	if err := cmd.Run(); err != nil {
		t.Fatalf("run: %v (%s)", err, buf.String())
	}
	argvRaw, err := os.ReadFile(filepath.Join(probe, "argv"))
	if err != nil {
		t.Fatal(err)
	}
	argv := string(argvRaw)
	if strings.Contains(argv, "/bin/sh") || strings.Contains(argv, "CURSOR_API_KEY") {
		t.Fatalf("argv leaked injection: %q", argv)
	}
	if !strings.Contains(argv, "summarize this repository") {
		t.Fatalf("prompt missing from argv: %q", argv)
	}
	cwd, err := os.ReadFile(filepath.Join(probe, "cwd"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(cwd), execID) {
		t.Fatalf("cwd = %s", cwd)
	}
}

func TestNewHostExecJobCmdUnknownProfile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "empty.json")
	if err := os.WriteFile(path, []byte(`{"profiles":[]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(hostExecProfilesEnv, path)
	_, _, _, err := newHostExecJobCmd(map[string]any{
		"agent_type": "cursor", "completion_protocol": "host_exec",
		"host_exec_profile": "missing",
		"execution_id":      "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
		"prompt":            "x",
	})
	if err == nil || !strings.Contains(err.Error(), "unknown host execution profile") {
		t.Fatalf("err = %v", err)
	}
}

func TestNewHostExecJobCmdRejectsJobArgv(t *testing.T) {
	root := t.TempDir()
	writeHostExecProfiles(t, []hostExecProfile{{
		Name:          "cursor-ask",
		Executable:    "cursor-agent",
		WorkspaceRoot: root,
	}})
	_, _, _, err := newHostExecJobCmd(map[string]any{
		"agent_type": "cursor", "completion_protocol": "host_exec",
		"host_exec_profile": "cursor-ask",
		"execution_id":      "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
		"argv":              []any{"-c", "id"},
		"prompt":            "x",
	})
	if err == nil || !strings.Contains(err.Error(), "argv") {
		t.Fatalf("err = %v", err)
	}
}

func TestHostExecTimeoutKillsChild(t *testing.T) {
	skipNoShebangOnWindows(t, "host execution timeout")
	probe := t.TempDir()
	root := t.TempDir()
	installFakeHostCLI(t, `
sleep 30 &
echo $! > "$PRELOOP_HOST_EXEC_PROBE/child"
wait
`)
	t.Setenv("PRELOOP_HOST_EXEC_PROBE", probe)
	writeHostExecProfiles(t, []hostExecProfile{{
		Name:           "slow",
		Executable:     "cursor-agent",
		WorkspaceRoot:  root,
		TimeoutSeconds: 1,
	}})
	cmd, _, timeout, err := newHostExecJobCmd(map[string]any{
		"agent_type": "cursor", "completion_protocol": "host_exec",
		"host_exec_profile": "slow",
		"execution_id":      "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
		"prompt":            "hang",
	})
	if err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	outcome := waitHostExecJob(cmd, "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", &buf, nil, timeout, "slow")
	if outcome.status != "TIMEOUT" {
		t.Fatalf("status = %s err=%s out=%s", outcome.status, outcome.errMsg, buf.String())
	}
	deadline := time.Now().Add(3 * time.Second)
	for {
		raw, readErr := os.ReadFile(filepath.Join(probe, "child"))
		if readErr == nil {
			pid, convErr := strconv.Atoi(strings.TrimSpace(string(raw)))
			if convErr == nil && pid > 0 && !isProcessAlive(pid) {
				return
			}
		}
		if time.Now().After(deadline) {
			t.Fatalf("descendant still alive")
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func TestRunnerFgHostExecJobSucceedsWithoutDocker(t *testing.T) {
	skipNoShebangOnWindows(t, "host execution runner loop")
	testenv.SetTempHome(t)
	probe := t.TempDir()
	root := t.TempDir()
	installFakeHostCLI(t, `
printf '%s\n' "$@" > "$PRELOOP_HOST_EXEC_PROBE/argv"
echo '{"type":"result","session_id":"ses-test"}'
exit 2
`)
	t.Setenv("PRELOOP_HOST_EXEC_PROBE", probe)
	writeHostExecProfiles(t, []hostExecProfile{{
		Name:          "cursor-ask",
		Executable:    "cursor-agent",
		Argv:          []string{"--mode=ask"},
		WorkspaceRoot: root,
	}})

	oldDocker := runnerHasDocker
	runnerHasDocker = func() bool { return false }
	t.Cleanup(func() { runnerHasDocker = oldDocker })

	execID := "ffffffff-ffff-4fff-8fff-ffffffffffff"
	var completed atomic.Bool
	var status string
	upgrader := websocket.Upgrader{CheckOrigin: func(r *http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/runners/register" {
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			profiles, _ := body["host_exec_profiles"].([]any)
			if len(profiles) == 0 {
				http.Error(w, "missing host_exec_profiles", http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"id":         "11111111-1111-4111-8111-111111111111",
				"account_id": "22222222-2222-4222-8222-222222222222",
				"name":       "box",
				"status":     "online",
				"token":      "runner-token",
				"created_at": "2026-08-17T00:00:00Z",
				"updated_at": "2026-08-17T00:00:00Z",
			})
			return
		}
		if !strings.HasSuffix(r.URL.Path, "/ws") {
			http.NotFound(w, r)
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close() //nolint:errcheck
		_ = conn.WriteJSON(map[string]any{
			"type": "hello",
			"job": map[string]any{
				"execution_id": execID,
				"agent_type":   "cursor", "completion_protocol": "host_exec",
				"host_exec_profile": "cursor-ask",
				"prompt":            "review the change",
			},
		})
		for {
			var msg map[string]any
			if err := conn.ReadJSON(&msg); err != nil {
				return
			}
			if msg["type"] == "complete" && msg["execution_id"] == execID {
				status, _ = msg["status"].(string)
				completed.Store(true)
				return
			}
		}
	}))
	defer server.Close()

	oldToken, oldURL := FlagToken, FlagURL
	FlagURL = server.URL
	FlagToken = "tok"
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })
	client := api.NewClientWithToken(server.URL, "tok")
	state, err := loadOrRegisterRunner(client, "box", "host", nil)
	if err != nil {
		t.Fatal(err)
	}
	interrupt := make(chan os.Signal, 1)
	done := make(chan error, 1)
	go func() {
		done <- runnerForegroundLoop(state, interrupt, io.Discard)
	}()
	deadline := time.Now().Add(8 * time.Second)
	for !completed.Load() && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	interrupt <- os.Interrupt
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("runner did not exit")
	}
	if !completed.Load() {
		t.Fatal("host exec job did not complete")
	}
	if status != "FAILED" {
		t.Fatalf("nonzero child should fail, status=%s", status)
	}
	argv, err := os.ReadFile(filepath.Join(probe, "argv"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(argv), "review the change") {
		t.Fatalf("argv = %s", argv)
	}
}

func TestNewHostExecJobCmdRejectsResumeFrom(t *testing.T) {
	root := t.TempDir()
	writeHostExecProfiles(t, []hostExecProfile{{
		Name:          "cursor-ask",
		Executable:    "cursor-agent",
		WorkspaceRoot: root,
	}})
	_, _, _, err := newHostExecJobCmd(map[string]any{
		"agent_type": "cursor", "completion_protocol": "host_exec",
		"host_exec_profile": "cursor-ask",
		"execution_id":      "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
		"resume_from":       "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
		"prompt":            "x",
	})
	if err == nil || !strings.Contains(err.Error(), "resume_from") {
		t.Fatalf("err = %v", err)
	}
}

func TestNewHostExecJobCmdRejectsUnenforceableModel(t *testing.T) {
	root := t.TempDir()
	writeHostExecProfiles(t, []hostExecProfile{{
		Name:          "cursor-ask",
		Executable:    "cursor-agent",
		WorkspaceRoot: root,
		PassModel:     false,
	}})
	_, _, _, err := newHostExecJobCmd(map[string]any{
		"agent_type": "cursor", "completion_protocol": "host_exec",
		"host_exec_profile": "cursor-ask",
		"execution_id":      "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
		"prompt":            "x",
		"model_identifier":  "composer-2.5",
	})
	want := "not in the local model_map"
	if runtime.GOOS == "windows" {
		// Windows rejects native profiles before checking their model map.
		want = "host execution requires Unix process-group ownership"
	}
	if err == nil || !strings.Contains(err.Error(), want) {
		t.Fatalf("err = %v; want %q", err, want)
	}
}

func TestHostExecExitZeroRequiresStructuredResult(t *testing.T) {
	skipNoShebangOnWindows(t, "host execution structured result")
	root := t.TempDir()
	installFakeHostCLI(t, `echo no-json-here; exit 0`)
	writeHostExecProfiles(t, []hostExecProfile{{
		Name:          "cursor-ask",
		Executable:    "cursor-agent",
		WorkspaceRoot: root,
	}})
	cmd, _, _, err := newHostExecJobCmd(map[string]any{
		"agent_type": "cursor", "completion_protocol": "host_exec",
		"host_exec_profile": "cursor-ask",
		"execution_id":      "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
		"prompt":            "x",
	})
	if err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	outcome := waitHostExecJob(cmd, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", &buf, nil, 0, "cursor-ask")
	if outcome.status != "FAILED" || !strings.Contains(outcome.errMsg, "structured completion") {
		t.Fatalf("status=%s err=%s", outcome.status, outcome.errMsg)
	}
	if !outcome.hostExec {
		t.Fatal("expected host-exec completion protocol")
	}
}

func TestHostExecExitZeroWithResultSucceeds(t *testing.T) {
	skipNoShebangOnWindows(t, "host execution structured success")
	root := t.TempDir()
	installFakeHostCLI(t, `
echo '{"type":"result","subtype":"success","session_id":"ses-ok","is_error":false}'
exit 0
`)
	writeHostExecProfiles(t, []hostExecProfile{{
		Name:          "cursor-ask",
		Executable:    "cursor-agent",
		WorkspaceRoot: root,
	}})
	cmd, _, _, err := newHostExecJobCmd(map[string]any{
		"agent_type": "cursor", "completion_protocol": "host_exec",
		"host_exec_profile": "cursor-ask",
		"execution_id":      "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
		"prompt":            "x",
	})
	if err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	outcome := waitHostExecJob(cmd, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", &buf, nil, 0, "cursor-ask")
	if outcome.status != "SUCCEEDED" {
		t.Fatalf("status=%s err=%s out=%s", outcome.status, outcome.errMsg, buf.String())
	}
	if outcome.result["status"] != "success" || outcome.result["harness"] != "cursor_cli" {
		t.Fatalf("result=%v", outcome.result)
	}
	if outcome.exitCode != 0 {
		t.Fatalf("exit=%v", outcome.exitCode)
	}
}
