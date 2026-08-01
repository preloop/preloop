package cmd

import (
	"bytes"
	"encoding/base64"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

const hermesSampleConfig = `# Sample Hermes Agent config
mcp_servers:
  github:
    command: "npx"
    args:
      - "-y"
      - "@modelcontextprotocol/server-github"
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_existing"
  remote_api:
    url: "https://mcp.example.com/mcp"
    headers:
      Authorization: "Bearer existing-token"
`

func TestParseHermesConfig_StdioAndHTTPServers(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(configPath, []byte(hermesSampleConfig), 0644); err != nil {
		t.Fatalf("failed to write Hermes config: %v", err)
	}

	servers, err := parseHermesConfig(configPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	github, ok := servers["github"]
	if !ok {
		t.Fatalf("expected github stdio server, got %+v", servers)
	}
	if github.Command != "npx" {
		t.Fatalf("unexpected github command: %q", github.Command)
	}
	if len(github.Args) != 2 || github.Args[0] != "-y" {
		t.Fatalf("unexpected github args: %+v", github.Args)
	}
	if github.Env["GITHUB_PERSONAL_ACCESS_TOKEN"] != "ghp_existing" {
		t.Fatalf("unexpected github env: %+v", github.Env)
	}

	remote, ok := servers["remote_api"]
	if !ok {
		t.Fatalf("expected remote_api HTTP server, got %+v", servers)
	}
	if remote.URL != "https://mcp.example.com/mcp" {
		t.Fatalf("unexpected remote_api URL: %q", remote.URL)
	}
	if remote.Headers["Authorization"] != "Bearer existing-token" {
		t.Fatalf("unexpected remote_api headers: %+v", remote.Headers)
	}
}

func TestParseHermesConfig_EmptyOrMissingDocument(t *testing.T) {
	dir := t.TempDir()

	emptyPath := filepath.Join(dir, "empty.yaml")
	if err := os.WriteFile(emptyPath, []byte(""), 0644); err != nil {
		t.Fatalf("failed to write empty Hermes config: %v", err)
	}
	servers, err := parseHermesConfig(emptyPath)
	if err != nil {
		t.Fatalf("unexpected error parsing empty config: %v", err)
	}
	if len(servers) != 0 {
		t.Fatalf("expected no servers from empty config, got %+v", servers)
	}

	commentsOnly := filepath.Join(dir, "comments.yaml")
	if err := os.WriteFile(commentsOnly, []byte("# only comments\n"), 0644); err != nil {
		t.Fatalf("failed to write commented Hermes config: %v", err)
	}
	servers, err = parseHermesConfig(commentsOnly)
	if err != nil {
		t.Fatalf("unexpected error parsing comments-only config: %v", err)
	}
	if len(servers) != 0 {
		t.Fatalf("expected no servers from comments-only config, got %+v", servers)
	}
}

func TestRuntimeSessionSourceTypeForHermesAgent(t *testing.T) {
	if got := runtimeSessionSourceTypeForAgent("Hermes"); got != hermesSourceType {
		t.Fatalf("expected source type %q, got %q", hermesSourceType, got)
	}
	if got := runtimeSessionSourceTypeForAgent("hermes"); got != hermesSourceType {
		t.Fatalf("expected source type %q, got %q", hermesSourceType, got)
	}
}

func TestDiscoverAgentsFindsHermesYAMLConfig(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, ".hermes", "config.yaml")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create hermes dir: %v", err)
	}
	if err := os.WriteFile(configPath, []byte(hermesSampleConfig), 0644); err != nil {
		t.Fatalf("failed to write hermes config: %v", err)
	}

	discovered, err := discoverAgents(io.Discard, false)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	for _, agent := range discovered {
		if agent.Name != hermesAgentName {
			continue
		}
		if agent.ConfigPath != configPath {
			t.Fatalf("expected hermes config path %q, got %q", configPath, agent.ConfigPath)
		}
		if _, ok := agent.MCPServers["github"]; !ok {
			t.Fatalf("expected discovered hermes mcp servers to include github, got %+v", agent.MCPServers)
		}
		return
	}
	t.Fatalf("expected Hermes to be discovered, got %#v", discovered)
}

func TestDiscoverAgentsFindsInstalledHermesWithoutConfig(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	if err := os.MkdirAll(filepath.Join(home, ".hermes", "sessions"), 0755); err != nil {
		t.Fatalf("failed to create hermes install marker: %v", err)
	}

	discovered, err := discoverAgents(io.Discard, false)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for _, agent := range discovered {
		if agent.Name != hermesAgentName {
			continue
		}
		wantPath := filepath.Join(home, hermesBootstrapConfigPath)
		if agent.ConfigPath != wantPath {
			t.Fatalf("expected synthesized Hermes config path %q, got %q", wantPath, agent.ConfigPath)
		}
		if len(agent.MCPServers) != 0 {
			t.Fatalf("expected empty MCP server set for unconfigured Hermes, got %+v", agent.MCPServers)
		}
		return
	}
	t.Fatalf("expected Hermes to be discovered from install markers, got %#v", discovered)
}

func TestBuildManagedMCPEnrollmentPlan_HermesAddsPreloopServer(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(configPath, []byte(hermesSampleConfig), 0644); err != nil {
		t.Fatalf("failed to write hermes config: %v", err)
	}

	plan, err := buildManagedMCPEnrollmentPlan(AgentConfig{
		Name:       hermesAgentName,
		ConfigPath: configPath,
	}, "https://preloop.example", "hermes-durable-token")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	servers, ok := plan.ManagedDocument["mcp_servers"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected mcp_servers map in managed Hermes config, got %#v", plan.ManagedDocument)
	}
	if _, ok := servers["github"]; !ok {
		t.Fatalf("expected existing github server to be preserved, got %#v", servers)
	}
	preloop, ok := servers["preloop"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected preloop server entry, got %#v", servers)
	}
	if preloop["url"] != "https://preloop.example/mcp/v1" {
		t.Fatalf("unexpected Preloop URL: %#v", preloop)
	}
	headers, ok := preloop["headers"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected headers in preloop server, got %#v", preloop)
	}
	if headers["Authorization"] != "Bearer hermes-durable-token" {
		t.Fatalf("unexpected Authorization header: %+v", headers)
	}
	if enabled, _ := preloop["enabled"].(bool); !enabled {
		t.Fatalf("expected preloop server to be enabled, got %#v", preloop)
	}
	preloopConfig, ok := plan.ManagedDocument["preloop"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected preloop config block, got %#v", plan.ManagedDocument)
	}
	control, ok := preloopConfig["control"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected control config block, got %#v", preloopConfig)
	}
	if control["control_ws_url"] != "wss://preloop.example/api/v1/agents/control/ws" {
		t.Fatalf("unexpected Hermes control WebSocket URL: %#v", control)
	}
	if control["bearer_token"] != "hermes-durable-token" {
		t.Fatalf("unexpected Hermes control bearer token: %#v", control)
	}
	if control["adapter_package"] != "preloop-hermes-plugin" ||
		control["runtime"] != hermesSourceType {
		t.Fatalf("unexpected Hermes control adapter metadata: %#v", control)
	}

	sanitizedServers, ok := plan.SanitizedManaged["mcp_servers"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected sanitized mcp_servers map, got %#v", plan.SanitizedManaged)
	}
	sanitizedPreloop := sanitizedServers["preloop"].(map[string]interface{})
	sanitizedHeaders := sanitizedPreloop["headers"].(map[string]interface{})
	if sanitizedHeaders["Authorization"] != "<redacted>" {
		t.Fatalf("expected sanitized auth header, got %+v", sanitizedHeaders)
	}

	sanitizedExisting := sanitizedServers["remote_api"].(map[string]interface{})
	existingHeaders := sanitizedExisting["headers"].(map[string]interface{})
	if existingHeaders["Authorization"] != "<redacted>" {
		t.Fatalf("expected upstream Authorization header to be redacted, got %+v", existingHeaders)
	}
	sanitizedPreloopConfig := plan.SanitizedManaged["preloop"].(map[string]interface{})
	sanitizedControl := sanitizedPreloopConfig["control"].(map[string]interface{})
	if sanitizedControl["bearer_token"] != "<redacted>" {
		t.Fatalf("expected sanitized control bearer token, got %+v", sanitizedControl)
	}
}

func TestBuildManagedMCPEnrollmentPlan_HermesAllowsMissingConfig(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, ".hermes", "config.yaml")

	plan, err := buildManagedMCPEnrollmentPlan(AgentConfig{
		Name:       hermesAgentName,
		ConfigPath: configPath,
	}, "https://preloop.example", "hermes-durable-token")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	servers := plan.ManagedDocument["mcp_servers"].(map[string]interface{})
	preloop := servers["preloop"].(map[string]interface{})
	if preloop["url"] != "https://preloop.example/mcp/v1" {
		t.Fatalf("unexpected Preloop URL: %#v", preloop)
	}
	headers := preloop["headers"].(map[string]interface{})
	if headers["Authorization"] != "Bearer hermes-durable-token" {
		t.Fatalf("unexpected Authorization header: %+v", headers)
	}
}

func TestWriteHermesAgentConfigDocument_RoundTripsThroughYAML(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "nested", "config.yaml")

	doc := map[string]interface{}{
		"mcp_servers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"url": "https://preloop.example/mcp/v1",
				"headers": map[string]interface{}{
					"Authorization": "Bearer round-trip-token",
				},
			},
		},
	}

	if err := writeHermesAgentConfigDocument(configPath, doc); err != nil {
		t.Fatalf("failed to write hermes config: %v", err)
	}

	written, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatalf("failed to read back hermes config: %v", err)
	}
	if !strings.Contains(string(written), "mcp_servers:") {
		t.Fatalf("expected mcp_servers key in written YAML, got %q", string(written))
	}

	reloaded, err := loadHermesAgentConfigDocument(configPath)
	if err != nil {
		t.Fatalf("failed to reload hermes config: %v", err)
	}
	servers, ok := reloaded["mcp_servers"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected mcp_servers to round-trip, got %#v", reloaded)
	}
	preloop, ok := servers["preloop"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected preloop server to round-trip, got %#v", servers)
	}
	if preloop["url"] != "https://preloop.example/mcp/v1" {
		t.Fatalf("unexpected reloaded URL: %#v", preloop)
	}
}

func TestHermesAdapterValidateManagedConfig_Passes(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: hermesAgentName})
	doc := map[string]interface{}{
		"mcp_servers": map[string]interface{}{
			"preloop": adapter.BuildManagedServer("https://preloop.example", "hermes-token"),
		},
	}
	result := adapter.ValidateManagedConfig(doc, "https://preloop.example")
	if passed, _ := result["validation_passed"].(bool); !passed {
		t.Fatalf("expected hermes adapter validation to pass, got %#v", result)
	}
	if result["adapter_key"] != hermesSourceType {
		t.Fatalf("expected adapter key %q, got %q", hermesSourceType, result["adapter_key"])
	}
	if result["control_channel_configured"] != false {
		t.Fatalf("expected missing control config to be reported separately, got %#v", result)
	}
	if result["control_config_written"] != false {
		t.Fatalf("expected missing control config to remain explicit, got %#v", result)
	}
}

func TestHermesAdapterValidateManagedConfig_PassesWithControlChannel(t *testing.T) {
	agent := AgentConfig{
		Name:       hermesAgentName,
		ConfigPath: "/tmp/hermes/config.yaml",
	}
	adapter := managedMCPAdapterForAgent(agent)
	doc := map[string]interface{}{
		"mcp_servers": map[string]interface{}{
			"preloop": adapter.BuildManagedServer("https://preloop.example", "hermes-token"),
		},
	}
	applyAgentControlConfigToDocument(
		agent,
		doc,
		buildManagedAgentControlConfig(agent, "https://preloop.example", "hermes-token", nil, nil, nil),
	)

	result := adapter.ValidateManagedConfig(doc, "https://preloop.example")
	if passed, _ := result["validation_passed"].(bool); !passed {
		t.Fatalf("expected hermes adapter validation to pass, got %#v", result)
	}
	if result["control_config_written"] != true ||
		result["control_ws_url_ok"] != true ||
		result["control_bearer_token_ok"] != true ||
		result["control_adapter_package_ok"] != true {
		t.Fatalf("expected Hermes control config validation to pass, got %#v", result)
	}
	if result["control_plugin_installed"] != false ||
		result["control_plugin_verified"] != false ||
		result["control_channel_configured"] != false {
		t.Fatalf("expected Hermes runtime plugin to remain unverified, got %#v", result)
	}
}

func TestInstallAgentControlRuntimePluginInstallsAndVerifiesHermes(t *testing.T) {
	skipNoShebangOnWindows(t, "Hermes runtime-plugin install+verify flow")
	dir := t.TempDir()
	pluginsRoot := filepath.Join(dir, "runtime-plugins")
	sourcePath := filepath.Join(pluginsRoot, "hermes-preloop")
	if err := os.MkdirAll(sourcePath, 0755); err != nil {
		t.Fatalf("failed to create Hermes plugin source dir: %v", err)
	}
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", pluginsRoot)

	binDir := filepath.Join(dir, "bin")
	if err := os.MkdirAll(binDir, 0755); err != nil {
		t.Fatalf("failed to create bin dir: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(binDir, "hermes"),
		[]byte("#!/bin/sh\n[ \"$1\" = plugins ] && [ \"$2\" = install ] && [ -d \"$3\" ]\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake Hermes installer: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(binDir, "preloop-hermes-plugin"),
		[]byte("#!/bin/sh\n[ \"$1\" = verify ] && [ \"$2\" = --config ]\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake Hermes verifier: %v", err)
	}
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	result := installAgentControlRuntimePlugin(
		AgentConfig{Name: hermesAgentName, ConfigPath: filepath.Join(dir, "config.yaml")},
		io.Discard,
	)
	if result["control_plugin_install_status"] != "installed_and_verified" ||
		result["control_plugin_installed"] != true ||
		result["control_plugin_verified"] != true {
		t.Fatalf("expected Hermes install and verify success, got %#v", result)
	}
}

func TestInstallAgentControlRuntimePluginFindsHermesInUserLocalBin(t *testing.T) {
	skipNoShebangOnWindows(t, "Hermes discovery in ~/.local/bin")
	dir := t.TempDir()
	home := filepath.Join(dir, "home")
	localBin := filepath.Join(home, ".local", "bin")
	pluginsRoot := filepath.Join(dir, "runtime-plugins")
	sourcePath := filepath.Join(pluginsRoot, "hermes-preloop")
	if err := os.MkdirAll(sourcePath, 0755); err != nil {
		t.Fatalf("failed to create Hermes plugin source dir: %v", err)
	}
	if err := os.MkdirAll(localBin, 0755); err != nil {
		t.Fatalf("failed to create local bin dir: %v", err)
	}
	testenv.SetHome(t, home)
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", pluginsRoot)
	t.Setenv("PATH", filepath.Join(dir, "empty-path"))

	if err := os.WriteFile(
		filepath.Join(localBin, "hermes"),
		[]byte("#!/bin/sh\n[ \"$1\" = plugins ] && [ \"$2\" = install ] && [ -d \"$3\" ]\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake Hermes installer: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(localBin, "preloop-hermes-plugin"),
		[]byte("#!/bin/sh\n[ \"$1\" = verify ] && [ \"$2\" = --config ]\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake Hermes verifier: %v", err)
	}

	result := installAgentControlRuntimePlugin(
		AgentConfig{Name: hermesAgentName, ConfigPath: filepath.Join(home, ".hermes", "config.yaml")},
		io.Discard,
	)
	if result["control_plugin_install_status"] != "installed_and_verified" ||
		result["control_plugin_installer"] != "hermes" ||
		result["control_plugin_installed"] != true ||
		result["control_plugin_verified"] != true {
		t.Fatalf("expected Hermes install from ~/.local/bin to verify, got %#v", result)
	}
}

func TestInstallAgentControlRuntimePluginFallsBackToPipWhenHermesRejectsIdentifier(t *testing.T) {
	skipNoShebangOnWindows(t, "Hermes pip fallback path")
	dir := t.TempDir()
	// Point the plugin source root at an empty directory so the install target
	// falls back to the PyPI package name, matching an end-user install.
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", filepath.Join(dir, "runtime-plugins"))
	testenv.SetHome(t, filepath.Join(dir, "home"))

	binDir := filepath.Join(dir, "bin")
	if err := os.MkdirAll(binDir, 0755); err != nil {
		t.Fatalf("failed to create bin dir: %v", err)
	}
	// Real Hermes rejects PyPI package names: reproduce its marketplace error.
	if err := os.WriteFile(
		filepath.Join(binDir, "hermes"),
		[]byte("#!/bin/sh\necho \"Error: Invalid plugin identifier: '$3'. Use a Git URL or owner/repo shorthand\" >&2\nexit 1\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake Hermes installer: %v", err)
	}
	// Fake python3 next to the hermes executable records the pip invocation.
	// The interpreter next to hermes only counts as Hermes' environment when
	// the parent directory is a real virtualenv, so mark it with pyvenv.cfg.
	if err := os.WriteFile(filepath.Join(dir, "pyvenv.cfg"), []byte("home = /usr\n"), 0644); err != nil {
		t.Fatalf("failed to write pyvenv.cfg: %v", err)
	}
	pipArgsFile := filepath.Join(dir, "pip-args.txt")
	if err := os.WriteFile(
		filepath.Join(binDir, "python3"),
		[]byte("#!/bin/sh\nprintf '%s' \"$*\" > "+pipArgsFile+"\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake python3: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(binDir, "preloop-hermes-plugin"),
		[]byte("#!/bin/sh\n[ \"$1\" = verify ] && [ \"$2\" = --config ]\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake Hermes verifier: %v", err)
	}
	t.Setenv("PATH", binDir)

	result := installAgentControlRuntimePlugin(
		AgentConfig{Name: hermesAgentName, ConfigPath: filepath.Join(dir, "config.yaml")},
		io.Discard,
	)
	if result["control_plugin_install_status"] != "installed_and_verified" ||
		result["control_plugin_installed"] != true ||
		result["control_plugin_verified"] != true {
		t.Fatalf("expected pip fallback install and verify success, got %#v", result)
	}
	if result["control_plugin_installer"] != "pip" {
		t.Fatalf("expected installer to be recorded as pip, got %#v", result)
	}
	marketplaceError, _ := result["control_plugin_marketplace_install_error"].(string)
	if !strings.Contains(marketplaceError, "Invalid plugin identifier") {
		t.Fatalf("expected marketplace error to be preserved, got %#v", result)
	}
	pipArgs, err := os.ReadFile(pipArgsFile)
	if err != nil {
		t.Fatalf("expected pip fallback to invoke python3: %v", err)
	}
	if string(pipArgs) != "-m pip install --upgrade preloop-hermes-plugin" {
		t.Fatalf("unexpected pip invocation: %q", string(pipArgs))
	}
}

func TestClassifyRuntimePluginInstallFailureHermesInvalidIdentifier(t *testing.T) {
	dir := t.TempDir()
	home := filepath.Join(dir, "home")
	testenv.SetHome(t, home)
	t.Setenv("PATH", filepath.Join(dir, "empty-path"))

	status, remediation := classifyRuntimePluginInstallFailure(
		"hermes",
		"Agent Control plugin install failed: Error: Invalid plugin identifier: 'preloop-hermes-plugin'. Use a Git URL or owner/repo shorthand",
	)
	if status != "runtime_marketplace_rejected_identifier" {
		t.Fatalf("unexpected status: %q", status)
	}
	// The remediation must be copy-pasteable with the full venv pip path —
	// never a bare `pip install`, which hits the system interpreter and fails
	// PEP 668 on stock Debian.
	expectedPip := filepath.Join(home, ".hermes", "hermes-agent", "venv", "bin", "pip")
	if !strings.Contains(remediation, expectedPip+" install --upgrade preloop-hermes-plugin") {
		t.Fatalf("expected venv pip remediation with full path %q, got %q", expectedPip, remediation)
	}
	if !strings.Contains(remediation, "hermes plugins enable preloop --allow-tool-override") {
		t.Fatalf("expected plugin enable step in remediation, got %q", remediation)
	}
}

func TestHermesInstallDirFromVersionOutput(t *testing.T) {
	output := "Hermes Agent 1.2.3\n  Install directory: /usr/local/lib/hermes-agent\nPython 3.11.2\n"
	if got := hermesInstallDirFromVersionOutput(output); got != "/usr/local/lib/hermes-agent" {
		t.Fatalf("expected install dir from version banner, got %q", got)
	}
	if got := hermesInstallDirFromVersionOutput("Hermes Agent 1.2.3\n"); got != "" {
		t.Fatalf("expected empty install dir when banner lacks the line, got %q", got)
	}
	if got := hermesInstallDirFromVersionOutput(""); got != "" {
		t.Fatalf("expected empty install dir for empty output, got %q", got)
	}
}

func TestResolveHermesPipPythonDiscoversVenvFromVersionOutput(t *testing.T) {
	skipNoShebangOnWindows(t, "Hermes venv discovery from `hermes --version` output")
	dir := t.TempDir()
	home := filepath.Join(dir, "home")
	testenv.SetHome(t, home)

	// Simulate a stock Debian layout: hermes on PATH reports its install
	// directory, and the venv python lives under <install-dir>/venv/bin.
	installDir := filepath.Join(dir, "usr", "local", "lib", "hermes-agent")
	venvBin := filepath.Join(installDir, "venv", "bin")
	if err := os.MkdirAll(venvBin, 0755); err != nil {
		t.Fatalf("failed to create venv bin dir: %v", err)
	}
	venvPython := filepath.Join(venvBin, "python3")
	if err := os.WriteFile(venvPython, []byte("#!/bin/sh\n"), 0755); err != nil {
		t.Fatalf("failed to write fake venv python: %v", err)
	}

	binDir := filepath.Join(dir, "bin")
	if err := os.MkdirAll(binDir, 0755); err != nil {
		t.Fatalf("failed to create bin dir: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(binDir, "hermes"),
		[]byte("#!/bin/sh\necho \"Hermes Agent 1.2.3\"\necho \"Install directory: "+installDir+"\"\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake hermes: %v", err)
	}
	t.Setenv("PATH", binDir)

	resolved, err := resolveHermesPipPython()
	if err != nil {
		t.Fatalf("unexpected python resolution error: %v", err)
	}
	if resolved != venvPython {
		t.Fatalf("expected venv python %q from version banner, got %q", venvPython, resolved)
	}
}

func TestResolveHermesPipPythonNeverFallsBackToSystemPython(t *testing.T) {
	skipNoShebangOnWindows(t, "Hermes pip interpreter refusing system python")
	dir := t.TempDir()
	home := filepath.Join(dir, "home")
	testenv.SetHome(t, home)

	// A system python3 is on PATH, but no Hermes venv exists anywhere. The
	// resolver must fail with a copy-pasteable venv pip hint instead of
	// returning the system interpreter (PEP 668 would reject the install).
	binDir := filepath.Join(dir, "bin")
	if err := os.MkdirAll(binDir, 0755); err != nil {
		t.Fatalf("failed to create bin dir: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(binDir, "python3"), []byte("#!/bin/sh\n"), 0755,
	); err != nil {
		t.Fatalf("failed to write fake system python: %v", err)
	}
	installDir := filepath.Join(dir, "usr", "local", "lib", "hermes-agent")
	if err := os.WriteFile(
		filepath.Join(binDir, "hermes"),
		[]byte("#!/bin/sh\necho \"Install directory: "+installDir+"\"\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake hermes: %v", err)
	}
	t.Setenv("PATH", binDir)

	_, err := resolveHermesPipPython()
	if err == nil {
		t.Fatalf("expected resolution to fail rather than use the system python")
	}
	expectedPip := filepath.Join(installDir, "venv", "bin", "pip")
	if !strings.Contains(err.Error(), expectedPip+" install --upgrade preloop-hermes-plugin") {
		t.Fatalf("expected error to carry full venv pip path %q, got %q", expectedPip, err.Error())
	}
}

func TestRunAgentsInstallPluginHermesDryRunPrintsVenvPipCommand(t *testing.T) {
	skipManagedLauncherOnWindows(t, "Hermes install-plugin dry-run venv pip command")
	dir := t.TempDir()
	home := filepath.Join(dir, "home")
	testenv.SetHome(t, home)
	t.Setenv("PATH", filepath.Join(dir, "empty-path"))
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", filepath.Join(dir, "runtime-plugins"))

	cmd := agentsInstallPluginCmd
	if err := cmd.Flags().Set("dry-run", "true"); err != nil {
		t.Fatalf("failed to set dry-run flag: %v", err)
	}
	t.Cleanup(func() {
		_ = cmd.Flags().Set("dry-run", "false")
		cmd.SetOut(nil)
	})
	buf := &bytes.Buffer{}
	cmd.SetOut(buf)

	if err := runAgentsInstallPlugin(cmd, []string{"hermes"}); err != nil {
		t.Fatalf("dry run failed: %v", err)
	}
	printed := strings.TrimSpace(buf.String())
	// The dry-run must print the actual working command: Hermes'
	// `plugins install` rejects PyPI names, so the command is a venv pip
	// install plus the plugin enable.
	expectedPip := filepath.Join(home, ".hermes", "hermes-agent", "venv", "bin", "pip")
	expected := expectedPip + " install --upgrade preloop-hermes-plugin && hermes plugins enable preloop --allow-tool-override"
	if printed != expected {
		t.Fatalf("unexpected dry-run command:\n  got:  %q\n  want: %q", printed, expected)
	}
	if strings.Contains(printed, "hermes plugins install") {
		t.Fatalf("dry-run must not print the broken marketplace command, got %q", printed)
	}
}

func TestVerifyAgentControlRuntimePluginDetectsHermesEntryPointInstall(t *testing.T) {
	skipNoShebangOnWindows(t, "Hermes entry-point plugin detection via `hermes plugins list`")
	dir := t.TempDir()
	home := filepath.Join(dir, "home")
	testenv.SetHome(t, home)

	// No `preloop-hermes-plugin` console script anywhere: the plugin was
	// installed via `venv/bin/pip install preloop-hermes-plugin` and is only
	// visible through Hermes' own plugin registry.
	binDir := filepath.Join(dir, "bin")
	if err := os.MkdirAll(binDir, 0755); err != nil {
		t.Fatalf("failed to create bin dir: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(binDir, "hermes"),
		[]byte("#!/bin/sh\nif [ \"$1\" = plugins ] && [ \"$2\" = list ]; then\n"+
			"  echo \"preloop    enabled    Preloop Agent Control\"\n  exit 0\nfi\n"+
			"echo \"Hermes Agent 1.2.3\"\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake hermes: %v", err)
	}
	t.Setenv("PATH", binDir)

	result := verifyAgentControlRuntimePlugin(AgentConfig{
		Name:       hermesAgentName,
		ConfigPath: filepath.Join(home, ".hermes", "config.yaml"),
	})
	if result["control_plugin_installed"] != true {
		t.Fatalf("expected entry-point plugin to be detected as installed, got %#v", result)
	}
	if result["control_plugin_verified"] != true {
		t.Fatalf("expected enabled entry-point plugin to verify, got %#v", result)
	}
	if result["control_plugin_verification"] != "hermes_entry_point_plugin_enabled" {
		t.Fatalf("unexpected verification detail: %#v", result)
	}
}

func TestVerifyAgentControlRuntimePluginReportsDisabledHermesEntryPointInstall(t *testing.T) {
	skipNoShebangOnWindows(t, "Hermes disabled entry-point plugin detection")
	dir := t.TempDir()
	home := filepath.Join(dir, "home")
	testenv.SetHome(t, home)

	binDir := filepath.Join(dir, "bin")
	if err := os.MkdirAll(binDir, 0755); err != nil {
		t.Fatalf("failed to create bin dir: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(binDir, "hermes"),
		[]byte("#!/bin/sh\nif [ \"$1\" = plugins ] && [ \"$2\" = list ]; then\n"+
			"  echo \"preloop    disabled    Preloop Agent Control\"\n  exit 0\nfi\n"+
			"echo \"Hermes Agent 1.2.3\"\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake hermes: %v", err)
	}
	t.Setenv("PATH", binDir)

	result := verifyAgentControlRuntimePlugin(AgentConfig{
		Name:       hermesAgentName,
		ConfigPath: filepath.Join(home, ".hermes", "config.yaml"),
	})
	if result["control_plugin_installed"] != true {
		t.Fatalf("expected disabled entry-point plugin to still count as installed, got %#v", result)
	}
	if result["control_plugin_verified"] != false {
		t.Fatalf("expected disabled plugin to fail verification, got %#v", result)
	}
	remediation, _ := result["control_plugin_install_remediation"].(string)
	if !strings.Contains(remediation, "hermes plugins enable preloop --allow-tool-override") {
		t.Fatalf("expected enable remediation for disabled plugin, got %#v", result)
	}
}

func TestVerifyAgentControlRuntimePluginFindsHermesVenvConsoleScript(t *testing.T) {
	skipNoShebangOnWindows(t, "Hermes venv console-script verification")
	dir := t.TempDir()
	home := filepath.Join(dir, "home")
	testenv.SetHome(t, home)

	// The plugin's console script lives only inside Hermes' venv bin
	// (reported via `hermes --version`), not on PATH.
	installDir := filepath.Join(dir, "opt", "hermes-agent")
	venvBin := filepath.Join(installDir, "venv", "bin")
	if err := os.MkdirAll(venvBin, 0755); err != nil {
		t.Fatalf("failed to create venv bin dir: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(venvBin, "preloop-hermes-plugin"),
		[]byte("#!/bin/sh\n[ \"$1\" = verify ] && [ \"$2\" = --config ]\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake venv verifier: %v", err)
	}
	binDir := filepath.Join(dir, "bin")
	if err := os.MkdirAll(binDir, 0755); err != nil {
		t.Fatalf("failed to create bin dir: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(binDir, "hermes"),
		[]byte("#!/bin/sh\necho \"Install directory: "+installDir+"\"\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake hermes: %v", err)
	}
	t.Setenv("PATH", binDir)

	result := verifyAgentControlRuntimePlugin(AgentConfig{
		Name:       hermesAgentName,
		ConfigPath: filepath.Join(home, ".hermes", "config.yaml"),
	})
	if result["control_plugin_installed"] != true ||
		result["control_plugin_verified"] != true ||
		result["control_plugin_verification"] != "verified" {
		t.Fatalf("expected venv console script to verify, got %#v", result)
	}
}

func TestResolveHermesPipPythonPrefersHermesEnvironment(t *testing.T) {
	skipManagedLauncherOnWindows(t, "preferring the Hermes-bundled Python interpreter")
	dir := t.TempDir()
	home := filepath.Join(dir, "home")
	venvBin := filepath.Join(home, ".hermes", "hermes-agent", "venv", "bin")
	if err := os.MkdirAll(venvBin, 0755); err != nil {
		t.Fatalf("failed to create Hermes venv bin dir: %v", err)
	}
	venvPython := filepath.Join(venvBin, "python3")
	if err := os.WriteFile(venvPython, []byte("#!/bin/sh\n"), 0755); err != nil {
		t.Fatalf("failed to write fake venv python: %v", err)
	}
	testenv.SetHome(t, home)
	t.Setenv("PATH", filepath.Join(dir, "empty-path"))

	resolved, err := resolveHermesPipPython()
	if err != nil {
		t.Fatalf("unexpected python resolution error: %v", err)
	}
	if resolved != venvPython {
		t.Fatalf("expected Hermes venv python %q, got %q", venvPython, resolved)
	}
}

func TestRuntimeExecutableSearchDescriptionIncludesHermesUserLocalBin(t *testing.T) {
	home := filepath.Join(t.TempDir(), "home")
	testenv.SetHome(t, home)

	description := runtimeExecutableSearchDescription("hermes")
	if !strings.Contains(description, "PATH") {
		t.Fatalf("expected PATH in search description, got %q", description)
	}
	if !strings.Contains(description, filepath.Join(home, ".local", "bin", "hermes")) {
		t.Fatalf("expected ~/.local/bin/hermes in search description, got %q", description)
	}
}

func TestAgentControlPluginInstallCommandFallsBackToHermesPackage(t *testing.T) {
	pluginsRoot := t.TempDir()
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", pluginsRoot)

	command, args, err := agentControlPluginInstallCommand(hermesAgentName)
	if err != nil {
		t.Fatalf("unexpected install command error: %v", err)
	}
	if command != "hermes" {
		t.Fatalf("expected hermes installer, got %q", command)
	}
	if len(args) != 3 ||
		args[0] != "plugins" ||
		args[1] != "install" ||
		args[2] != "preloop-hermes-plugin" {
		t.Fatalf("unexpected marketplace install args: %#v", args)
	}
}

func TestHermesAdapterValidateManagedConfig_FailsWithoutPreloopEntry(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: hermesAgentName})
	doc := map[string]interface{}{
		"mcp_servers": map[string]interface{}{
			"github": map[string]interface{}{
				"url": "https://example.com/mcp",
			},
		},
	}
	result := adapter.ValidateManagedConfig(doc, "https://preloop.example")
	if passed, _ := result["validation_passed"].(bool); passed {
		t.Fatalf("expected validation to fail without Preloop entry, got %#v", result)
	}
}

func TestSupportsManagedGateway_IncludesHermes(t *testing.T) {
	if !supportsManagedGateway(AgentConfig{Name: hermesAgentName}) {
		t.Fatalf("expected Hermes to support the managed gateway")
	}
	if !supportsManagedGateway(AgentConfig{Name: "hermes"}) {
		t.Fatalf("expected lowercase hermes to support the managed gateway")
	}
}

func TestApplyHermesManagedGateway_RewritesModelBlock(t *testing.T) {
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{
			"model": map[string]interface{}{
				"default":  "gpt-5.4",
				"provider": "openai-codex",
				"base_url": "https://chatgpt.com/backend-api/codex",
			},
			"mcp_servers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"url": "https://preloop.example/mcp/v1",
					"headers": map[string]interface{}{
						"Authorization": "Bearer hermes-durable-token",
					},
				},
			},
		},
	}

	plan, err := applyHermesManagedGateway(
		plan,
		"https://preloop.example",
		"hermes-durable-token",
		"openai/gpt-5.4",
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}

	model, ok := plan.ManagedDocument["model"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected model mapping in managed doc, got %#v", plan.ManagedDocument["model"])
	}
	if model["provider"] != hermesGatewayProviderName {
		t.Fatalf("expected provider %q, got %#v", hermesGatewayProviderName, model["provider"])
	}
	if model["base_url"] != "https://preloop.example/openai/v1" {
		t.Fatalf("unexpected hermes gateway base_url: %#v", model["base_url"])
	}
	if model["api_key"] != "hermes-durable-token" {
		t.Fatalf("unexpected hermes gateway api_key: %#v", model["api_key"])
	}
	if model["default"] != "openai/gpt-5.4" {
		t.Fatalf("unexpected hermes gateway model default: %#v", model["default"])
	}
	if _, exists := model["model"]; exists {
		t.Fatalf("expected stale `model` shorthand to be cleared")
	}
	if plan.ManagedModelAlias != "openai/gpt-5.4" {
		t.Fatalf("unexpected ManagedModelAlias: %q", plan.ManagedModelAlias)
	}
	if plan.ManagedProviderName != "preloop" {
		t.Fatalf("unexpected ManagedProviderName: %q", plan.ManagedProviderName)
	}

	servers := plan.ManagedDocument["mcp_servers"].(map[string]interface{})
	preloop := servers["preloop"].(map[string]interface{})
	if preloop["url"] != "https://preloop.example/mcp/v1" {
		t.Fatalf("expected pre-existing MCP entry to be preserved, got %#v", preloop)
	}
}

func TestParseHermesManagedGatewayUpstreamResolvesProviderSpecificEnvKey(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	t.Setenv("DEEPSEEK_API_KEY", "")
	configPath := filepath.Join(home, ".hermes", "config.yaml")
	if err := os.MkdirAll(filepath.Dir(configPath), 0o755); err != nil {
		t.Fatalf("failed to create Hermes config dir: %v", err)
	}
	if err := os.WriteFile(
		configPath,
		[]byte(`model:
  default: deepseek-v4-pro
  provider: deepseek
  base_url: https://api.deepseek.com/v1
`),
		0o600,
	); err != nil {
		t.Fatalf("failed to write Hermes config: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(home, hermesEnvFile),
		[]byte("DEEPSEEK_API_KEY=deepseek-secret\n"),
		0o600,
	); err != nil {
		t.Fatalf("failed to write Hermes env file: %v", err)
	}

	upstream, err := parseHermesManagedGatewayUpstream(AgentConfig{
		Name:       hermesAgentName,
		ConfigPath: configPath,
	})
	if err != nil {
		t.Fatalf("unexpected Hermes upstream parse error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected Hermes upstream to be resolved")
	}
	if !upstream.CanRouteThroughGateway() {
		t.Fatalf("expected deepseek upstream to be gateway-routable, got %#v", upstream)
	}
	if upstream.ProviderName != "deepseek" {
		t.Fatalf("expected provider deepseek, got %q", upstream.ProviderName)
	}
	if upstream.ModelIdentifier != "deepseek-v4-pro" {
		t.Fatalf("expected model id deepseek-v4-pro, got %q", upstream.ModelIdentifier)
	}
	if upstream.APIKey != "deepseek-secret" {
		t.Fatalf("expected provider-specific env key to resolve, got %q", upstream.APIKey)
	}
	if upstream.ManagedModelAlias != "deepseek/deepseek-v4-pro" {
		t.Fatalf("unexpected managed model alias %q", upstream.ManagedModelAlias)
	}
}

func TestApplyHermesManagedGateway_CreatesModelBlockWhenMissing(t *testing.T) {
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{},
	}

	plan, err := applyHermesManagedGateway(
		plan,
		"https://preloop.example/",
		"hermes-token",
		"openai/gpt-5.4",
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}

	model, ok := plan.ManagedDocument["model"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected applyHermesManagedGateway to materialise the model block, got %#v", plan.ManagedDocument)
	}
	if model["base_url"] != "https://preloop.example/openai/v1" {
		t.Fatalf("expected trailing slash to be stripped from base URL, got %#v", model["base_url"])
	}
}

func TestHermesAdapterValidateManagedConfig_PassesWithGatewayConfigured(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: hermesAgentName})
	doc := map[string]interface{}{
		"mcp_servers": map[string]interface{}{
			"preloop": adapter.BuildManagedServer("https://preloop.example", "hermes-token"),
		},
		"model": map[string]interface{}{
			"provider": hermesGatewayProviderName,
			"base_url": "https://preloop.example/openai/v1",
			"api_key":  "hermes-token",
			"default":  "openai/gpt-5.4",
		},
	}
	result := adapter.ValidateManagedConfig(doc, "https://preloop.example")
	if passed, _ := result["validation_passed"].(bool); !passed {
		t.Fatalf("expected gateway-configured validation to pass, got %#v", result)
	}
	if result["gateway_provider_ok"] != true {
		t.Fatalf("expected gateway_provider_ok=true, got %#v", result)
	}
	if result["gateway_base_url_ok"] != true {
		t.Fatalf("expected gateway_base_url_ok=true, got %#v", result)
	}
	if result["gateway_model_alias"] != "openai/gpt-5.4" {
		t.Fatalf("unexpected gateway alias surfaced by validation: %#v", result["gateway_model_alias"])
	}
}

func TestHermesAdapterValidateManagedConfig_FailsWhenGatewayBaseURLWrong(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: hermesAgentName})
	doc := map[string]interface{}{
		"mcp_servers": map[string]interface{}{
			"preloop": adapter.BuildManagedServer("https://preloop.example", "hermes-token"),
		},
		"model": map[string]interface{}{
			"provider": hermesGatewayProviderName,
			"base_url": "https://chatgpt.com/backend-api/codex",
			"default":  "openai/gpt-5.4",
		},
	}
	result := adapter.ValidateManagedConfig(doc, "https://preloop.example")
	if passed, _ := result["validation_passed"].(bool); passed {
		t.Fatalf("expected validation to fail when gateway base_url is wrong, got %#v", result)
	}
}

func TestExtractHermesModelSelection_StringShorthand(t *testing.T) {
	modelRef, providerHint, baseURL := extractHermesModelSelection(map[string]interface{}{
		"model": "anthropic/claude-opus-4.6",
	})
	if modelRef != "anthropic/claude-opus-4.6" || providerHint != "" || baseURL != "" {
		t.Fatalf("unexpected shorthand parse: model=%q hint=%q baseURL=%q", modelRef, providerHint, baseURL)
	}
}

func TestExtractHermesModelSelection_StructuredMapping(t *testing.T) {
	modelRef, providerHint, baseURL := extractHermesModelSelection(map[string]interface{}{
		"model": map[string]interface{}{
			"default":  "gpt-5.4",
			"provider": "openai-codex",
			"base_url": "https://chatgpt.com/backend-api/codex",
		},
	})
	if modelRef != "gpt-5.4" || providerHint != "openai-codex" ||
		baseURL != "https://chatgpt.com/backend-api/codex" {
		t.Fatalf("unexpected structured parse: model=%q hint=%q baseURL=%q", modelRef, providerHint, baseURL)
	}
}

func TestSplitHermesModelRef_PrefersExplicitProviderHint(t *testing.T) {
	provider, model := splitHermesModelRef("anthropic/claude-opus-4.6", "openrouter")
	if provider != "openrouter" || model != "claude-opus-4.6" {
		t.Fatalf("expected explicit provider hint to win, got provider=%q model=%q", provider, model)
	}
}

func TestSplitHermesModelRef_DefaultsToOpenRouterWhenAuto(t *testing.T) {
	provider, model := splitHermesModelRef("hermes-3-llama-3.1-405b", "auto")
	if provider != "openrouter" || model != "hermes-3-llama-3.1-405b" {
		t.Fatalf("expected auto to default to openrouter, got provider=%q model=%q", provider, model)
	}
}

func TestNormalizeHermesManagedAlias_CollapsesCodexProvider(t *testing.T) {
	if got := normalizeHermesManagedAlias("gpt-5.4", "openai-codex", "gpt-5.4"); got != "openai/gpt-5.4" {
		t.Fatalf("expected openai/gpt-5.4, got %q", got)
	}
	if got := normalizeHermesManagedAlias("anthropic/claude-opus-4.6", "anthropic", "claude-opus-4.6"); got != "anthropic/claude-opus-4.6" {
		t.Fatalf("expected pre-existing slashed alias to round-trip, got %q", got)
	}
}

func TestParseHermesManagedGatewayUpstream_ImportsCodexOAuth(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, hermesBootstrapConfigPath)
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create hermes dir: %v", err)
	}
	configBody := `model:
  default: gpt-5.4
  provider: openai-codex
  base_url: https://chatgpt.com/backend-api/codex
`
	if err := os.WriteFile(configPath, []byte(configBody), 0644); err != nil {
		t.Fatalf("failed to seed hermes config: %v", err)
	}

	jwtPayload := base64.RawURLEncoding.EncodeToString([]byte(`{"exp":1893456000,"https://api.openai.com/auth":{"chatgpt_account_id":"acct-test"}}`))
	accessToken := "header." + jwtPayload + ".sig"
	authJSON := `{
        "version": 1,
        "providers": {
            "openai-codex": {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "` + accessToken + `",
                    "refresh_token": "refresh-token"
                }
            }
        },
        "active_provider": "openai-codex"
    }`
	authPath := filepath.Join(home, hermesAuthFile)
	if err := os.WriteFile(authPath, []byte(authJSON), 0600); err != nil {
		t.Fatalf("failed to seed hermes auth.json: %v", err)
	}

	upstream, err := parseHermesManagedGatewayUpstream(AgentConfig{
		Name:       hermesAgentName,
		ConfigPath: configPath,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected hermes upstream to resolve")
	}
	if upstream.CredentialType != "oauth_openai_codex" {
		t.Fatalf("expected oauth_openai_codex credentials, got %#v", upstream.CredentialType)
	}
	if upstream.ProviderName != "openai-codex" {
		t.Fatalf("expected openai-codex provider name, got %q", upstream.ProviderName)
	}
	if upstream.ManagedModelAlias != "openai/gpt-5.4" {
		t.Fatalf("expected gateway alias openai/gpt-5.4, got %q", upstream.ManagedModelAlias)
	}
	if upstream.ModelIdentifier != "gpt-5.4" {
		t.Fatalf("expected model identifier gpt-5.4, got %q", upstream.ModelIdentifier)
	}
	if upstream.APIEndpoint != "https://chatgpt.com/backend-api/codex" {
		t.Fatalf("unexpected API endpoint: %q", upstream.APIEndpoint)
	}
	if got := upstream.CredentialPayload["access"]; got != accessToken {
		t.Fatalf("expected access token in payload, got %#v", got)
	}
	if got := upstream.CredentialPayload["account_id"]; got != "acct-test" {
		t.Fatalf("expected account_id from JWT, got %#v", got)
	}
	if !upstream.CanRouteThroughGateway() {
		t.Fatalf("expected upstream to be routable, got %#v", upstream)
	}
}

func TestParseHermesManagedGatewayUpstream_ImportsCredentialPoolCodexOAuth(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, hermesBootstrapConfigPath)
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create hermes dir: %v", err)
	}
	configBody := `model:
  default: gpt-5.5
  provider: openai-codex
  base_url: https://chatgpt.com/backend-api/codex
`
	if err := os.WriteFile(configPath, []byte(configBody), 0644); err != nil {
		t.Fatalf("failed to seed hermes config: %v", err)
	}

	jwtPayload := base64.RawURLEncoding.EncodeToString([]byte(`{"exp":1893456000}`))
	accessToken := "header." + jwtPayload + ".sig"
	authJSON := `{
        "version": 2,
        "providers": {
            "openai-codex": {
                "auth_mode": "chatgpt",
                "tokens": {}
            }
        },
        "credential_pool": {
            "openai-codex": [
                {
                    "auth_type": "oauth",
                    "access_token": "` + accessToken + `",
                    "refresh_token": "refresh-token",
                    "last_refresh": "2026-06-07T22:00:00Z"
                }
            ]
        },
        "active_provider": "openai-codex"
    }`
	authPath := filepath.Join(home, hermesAuthFile)
	if err := os.WriteFile(authPath, []byte(authJSON), 0600); err != nil {
		t.Fatalf("failed to seed hermes auth.json: %v", err)
	}

	upstream, err := parseHermesManagedGatewayUpstream(AgentConfig{
		Name:       hermesAgentName,
		ConfigPath: configPath,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil || !upstream.CanRouteThroughGateway() {
		t.Fatalf("expected credential_pool OAuth to be routable, got %#v", upstream)
	}
	if upstream.CredentialType != "oauth_openai_codex" {
		t.Fatalf("expected oauth_openai_codex credentials, got %#v", upstream.CredentialType)
	}
	if got := upstream.CredentialPayload["access"]; got != accessToken {
		t.Fatalf("expected access token from credential_pool, got %#v", got)
	}
	if upstream.ManagedModelAlias != "openai/gpt-5.5" {
		t.Fatalf("expected gateway alias openai/gpt-5.5, got %q", upstream.ManagedModelAlias)
	}
}

func TestParseHermesManagedGatewayUpstream_ResolvesOpenAIKeyFromEnvFile(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	for _, key := range []string{"OPENAI_API_KEY", "OPENROUTER_API_KEY"} {
		old, present := os.LookupEnv(key)
		_ = os.Unsetenv(key)
		defer func(k, v string, p bool) {
			if p {
				_ = os.Setenv(k, v)
			} else {
				_ = os.Unsetenv(k)
			}
		}(key, old, present)
	}

	configPath := filepath.Join(home, hermesBootstrapConfigPath)
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create hermes dir: %v", err)
	}
	configBody := `model:
  default: gpt-5.4
  provider: openai
`
	if err := os.WriteFile(configPath, []byte(configBody), 0644); err != nil {
		t.Fatalf("failed to seed hermes config: %v", err)
	}

	envPath := filepath.Join(home, hermesEnvFile)
	if err := os.MkdirAll(filepath.Dir(envPath), 0755); err != nil {
		t.Fatalf("failed to create hermes dir: %v", err)
	}
	if err := os.WriteFile(envPath, []byte("OPENAI_API_KEY=sk-test-1234\n"), 0600); err != nil {
		t.Fatalf("failed to seed hermes .env: %v", err)
	}

	upstream, err := parseHermesManagedGatewayUpstream(AgentConfig{
		Name:       hermesAgentName,
		ConfigPath: configPath,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected upstream resolution from .env API key")
	}
	if upstream.APIKey != "sk-test-1234" {
		t.Fatalf("expected API key from .env, got %q", upstream.APIKey)
	}
	if upstream.ManagedModelAlias != "openai/gpt-5.4" {
		t.Fatalf("expected openai/gpt-5.4 alias, got %q", upstream.ManagedModelAlias)
	}
	if !upstream.CanRouteThroughGateway() {
		t.Fatalf("expected upstream to route through gateway, got %#v", upstream)
	}
}

func TestParseHermesManagedGatewayUpstream_SkipsAlreadyManagedConfig(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, hermesBootstrapConfigPath)
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create hermes dir: %v", err)
	}
	body := `model:
  default: preloop/openai/gpt-5.4
  provider: custom
  base_url: https://preloop.example/openai/v1
`
	if err := os.WriteFile(configPath, []byte(body), 0644); err != nil {
		t.Fatalf("failed to seed hermes config: %v", err)
	}

	upstream, err := parseHermesManagedGatewayUpstream(AgentConfig{
		Name:       hermesAgentName,
		ConfigPath: configPath,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream != nil {
		t.Fatalf("expected nil upstream when already pointed at preloop, got %#v", upstream)
	}
}

func TestManagedGatewayBindingConfigKey_Hermes(t *testing.T) {
	got := managedGatewayBindingConfigKey(AgentConfig{Name: hermesAgentName})
	if got != "model.default" {
		t.Fatalf("expected model.default, got %q", got)
	}
}

func TestRestartHermesGatewayAfterReconfigSkipsNonHermes(t *testing.T) {
	result := restartHermesGatewayAfterReconfig(
		AgentConfig{Name: "OpenClaw"},
		io.Discard,
	)
	if len(result) != 0 {
		t.Fatalf("expected empty result for non-Hermes agent, got %#v", result)
	}
}

func TestRestartHermesGatewayAfterReconfigHermesNotFound(t *testing.T) {
	t.Setenv("PATH", filepath.Join(t.TempDir(), "empty-path"))

	result := restartHermesGatewayAfterReconfig(
		AgentConfig{Name: hermesAgentName},
		io.Discard,
	)
	if result["gateway_restart_status"] != "skipped" ||
		result["gateway_restart_skip_reason"] != "hermes_not_found" ||
		result["gateway_restarted"] != false {
		t.Fatalf("expected skipped restart when hermes is missing, got %#v", result)
	}
}

func TestRestartHermesGatewayAfterReconfigSuccess(t *testing.T) {
	skipNoShebangOnWindows(t, "Hermes gateway restart success path")
	dir := t.TempDir()
	binDir := filepath.Join(dir, "bin")
	if err := os.MkdirAll(binDir, 0755); err != nil {
		t.Fatalf("failed to create bin dir: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(binDir, "hermes"),
		[]byte("#!/bin/sh\n[ \"$1\" = gateway ] && [ \"$2\" = restart ]\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake Hermes binary: %v", err)
	}
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	result := restartHermesGatewayAfterReconfig(
		AgentConfig{Name: hermesAgentName},
		io.Discard,
	)
	if result["gateway_restart_status"] != "restarted" ||
		result["gateway_restarted"] != true {
		t.Fatalf("expected successful Hermes gateway restart, got %#v", result)
	}
}

func TestRestartHermesGatewayAfterReconfigFailure(t *testing.T) {
	skipNoShebangOnWindows(t, "Hermes gateway restart failure path")
	dir := t.TempDir()
	binDir := filepath.Join(dir, "bin")
	if err := os.MkdirAll(binDir, 0755); err != nil {
		t.Fatalf("failed to create bin dir: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(binDir, "hermes"),
		[]byte("#!/bin/sh\necho restart failed 1>&2\nexit 1\n"),
		0755,
	); err != nil {
		t.Fatalf("failed to write fake Hermes binary: %v", err)
	}
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	result := restartHermesGatewayAfterReconfig(
		AgentConfig{Name: hermesAgentName},
		io.Discard,
	)
	if result["gateway_restart_status"] != "failed" ||
		result["gateway_restarted"] != false ||
		!strings.Contains(result["gateway_restart_error"].(string), "restart failed") {
		t.Fatalf("expected failed Hermes gateway restart, got %#v", result)
	}
}
