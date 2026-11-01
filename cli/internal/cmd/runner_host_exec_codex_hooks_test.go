package cmd

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"testing"
	"time"
)

func codexHookTestResult(workspace string) map[string]any {
	return map[string]any{"data": []any{map[string]any{"cwd": workspace, "errors": []any{}, "hooks": []any{map[string]any{"eventName": "PreToolUse", "handlerType": "command", "command": "preloop agents permission-hook --source codex_cli --hook-event PreToolUse", "enabled": true, "trustStatus": "trusted", "async": false, "matcher": "*"}}}}}
}

func TestCodexHookMetadataRequiresEffectiveTrust(t *testing.T) {
	workspace := t.TempDir()
	for _, mode := range []string{"trusted", "managed", "camel_case_event", "quoted_executable", "disabled", "untrusted", "modified", "async", "matcher", "wrong_source", "wrong_event", "wrong_cwd", "errors", "feature_disabled"} {
		t.Run(mode, func(t *testing.T) {
			result := codexHookTestResult(workspace)
			entry := result["data"].([]any)[0].(map[string]any)
			hook := entry["hooks"].([]any)[0].(map[string]any)
			switch mode {
			case "managed", "untrusted", "modified":
				hook["trustStatus"] = mode
			case "disabled":
				hook["enabled"] = false
			case "async":
				hook["async"] = true
			case "matcher":
				hook["matcher"] = "Read"
			case "wrong_source":
				hook["command"] = "preloop agents permission-hook --source claude_code"
			case "camel_case_event":
				// Codex 0.164 hooks/list output.
				hook["eventName"] = "preToolUse"
			case "quoted_executable":
				hook["command"] = "'/Users/jane/.local/bin/preloop' agents permission-hook --source codex_cli --hook-event PreToolUse"
			case "wrong_event":
				hook["eventName"] = "PostToolUse"
			case "wrong_cwd":
				entry["cwd"] = t.TempDir()
			case "errors":
				entry["errors"] = []any{map[string]any{"message": "bad config"}}
			case "feature_disabled":
				entry["hooks"] = []any{}
			}
			raw, _ := json.Marshal(result)
			want := mode == "trusted" || mode == "managed" || mode == "camel_case_event" || mode == "quoted_executable"
			if got := codexHookMetadataReady(raw, workspace); got != want {
				t.Fatalf("ready=%v want=%v", got, want)
			}
		})
	}
}

// A native test executable implements only initialization and hook metadata.
// It fails if the probe tries to create a session, authenticate or infer.
func TestCodexHookProbeChild(t *testing.T) {
	mode := os.Getenv("PRELOOP_CODEX_HOOK_PROBE_TEST")
	if mode == "" {
		return
	}
	if mode == "wait" {
		time.Sleep(time.Minute)
		os.Exit(0)
	}
	scanner := bufio.NewScanner(os.Stdin)
	stage := 0
	for scanner.Scan() {
		var request struct {
			ID     int    `json:"id"`
			Method string `json:"method"`
			Params struct {
				Cwds []string `json:"cwds"`
			} `json:"params"`
		}
		if json.Unmarshal(scanner.Bytes(), &request) != nil {
			os.Exit(44)
		}
		switch stage {
		case 0:
			if request.Method != "initialize" {
				os.Exit(41)
			}
			if mode == "unsupported" {
				fmt.Println(`{"id":1,"error":{"code":-32601,"message":"unsupported"}}`)
				os.Exit(0)
			}
			fmt.Println(`{"method":"startup/notice"}`)
			fmt.Println(`{"id":1,"result":{"userAgent":"fake"}}`)
		case 1:
			if request.Method != "initialized" {
				os.Exit(42)
			}
		case 2:
			if request.Method != "hooks/list" || len(request.Params.Cwds) != 1 {
				os.Exit(43)
			}
			result := codexHookTestResult(request.Params.Cwds[0])
			if mode == "feature_disabled" {
				result["data"].([]any)[0].(map[string]any)["hooks"] = []any{}
			}
			_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"id": 2, "result": result})
			// Keep stdin open to prove probe owns bounded process cleanup.
			time.Sleep(time.Minute)
		default:
			os.Exit(45)
		}
		stage++
	}
	os.Exit(0)
}

func TestCodexHookRuntimeProbeUsesOnlyMetadata(t *testing.T) {
	for _, mode := range []string{"ready", "unsupported", "feature_disabled", "wait"} {
		t.Run(mode, func(t *testing.T) {
			cmd := exec.Command(os.Args[0], "-test.run=^TestCodexHookProbeChild$")
			cmd.Env = append(os.Environ(), "PRELOOP_CODEX_HOOK_PROBE_TEST="+mode)
			cmd.SysProcAttr = hostExecSysProcAttr()
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			err := probeCodexHookRuntime(ctx, cmd, t.TempDir())
			if (err == nil) != (mode == "ready") {
				t.Fatalf("error=%v", err)
			}
			if cmd.ProcessState == nil {
				t.Fatal("probe child was not reaped")
			}
		})
	}
}

func TestCodexPreloopApprovalCommandRequiresRealInvocation(t *testing.T) {
	for _, tc := range []struct {
		command string
		want    bool
	}{
		{`preloop agents permission-hook --source codex_cli`, true},
		{`"C:\Program Files\Preloop\preloop.exe" agents permission-hook --source codex_cli`, true},
		{`'/opt/Example App/preloop' agents permission-hook --source codex_cli`, true},
		{`preloop agents permission-hook --source codex_cli --hook-event PreToolUse`, true},
		{`echo preloop agents permission-hook --source codex_cli`, false},
		{`false && preloop agents permission-hook --source codex_cli`, false},
		{`$(echo preloop) agents permission-hook --source codex_cli`, false},
		{`C:\Program Files\Preloop\preloop.exe agents permission-hook --source codex_cli`, false},
		{`preloop agents permission-hook --source codex_cli; true`, false},
	} {
		if got := codexPreloopApprovalCommand(tc.command); got != tc.want {
			t.Errorf("%q: got %v", tc.command, got)
		}
	}
}

func TestCodexHookExecutableQuotesNativeShellPaths(t *testing.T) {
	for _, tc := range []struct{ goos, executable, want string }{
		{"windows", `C:\Program Files\Preloop\preloop.exe`, `"C:\Program Files\Preloop\preloop.exe"`},
		{"windows", `C:\Users\Example User\bin\preloop.exe`, `"C:\Users\Example User\bin\preloop.exe"`},
		{"linux", "/opt/Example App/preloop", "'/opt/Example App/preloop'"},
		{"darwin", "/Applications/Example App/preloop", "'/Applications/Example App/preloop'"},
		{"windows", `C:\bin\preloop.exe`, `"C:\bin\preloop.exe"`},
	} {
		got, err := codexHookExecutable(tc.goos, tc.executable)
		if err != nil || got != tc.want {
			t.Errorf("%s %q: got %q, %v", tc.goos, tc.executable, got, err)
		}
		if !codexPreloopApprovalCommand(got + " agents permission-hook --source codex_cli --hook-event PreToolUse") {
			t.Errorf("generated command rejected: %s", got)
		}
	}
	for _, executable := range []string{`C:\Users\%USER%\preloop.exe`, `C:\Users\!USER!\preloop.exe`, "preloop\nother", "preloop;other", "$(other)/preloop", `C:\bad"name\preloop.exe`} {
		for _, goos := range []string{"windows", "linux"} {
			if _, err := codexHookExecutable(goos, executable); err == nil {
				t.Errorf("unsafe %q accepted", executable)
			}
		}
	}
}
