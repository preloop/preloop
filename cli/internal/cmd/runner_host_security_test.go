package cmd

import (
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func nativeTestJob() map[string]any {
	return map[string]any{"execution_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "host_exec_profile": "native", "agent_type": "cursor", "completion_protocol": "host_exec", "prompt": "review"}
}

func TestHostExecPromptIsDataAndModelIsMapped(t *testing.T) {
	binary := installFakeHostCLI(t, `printf '%s\0' "$@"`)
	for _, prompt := range []string{"--force", "--workspace=/outside", strings.Repeat("p", 8192)} {
		t.Run(fmt.Sprintf("length-%d", len(prompt)), func(t *testing.T) {
			profile := hostExecProfile{Name: "native", Executable: binary, WorkspaceRoot: t.TempDir(), ModelMap: map[string]string{"api/model": "cursor-local"}}
			writeHostExecProfiles(t, []hostExecProfile{profile})
			job := nativeTestJob()
			job["prompt"] = prompt
			job["model_identifier"] = "api/model"
			cmd, _, _, err := newHostExecJobCmd(job)
			if err != nil {
				t.Fatal(err)
			}
			out, err := cmd.Output()
			if err != nil {
				t.Fatal(err)
			}
			args := strings.Split(strings.TrimSuffix(string(out), "\x00"), "\x00")
			if args[len(args)-2] != "--" || args[len(args)-1] != prompt {
				t.Fatalf("prompt escaped data slot: %q", args)
			}
			if cursorArgValue(args, "--model") != "cursor-local" {
				t.Fatalf("model mapping missing: %q", args)
			}
			if cursorArgsHasFlag(args, "--force") {
				t.Fatal("prompt became force option")
			}
			if cursorArgValue(args, "--workspace") != cmd.Dir {
				t.Fatal("workspace override")
			}
		})
	}
}

func TestHostExecRejectsUnsafeProfilesAndUnknownModels(t *testing.T) {
	for _, args := range [][]string{{"--"}, {"--workspace=/tmp"}, {"--model", "other"}, {"--resume=foreign"}, {"--force"}} {
		_, err := normalizeHostExecProfile(hostExecProfile{Name: "native", Executable: "cursor-agent", WorkspaceRoot: t.TempDir(), Argv: args})
		if err == nil {
			t.Fatalf("accepted managed override %q", args)
		}
	}
	if _, err := normalizeHostExecProfile(hostExecProfile{Name: "native", Executable: "sh", WorkspaceRoot: t.TempDir(), PassModel: true}); err == nil {
		t.Fatal("accepted non-Cursor executable")
	}
	if err := enforceHostExecModel(hostExecProfile{PassModel: true}, map[string]any{"model_identifier": "composer"}); err == nil {
		t.Fatal("regex/pass_model is not a model capability")
	}
	profile := hostExecProfile{ModelMap: map[string]string{"known": "local"}}
	if err := enforceHostExecModel(profile, map[string]any{"model_identifier": "unknown"}); err == nil {
		t.Fatal("accepted unmapped model")
	}
}

func TestHostExecRejectsWorkspaceReuseAndSiblingSymlinks(t *testing.T) {
	root := t.TempDir()
	id := "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
	first, err := boundHostExecWorkspace(root, id)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = boundHostExecWorkspace(root, id); err == nil {
		t.Fatal("reused stale workspace")
	}
	other := "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
	if err = os.Symlink(first, filepath.Join(root, hostExecWorkspaceDir, other)); err != nil {
		t.Fatal(err)
	}
	if _, err = boundHostExecWorkspace(root, other); err == nil {
		t.Fatal("accepted sibling workspace symlink")
	}
	parentRoot := t.TempDir()
	outside := t.TempDir()
	if err = os.Symlink(outside, filepath.Join(parentRoot, hostExecWorkspaceDir)); err != nil {
		t.Fatal(err)
	}
	if _, err = boundHostExecWorkspace(parentRoot, id); err == nil {
		t.Fatal("followed managed parent symlink")
	}
	if entries, _ := os.ReadDir(outside); len(entries) != 0 {
		t.Fatal("created data outside root before rejecting symlink")
	}
}

func TestHostExecTerminalCleanupAndHaltPrecedence(t *testing.T) {
	for _, tc := range []struct {
		name, child string
		halt        bool
		status      string
	}{
		{"redirected child", "sleep 30 >/dev/null 2>&1 &", false, "SUCCEEDED"},
		{"child retains pipe", "sleep 30 &", false, "FAILED"},
		{"halt after parent exit", "sleep 30 &", true, "STOPPED"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			probe := filepath.Join(t.TempDir(), "child")
			t.Setenv("NATIVE_TEST_CHILD", probe)
			binary := installFakeHostCLI(t, tc.child+"\necho $! > \"$NATIVE_TEST_CHILD\"\necho '{\"type\":\"result\",\"subtype\":\"success\"}'\nexit 0")
			writeHostExecProfiles(t, []hostExecProfile{{Name: "native", Executable: binary, WorkspaceRoot: t.TempDir()}})
			cmd, _, _, err := newHostExecJobCmd(nativeTestJob())
			if err != nil {
				t.Fatal(err)
			}
			buffer := &runnerLogBuffer{native: true}
			cmd.Stdout = buffer
			cmd.Stderr = buffer
			if err = cmd.Start(); err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { killRunnerJobProcess(cmd) })
			halted := &atomic.Bool{}
			if tc.halt {
				deadline := time.Now().Add(time.Second)
				for {
					if _, err = os.Stat(probe); err == nil {
						break
					}
					if time.Now().After(deadline) {
						t.Fatal("child not started")
					}
					time.Sleep(time.Millisecond)
				}
				requestJobHalt(halted, cmd)
			}
			outcome := waitHostExecJob(cmd, "execution", buffer, halted, 2*time.Second, "native")
			if outcome.status != tc.status {
				t.Fatalf("status=%s err=%s", outcome.status, outcome.errMsg)
			}
			raw, err := os.ReadFile(probe)
			if err != nil {
				t.Fatal(err)
			}
			pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
			if err != nil {
				t.Fatal(err)
			}
			deadline := time.Now().Add(time.Second)
			for isProcessAlive(pid) {
				if time.Now().After(deadline) {
					t.Fatalf("child %d survived terminal cleanup", pid)
				}
				time.Sleep(10 * time.Millisecond)
			}
		})
	}
}

func TestHostExecNativeCaptureSurvivesRawFlushAndIsBounded(t *testing.T) {
	buffer := &runnerLogBuffer{native: true}
	_, _ = io.WriteString(buffer, "{\"type\":\"system\",\"subtype\":\"init\",\"model\":\"observed-local\"}\n")
	for i := 0; i < 10000; i++ {
		_, _ = io.WriteString(buffer, "ordinary log\n")
		buffer.acknowledge(len(buffer.batch()))
	}
	_, _ = io.WriteString(buffer, "{\"type\":\"result\",\"subtype\":\"success\"}\n")
	result, err := nativeRunnerResult(buffer, nil)
	if err != nil || result["model"] != "observed-local" {
		t.Fatalf("result=%v err=%v", result, err)
	}
	if _, ok := result["requested_model"]; ok {
		t.Fatal("invented requested attribution")
	}
	overflow := &runnerLogBuffer{native: true}
	_, _ = io.WriteString(overflow, strings.Repeat("x", runnerLogPartialLimit+1))
	_, _ = io.WriteString(overflow, "\n{\"type\":\"result\",\"subtype\":\"success\"}\n")
	if _, err = nativeRunnerResult(overflow, nil); err == nil {
		t.Fatal("overflow succeeded")
	}
}

func TestHostExecAndDockerOutcomesUseDistinctProtocolsAfterLogs(t *testing.T) {
	for _, native := range []bool{false, true} {
		t.Run(fmt.Sprint(native), func(t *testing.T) {
			messages := make(chan []map[string]any, 1)
			upgrade := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				conn, err := upgrade.Upgrade(w, r, nil)
				if err != nil {
					return
				}
				defer conn.Close()
				var received []map[string]any
				for {
					var msg map[string]any
					if err = conn.ReadJSON(&msg); err != nil {
						return
					}
					received = append(received, msg)
					if msg["type"] == "complete" {
						messages <- received
						return
					}
				}
			}))
			defer server.Close()
			conn, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), nil)
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			buffer := &runnerLogBuffer{native: native}
			_, _ = io.WriteString(buffer, "final marker\n")
			outcome := leasedJobOutcome{executionID: "execution", status: "SUCCEEDED", hostExec: native, profile: "native", result: map[string]any{"status": "success"}, logBuffer: buffer}
			if err = writeJobOutcome(conn, outcome); err != nil {
				t.Fatal(err)
			}
			select {
			case received := <-messages:
				if len(received) != 2 || received[0]["type"] != "logs" {
					t.Fatalf("order=%v", received)
				}
				complete := received[1]
				if native {
					if complete["completion_protocol"] != "host_exec" || complete["host_exec_profile"] != "native" || complete["launch_version"] != nil {
						t.Fatalf("native envelope=%v", complete)
					}
				} else {
					if complete["completion_protocol"] != "docker_v1" || complete["launch_version"] != float64(1) || complete["host_exec_profile"] != nil {
						t.Fatalf("docker envelope=%v", complete)
					}
				}
			case <-time.After(time.Second):
				t.Fatal("no completion")
			}
		})
	}
}

func TestHostExecRejectsNonDataJobFieldsAndWrongProtocol(t *testing.T) {
	for _, key := range []string{"launch", "launch_version", "script", "environment", "account_api_token", "custom_commands", "argv", "resume_from"} {
		job := nativeTestJob()
		job[key] = "injected"
		if jobRejectedHostExecInjection(job) == "" {
			t.Fatalf("accepted %s", key)
		}
	}
	for _, field := range []string{"agent_type", "completion_protocol"} {
		job := nativeTestJob()
		delete(job, field)
		if _, _, _, err := newHostExecJobCmd(job); err == nil {
			t.Fatalf("missing %s accepted", field)
		}
	}
}
