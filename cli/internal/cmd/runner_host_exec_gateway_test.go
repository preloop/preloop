package cmd

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
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
	if info, _ := os.Stat(args[1]); runtime.GOOS != "windows" && info.Mode().Perm() != 0o600 {
		t.Fatalf("mode = %v", info.Mode())
	}
}
