package cmd

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// A JWT-shaped synthetic marker (three base64url segments).
const hostGatewayTestMarker = "eyJhbGciOiJIUzI1NiJ9.eyJleGVjdXRpb25faWQiOiJ4In0.c2lnbmF0dXJl"

func gatewayDelivery() map[string]any {
	return map[string]any{"header": hostExecGatewayHeader, "marker": hostGatewayTestMarker}
}

func writeCodexConfig(t *testing.T, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(os.Getenv("CODEX_HOME"), "config.toml"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

const codexPreloopConfig = `model_provider = "preloop"
model = "gpt-5.4"

[model_providers.preloop]
name = "Preloop"
base_url = "https://preloop.example.com/openai/v1"
experimental_bearer_token = "synthetic-agent-key"
wire_api = "responses"
`

func TestJobHostExecGatewayMarkerValidates(t *testing.T) {
	if marker, err := jobHostExecGatewayMarker(map[string]any{}); err != nil || marker != "" {
		t.Fatalf("absent marker = %q, %v", marker, err)
	}
	if marker, err := jobHostExecGatewayMarker(map[string]any{hostExecGatewayKey: gatewayDelivery()}); err != nil || marker != hostGatewayTestMarker {
		t.Fatalf("valid marker = %q, %v", marker, err)
	}
	for _, bad := range []any{
		"not-an-object",
		map[string]any{"header": "Authorization", "marker": hostGatewayTestMarker},
		map[string]any{"header": hostExecGatewayHeader, "marker": "a.b.c\nX-Other: 1"},
		map[string]any{"header": hostExecGatewayHeader, "marker": `a.b.c"`},
		map[string]any{"header": hostExecGatewayHeader, "marker": strings.Repeat("a", 3000) + ".b.c"},
	} {
		if _, err := jobHostExecGatewayMarker(map[string]any{hostExecGatewayKey: bad}); err == nil {
			t.Fatalf("accepted %v", bad)
		}
	}
}

func TestCodexHostJobSendsExecutionMarkerThroughPreloopProvider(t *testing.T) {
	setupCodexReadOnlyHost(t, hostExecProfile{AllowCheckout: true})
	writeCodexConfig(t, codexPreloopConfig)
	run, err := newHostExecJob(codexReadOnlyJob(map[string]any{hostExecGatewayKey: gatewayDelivery()}))
	if err != nil {
		t.Fatal(err)
	}
	defer run.cleanup()
	args := run.cmd.Args
	joined := strings.Join(args, " ")
	header := `model_providers.preloop.env_http_headers.X-Preloop-Execution="PRELOOP_EXECUTION_MARKER"`
	if !strings.Contains(joined, header) {
		t.Fatalf("marker header override missing: %v", args)
	}
	if !strings.Contains(joined, `shell_environment_policy.set.PRELOOP_EXECUTION_MARKER=""`) {
		t.Fatalf("marker not hidden from shell commands: %v", args)
	}
	if args[len(args)-1] != "-" {
		t.Fatalf("prompt marker must stay last: %v", args)
	}
	if strings.Contains(joined, hostGatewayTestMarker) {
		t.Fatal("marker reached argv")
	}
	found := false
	for _, entry := range run.cmd.Env {
		if entry == "PRELOOP_EXECUTION_MARKER="+hostGatewayTestMarker {
			found = true
		}
	}
	if !found {
		t.Fatal("marker not exported to Codex")
	}
	if !strings.Contains(run.gatewayNotice, "carry this execution's marker") {
		t.Fatalf("notice = %q", run.gatewayNotice)
	}
}

func TestCodexHostJobWithoutPreloopProviderSendsNoMarker(t *testing.T) {
	setupCodexReadOnlyHost(t, hostExecProfile{AllowCheckout: true})
	// A separate CODEX_HOME that logs in to ChatGPT directly: no provider.
	writeCodexConfig(t, "model = \"gpt-5.4\"\n")
	run, err := newHostExecJob(codexReadOnlyJob(map[string]any{hostExecGatewayKey: gatewayDelivery()}))
	if err != nil {
		t.Fatal(err)
	}
	defer run.cleanup()
	if strings.Contains(strings.Join(run.cmd.Args, " "), "env_http_headers") {
		t.Fatalf("marker attached to a non-Preloop provider: %v", run.cmd.Args)
	}
	for _, entry := range run.cmd.Env {
		if strings.HasPrefix(entry, "PRELOOP_EXECUTION_MARKER=") {
			t.Fatal("marker exported without a Preloop provider")
		}
	}
	if !strings.Contains(run.gatewayNotice, "gateway_bypassed") {
		t.Fatalf("notice = %q", run.gatewayNotice)
	}
}

func TestCodexHostJobWithoutDeliveredMarkerIsUnchanged(t *testing.T) {
	setupCodexReadOnlyHost(t, hostExecProfile{AllowCheckout: true})
	writeCodexConfig(t, codexPreloopConfig)
	run, err := newHostExecJob(codexReadOnlyJob(nil))
	if err != nil {
		t.Fatal(err)
	}
	defer run.cleanup()
	if strings.Contains(strings.Join(run.cmd.Args, " "), "env_http_headers") || run.gatewayNotice != "" {
		t.Fatalf("unexpected marker plumbing: %v %q", run.cmd.Args, run.gatewayNotice)
	}
}

func TestClaudeHostJobSendsExecutionMarkerHeader(t *testing.T) {
	configDir := setupClaudeHost(t, true, `cat >/dev/null`)
	settings := `{"env":{"ANTHROPIC_BASE_URL":"https://preloop.example.com/anthropic"},` + claudeHookSettings[1:]
	if err := os.WriteFile(filepath.Join(configDir, "settings.json"), []byte(settings), 0o600); err != nil {
		t.Fatal(err)
	}
	run, err := newHostExecJob(claudeJob(map[string]any{hostExecGatewayKey: gatewayDelivery()}))
	if err != nil {
		t.Fatal(err)
	}
	defer run.cleanup()
	want := "ANTHROPIC_CUSTOM_HEADERS=X-Preloop-Execution: " + hostGatewayTestMarker
	found := false
	for _, entry := range run.cmd.Env {
		if entry == want {
			found = true
		}
	}
	if !found {
		t.Fatalf("custom header not set: %v", run.cmd.Env)
	}
}

func TestClaudeHostJobNotRoutedSendsNoMarker(t *testing.T) {
	setupClaudeHost(t, true, `cat >/dev/null`)
	run, err := newHostExecJob(claudeJob(map[string]any{hostExecGatewayKey: gatewayDelivery()}))
	if err != nil {
		t.Fatal(err)
	}
	defer run.cleanup()
	for _, entry := range run.cmd.Env {
		if strings.HasPrefix(entry, "ANTHROPIC_CUSTOM_HEADERS=") {
			t.Fatal("marker sent to a CLI that is not routed to Preloop")
		}
	}
	if !strings.Contains(run.gatewayNotice, "not routed") {
		t.Fatalf("notice = %q", run.gatewayNotice)
	}
}

func TestClaudeGatewayEnvKeepsExistingCustomHeaders(t *testing.T) {
	configDir := t.TempDir()
	t.Setenv("CLAUDE_CONFIG_DIR", configDir)
	if err := os.WriteFile(filepath.Join(configDir, "settings.json"), []byte(`{"env":{"ANTHROPIC_BASE_URL":"https://preloop.example.com/anthropic/"}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	env, ok := claudeHostGatewayEnv(hostGatewayTestMarker, []string{"ANTHROPIC_CUSTOM_HEADERS=X-Team: blue"})
	if !ok || len(env) != 1 || env[0] != "ANTHROPIC_CUSTOM_HEADERS=X-Team: blue\nX-Preloop-Execution: "+hostGatewayTestMarker {
		t.Fatalf("env = %q ok=%v", env, ok)
	}
	if err := os.WriteFile(filepath.Join(configDir, "settings.json"), []byte(`{"env":{"ANTHROPIC_BASE_URL":"https://preloop.example.com/anthropic","ANTHROPIC_CUSTOM_HEADERS":"X-Team: blue"}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, ok := claudeHostGatewayEnv(hostGatewayTestMarker, nil); ok {
		t.Fatal("settings-level custom headers would drop the marker; must report not routed")
	}
}

func TestCodexHostJobAsksForTheNativeResultSchema(t *testing.T) {
	setupCodexReadOnlyHost(t, hostExecProfile{AllowCheckout: true})
	schema := map[string]any{"type": "object", "additionalProperties": false, "required": []any{"status"}, "properties": map[string]any{"status": map[string]any{"type": "string"}}}
	run, err := newHostExecJob(codexReadOnlyJob(map[string]any{hostExecResultSchemaKey: schema}))
	if err != nil {
		t.Fatal(err)
	}
	defer run.cleanup()
	args := run.cmd.Args
	schemaPath := cursorArgValue(args, "--output-schema")
	if schemaPath == "" || cursorArgValue(args, "--output-last-message") != run.resultPath || run.resultPath == "" {
		t.Fatalf("native result flags missing: %v", args)
	}
	if args[len(args)-1] != "-" {
		t.Fatalf("prompt marker must stay last: %v", args)
	}
	written, err := os.ReadFile(schemaPath)
	if err != nil || !strings.Contains(string(written), `"additionalProperties":false`) {
		t.Fatalf("schema file = %q, %v", written, err)
	}
}

func TestNativeResultSchemaIsCodexOnly(t *testing.T) {
	setupClaudeHost(t, true, `cat >/dev/null`)
	if _, err := newHostExecJob(claudeJob(map[string]any{hostExecResultSchemaKey: map[string]any{"type": "object"}})); err == nil {
		t.Fatal("schema accepted for a harness without native results")
	}
	if _, err := jobHostExecResultSchema(map[string]any{hostExecResultSchemaKey: "not-an-object"}); err == nil {
		t.Fatal("non-object schema accepted")
	}
}

func TestCodexNativeResultBecomesTheResultDocument(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "answer.json")
	answer := `{"status":"success","recommendation":"request_changes","findings":[],"harness":"spoofed","session_id":"spoofed"}`
	if err := os.WriteFile(path, []byte(answer), 0o600); err != nil {
		t.Fatal(err)
	}
	b := &runnerLogBuffer{native: true, harness: hostExecHarnessCodex, resultPath: path}
	_, _ = b.Write([]byte(`{"type":"thread.started","thread_id":"11111111-1111-4111-8111-111111111111"}` + "\n" + `{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":4}}` + "\n"))
	b.finish()
	result, err := nativeRunnerResult(b, nil)
	if err != nil {
		t.Fatal(err)
	}
	if result["recommendation"] != "request_changes" || result["status"] != "success" {
		t.Fatalf("answer not used: %v", result)
	}
	if result["harness"] != hostExecHarnessCodex || result["session_id"] != "11111111-1111-4111-8111-111111111111" {
		t.Fatalf("runner facts overwritten: %v", result)
	}
	if _, ok := result["usage"].(map[string]any); !ok {
		t.Fatalf("usage lost: %v", result)
	}
}

func TestCodexNativeResultCannotTurnAFailedTurnIntoSuccess(t *testing.T) {
	path := filepath.Join(t.TempDir(), "answer.json")
	if err := os.WriteFile(path, []byte(`{"status":"success"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	merged := mergeCodexNativeResult(map[string]any{"status": "failure", "harness": hostExecHarnessCodex}, path)
	if merged["status"] != "failure" {
		t.Fatalf("failed turn reported as %v", merged["status"])
	}
	if got := mergeCodexNativeResult(map[string]any{"status": "success"}, filepath.Join(t.TempDir(), "missing.json")); got["status"] != "success" || len(got) != 1 {
		t.Fatalf("missing answer changed the result: %v", got)
	}
}

func TestClaudeGatewayEnvReadsTheProcessBaseURL(t *testing.T) {
	t.Setenv("CLAUDE_CONFIG_DIR", t.TempDir())
	env, ok := claudeHostGatewayEnv(hostGatewayTestMarker, []string{"ANTHROPIC_BASE_URL=https://preloop.example.com/anthropic"})
	if !ok || len(env) != 1 || env[0] != "ANTHROPIC_CUSTOM_HEADERS=X-Preloop-Execution: "+hostGatewayTestMarker {
		t.Fatalf("env = %q ok=%v", env, ok)
	}
	if _, ok := claudeHostGatewayEnv(hostGatewayTestMarker, []string{"ANTHROPIC_BASE_URL=https://api.anthropic.com"}); ok {
		t.Fatal("marker sent to a non-Preloop base URL")
	}
}

func TestClaudeGatewayPlanMergesSettingsLevelCustomHeaders(t *testing.T) {
	configDir := t.TempDir()
	t.Setenv("CLAUDE_CONFIG_DIR", configDir)
	settings := `{"env":{"ANTHROPIC_BASE_URL":"https://preloop.example.com/anthropic","ANTHROPIC_CUSTOM_HEADERS":"X-Team: blue"}}`
	if err := os.WriteFile(filepath.Join(configDir, "settings.json"), []byte(settings), 0o600); err != nil {
		t.Fatal(err)
	}
	scratch := t.TempDir()
	env, args, files, ok, err := claudeHostGatewayPlan(hostGatewayTestMarker, nil, scratch)
	if err != nil || !ok || len(env) != 0 || len(args) != 2 || args[0] != "--settings" || len(files) != 1 {
		t.Fatalf("plan = %v %v %v %v %v", env, args, files, ok, err)
	}
	if strings.Contains(strings.Join(args, " "), hostGatewayTestMarker) {
		t.Fatal("marker reached argv")
	}
	raw, _ := os.ReadFile(args[1])
	var doc map[string]map[string]string
	if json.Unmarshal(raw, &doc) != nil || doc["env"]["ANTHROPIC_CUSTOM_HEADERS"] != "X-Team: blue\nX-Preloop-Execution: "+hostGatewayTestMarker {
		t.Fatalf("settings file = %s", raw)
	}
	if info, _ := os.Stat(args[1]); info.Mode().Perm() != 0o600 {
		t.Fatalf("mode = %v", info.Mode())
	}
}
