package cmd

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"os"
	"os/exec"
	"strings"
	"testing"
)

func resultLine(result string, code int) string {
	raw, _ := json.Marshal(map[string]any{"exit_code": code, "result": json.RawMessage(result)})
	return runnerResultPrefix + base64.StdEncoding.EncodeToString(raw)
}

func TestRunnerCompletionRequiresReportAndExit(t *testing.T) {
	for _, tc := range []struct {
		name, output string
		success      bool
	}{
		{"empty shell", "Welcome to the image", false},
		{"sentinel only", "FLOW_EXECUTION_SUCCESS", false},
		{"unknown status", resultLine(`{"status":"partial"}`, 0), false},
		{"agent failure", resultLine(`{"status":"success"}`, 2), false},
		{"invalid json", runnerResultPrefix + "bad", false},
		{"no exit code", runnerResultPrefix + base64.StdEncoding.EncodeToString([]byte(`{"result":{"status":"success"}}`)), false},
		{"success", resultLine(`{"status":"success","tests":["pytest"]}`, 0), true},
		{"completed audit", resultLine(`{"verdict":"fail"}`, 0), true},
		{"duplicate", resultLine(`{"status":"success"}`, 0) + "\n" + resultLine(`{"status":"success"}`, 0), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, logs, err := runnerStructuredResult(splitNonEmptyLines(tc.output))
			if (err == nil) != tc.success {
				t.Fatalf("success=%v error=%v", tc.success, err)
			}
			for _, line := range logs {
				if strings.HasPrefix(line, runnerResultPrefix) {
					t.Fatal("result leaked into logs")
				}
			}
		})
	}
}

func TestRunnerDockerExitCannotBeOverriddenByReport(t *testing.T) {
	for _, code := range []string{"0", "2"} {
		cmd := exec.Command("sh", "-c", `printf '%s\n' "$REPORT"; exit `+code)
		cmd.Env = append(os.Environ(), "REPORT="+resultLine(`{"status":"success"}`, 0))
		var output bytes.Buffer
		cmd.Stdout = &output
		if err := cmd.Start(); err != nil {
			t.Fatal(err)
		}
		outcome := waitDockerJob(cmd, "example", &output, nil)
		if (outcome.status == "SUCCEEDED") != (code == "0") {
			t.Fatalf("outcome: %+v", outcome)
		}
	}
}

func TestRunnerLaunchRejectsMissingAndUnsupported(t *testing.T) {
	valid := map[string]any{"agent_type": "codex", "launch": map[string]any{"version": float64(1), "script": "echo work", "env": map[string]any{"OPENAI_API_KEY": "secret"}}}
	if _, err := runnerLaunchFromJob(valid); err != nil {
		t.Fatal(err)
	}
	for _, job := range []map[string]any{
		{}, {"agent_type": "unknown", "launch": valid["launch"]},
		{"agent_type": "codex", "launch": map[string]any{"version": float64(2), "script": "true", "env": map[string]any{}}},
		{"agent_type": "codex", "launch": map[string]any{"version": float64(1), "script": "true", "env": map[string]any{"BAD=KEY": "secret"}}},
	} {
		if _, err := runnerLaunchFromJob(job); err == nil {
			t.Fatal("invalid launch accepted")
		}
	}
}

func TestRunnerBootstrapPassesNoSecretsInArgv(t *testing.T) {
	env := map[string]string{"PRELOOP_RUNNER_SCRIPT": "private prompt and script", "OPENAI_API_KEY": "secret-key"}
	args := dockerRunArgs("example/custom:1", env, runnerDockerOpts{Launch: true})
	joined := strings.Join(args, " ")
	if !strings.Contains(joined, "--entrypoint /bin/bash") || args[len(args)-1] != runnerBootstrap {
		t.Fatal("missing bootstrap")
	}
	for _, value := range env {
		if strings.Contains(joined, value) {
			t.Fatal("secret in argv")
		}
	}
	preserve := dockerRunArgs("ghcr.io/openai/codex-universal:latest", env, runnerDockerOpts{Launch: true, PreserveEntrypoint: true})
	if strings.Contains(strings.Join(preserve, " "), "--entrypoint") {
		t.Fatal("universal entrypoint overridden")
	}
}

// Optional real-Docker check. The fixture contains a generated hosted script
// with fake credentials and fake CLIs. Network is disabled unconditionally.
func TestRunnerDockerLaunchIntegration(t *testing.T) {
	fixture := os.Getenv("PRELOOP_TEST_RUNNER_LAUNCH")
	if fixture == "" {
		t.Skip("set PRELOOP_TEST_RUNNER_LAUNCH to a local fake-harness launch fixture")
	}
	raw, err := os.ReadFile(fixture)
	if err != nil {
		t.Fatal(err)
	}
	var launch struct {
		Script string            `json:"script"`
		Env    map[string]string `json:"env"`
	}
	if err = json.Unmarshal(raw, &launch); err != nil {
		t.Fatal(err)
	}
	launch.Env["PRELOOP_RUNNER_SCRIPT"] = launch.Script
	launch.Env["PRELOOP_URL"] = "http://example.invalid"
	launch.Env["PRELOOP_MCP_URL"] = "http://example.invalid/mcp/v1"
	launch.Env["PRELOOP_DISABLE_TELEMETRY"] = "true"
	fakebin := os.Getenv("PRELOOP_TEST_RUNNER_FAKEBIN")
	if fakebin == "" {
		t.Fatal("fake CLI directory required")
	}
	cmd := defaultNewRunnerJobCmd("ghcr.io/openai/codex-universal:latest", launch.Env, runnerDockerOpts{Launch: true, PreserveEntrypoint: true, Network: "none", ExtraMounts: []string{fakebin + ":/fakebin:ro"}})
	var output bytes.Buffer
	cmd.Stdout = &output
	cmd.Stderr = &output
	if err = cmd.Start(); err != nil {
		t.Fatal(err)
	}
	outcome := waitDockerJob(cmd, "local-fake", &output, nil)
	if outcome.status != "SUCCEEDED" {
		t.Fatalf("status=%s error=%s logs=%s", outcome.status, outcome.errMsg, strings.Join(outcome.lines, "\n"))
	}
	if outcome.result["fake_harness"] != true {
		t.Fatal("fake harness was not executed")
	}
}
