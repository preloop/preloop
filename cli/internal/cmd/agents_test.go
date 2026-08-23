package cmd

import (
	"bufio"
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestOnboardCandidatesBestEffortContinuesPastLauncherFailures(t *testing.T) {
	candidates := []AgentConfig{
		{
			Name:        "Gemini CLI",
			DisplayName: "Gemini CLI",
			ConfigPath:  "/tmp/gemini/settings.json",
		},
		{
			Name:        "Codex CLI",
			DisplayName: "Codex CLI",
			ConfigPath:  "/tmp/codex/config.toml",
		},
		{
			Name:        "OpenClaw",
			DisplayName: "Mini",
			ConfigPath:  "/tmp/openclaw/openclaw.json",
		},
		{
			Name:        "Hermes",
			DisplayName: "Hermes",
			ConfigPath:  "/tmp/hermes/config.yaml",
		},
	}
	var attempted []string

	enrolled, failures := onboardCandidatesBestEffort(
		bytes.NewBuffer(nil),
		io.Discard,
		candidates,
		managedEnrollmentOptions{},
		true,
		func(agent AgentConfig, opts managedEnrollmentOptions) error {
			attempted = append(attempted, agent.Name)
			switch agent.Name {
			case "Gemini CLI":
				return fmt.Errorf(
					"failed to locate gemini executable for managed launcher: %w",
					exec.ErrNotFound,
				)
			case "Codex CLI":
				return fmt.Errorf(
					"failed to locate codex executable for managed launcher: %w",
					exec.ErrNotFound,
				)
			}
			if !opts.AutoApprove || !opts.SkipConfirmation || !opts.DeferLiveValidate {
				t.Fatalf("expected auto-approved deferred opts, got %+v", opts)
			}
			return nil
		},
	)

	if got := strings.Join(attempted, ","); got != "Gemini CLI,Codex CLI,OpenClaw,Hermes" {
		t.Fatalf("expected all candidates to be attempted in order, got %v", attempted)
	}
	if len(enrolled) != 2 ||
		enrolled[0].Name != "OpenClaw" ||
		enrolled[1].Name != "Hermes" {
		t.Fatalf("expected OpenClaw and Hermes to enroll after launcher failures, got %+v", enrolled)
	}
	if len(failures) != 2 ||
		failures[0].Agent.Name != "Gemini CLI" ||
		failures[1].Agent.Name != "Codex CLI" {
		t.Fatalf("expected Gemini and Codex failures to be recorded, got %+v", failures)
	}
}

func TestPrintAgentOnboardingFailuresReportsRecordedFailures(t *testing.T) {
	failures := []agentOnboardingFailure{
		{
			Agent: AgentConfig{Name: "Gemini CLI", DisplayName: "Gemini CLI"},
			Err: fmt.Errorf(
				"failed to locate gemini executable for managed launcher: %w",
				exec.ErrNotFound,
			),
		},
		{
			Agent: AgentConfig{Name: "Codex CLI", DisplayName: "Codex CLI"},
			Err: fmt.Errorf(
				"failed to locate codex executable for managed launcher: %w",
				exec.ErrNotFound,
			),
		},
	}

	var output bytes.Buffer
	printAgentOnboardingFailures(&output, failures)

	report := output.String()
	for _, expected := range []string{
		"Some agents could not be onboarded:",
		"Gemini CLI: failed to locate gemini executable for managed launcher",
		"Codex CLI: failed to locate codex executable for managed launcher",
		"preloop agents onboard <agent> -y",
	} {
		if !strings.Contains(report, expected) {
			t.Fatalf("expected failure report to contain %q, got:\n%s", expected, report)
		}
	}
}

func TestFailureFootersPointAtTroubleshootingDocs(t *testing.T) {
	t.Run("summary with a failed agent links the docs once", func(t *testing.T) {
		var output bytes.Buffer
		printAgentOnboardingSummary(&output, []agentOnboardingOutcome{
			{Agent: AgentConfig{Name: "Codex CLI"}, Status: agentOnboardingStatusOnboarded},
			{Agent: AgentConfig{Name: "Gemini CLI"}, Status: agentOnboardingStatusFailed, Reason: "boom"},
		})
		if strings.Count(output.String(), troubleshootingDocsURL) != 1 {
			t.Fatalf("expected exactly one troubleshooting link, got:\n%s", output.String())
		}
	})

	t.Run("fully successful summary stays clean", func(t *testing.T) {
		var output bytes.Buffer
		printAgentOnboardingSummary(&output, []agentOnboardingOutcome{
			{Agent: AgentConfig{Name: "Codex CLI"}, Status: agentOnboardingStatusOnboarded},
			{Agent: AgentConfig{Name: "Gemini CLI"}, Status: agentOnboardingStatusPartial, Reason: "launcher skipped"},
		})
		if strings.Contains(output.String(), troubleshootingDocsURL) {
			t.Fatalf("expected no troubleshooting link without failures, got:\n%s", output.String())
		}
	})

	t.Run("batch failure report links the docs once", func(t *testing.T) {
		var output bytes.Buffer
		printAgentOnboardingFailures(&output, []agentOnboardingFailure{
			{Agent: AgentConfig{Name: "Codex CLI"}, Err: fmt.Errorf("boom")},
			{Agent: AgentConfig{Name: "Gemini CLI"}, Err: fmt.Errorf("also boom")},
		})
		if strings.Count(output.String(), troubleshootingDocsURL) != 1 {
			t.Fatalf("expected exactly one troubleshooting link, got:\n%s", output.String())
		}
	})
}

func TestResolveManagedAgentExecutablePathUsesNVMFallback(t *testing.T) {
	skipManagedLauncherOnWindows(t, "resolving an agent binary from the nvm fallback path")
	home := t.TempDir()
	testenv.SetHome(t, home)
	t.Setenv("PATH", "")

	geminiPath := filepath.Join(home, ".nvm", "versions", "node", "v25.8.1", "bin", "gemini")
	if err := os.MkdirAll(filepath.Dir(geminiPath), 0755); err != nil {
		t.Fatalf("failed to create nvm bin dir: %v", err)
	}
	if err := os.WriteFile(geminiPath, []byte("#!/usr/bin/env bash\n"), 0755); err != nil {
		t.Fatalf("failed to write gemini executable: %v", err)
	}
	launcherPath := filepath.Join(home, ".local", "bin", "gemini")

	resolved, err := resolveManagedAgentExecutablePath("gemini", launcherPath)
	if err != nil {
		t.Fatalf("expected nvm fallback to resolve executable: %v", err)
	}
	if resolved != geminiPath {
		t.Fatalf("expected %q, got %q", geminiPath, resolved)
	}
}

func TestFindStarterPolicyTools_CaseInsensitiveServerMatch(t *testing.T) {
	tools := []starterPolicyTool{
		{Name: "list_repos", Source: "mcp", SourceID: "srv-1", SourceName: "GitHub"},
		{Name: "create_issue", Source: "mcp", SourceID: "srv-1", SourceName: "GitHub"},
		{Name: "get_issue", Source: "builtin", SourceName: "Built-in"},
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(tools)
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	matchedName, filtered, err := findStarterPolicyTools(client, "github")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if matchedName != "GitHub" {
		t.Fatalf("expected canonical server name GitHub, got %q", matchedName)
	}
	if len(filtered) != 2 {
		t.Fatalf("expected 2 tools, got %d", len(filtered))
	}
	if filtered[0].Name != "create_issue" || filtered[1].Name != "list_repos" {
		t.Fatalf("expected sorted MCP tools, got %+v", filtered)
	}
}

func TestFindStarterPolicyTools_NotFoundIncludesAvailableServers(t *testing.T) {
	tools := []starterPolicyTool{
		{Name: "list_repos", Source: "mcp", SourceName: "GitHub"},
		{Name: "search_docs", Source: "mcp", SourceName: "Docs"},
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(tools)
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	_, _, err := findStarterPolicyTools(client, "missing")
	if err == nil {
		t.Fatal("expected an error")
	}

	errMsg := err.Error()
	if !strings.Contains(errMsg, "GitHub") || !strings.Contains(errMsg, "Docs") {
		t.Fatalf("expected available servers in error, got %q", errMsg)
	}
}

func TestBuildStarterPolicyPrompt_IncludesToolSummaries(t *testing.T) {
	tools := []starterPolicyTool{
		{
			Name:        "delete_repo",
			Description: "Delete a repository",
			Source:      "mcp",
			SourceName:  "GitHub",
			Schema: map[string]interface{}{
				"properties": map[string]interface{}{
					"owner": map[string]interface{}{},
					"repo":  map[string]interface{}{},
				},
				"required": []interface{}{"owner", "repo"},
			},
			AccessRules: []starterPolicyAccessRule{
				{Action: "require_approval", ConditionType: "simple"},
			},
		},
	}

	prompt := buildStarterPolicyPrompt("GitHub", tools)

	for _, expected := range []string{
		`MCP server "GitHub"`,
		"delete_repo: Delete a repository",
		"Suggested posture: require approval",
		"Args: owner, repo",
		"Required args: owner, repo",
		"Existing rules: require_approval (simple)",
	} {
		if !strings.Contains(prompt, expected) {
			t.Fatalf("expected prompt to contain %q, got:\n%s", expected, prompt)
		}
	}
}

func TestDefaultStarterPolicyFileName(t *testing.T) {
	if got := defaultStarterPolicyFileName("GitHub Enterprise"); got != "github-enterprise-starter-policy.yaml" {
		t.Fatalf("unexpected file name: %q", got)
	}
}

func TestRuntimeSessionSourceTypeForAgent(t *testing.T) {
	cases := map[string]string{
		"Claude Code":    "claude_code",
		"Claude Desktop": "claude_desktop",
		"Codex CLI":      "codex",
		"OpenClaw":       "openclaw",
		"Hermes":         "hermes",
		"Cursor":         "desktop_agent",
	}

	for agentName, want := range cases {
		if got := runtimeSessionSourceTypeForAgent(agentName); got != want {
			t.Fatalf("agent %q: expected %q, got %q", agentName, want, got)
		}
	}
}

func TestRuntimePrincipalIDForAgent_IsStable(t *testing.T) {
	agent := AgentConfig{
		Name:        "Claude Code",
		DisplayName: "Repo Assistant",
		ConfigPath:  filepath.Join("/tmp", "workspace", "claude_desktop_config.json"),
	}

	got1 := runtimePrincipalIDForAgent(agent)
	got2 := runtimePrincipalIDForAgent(agent)
	if got1 != got2 {
		t.Fatalf("expected stable source id, got %q and %q", got1, got2)
	}
	// v2 prefixes with the source type slug, not the display name.
	if !strings.HasPrefix(got1, "claude-code-") {
		t.Fatalf("expected v2 source-type prefix, got %q", got1)
	}
	renamed := agent
	renamed.DisplayName = "Totally Different"
	if runtimePrincipalIDForAgent(renamed) != got1 {
		t.Fatal("display name must not change the v2 principal id")
	}
}

func TestRuntimeSessionInstanceIDForAgent_UsesPrincipalPrefix(t *testing.T) {
	agent := AgentConfig{
		Name:       "Claude Code",
		ConfigPath: filepath.Join("/tmp", "workspace", "claude_desktop_config.json"),
	}

	principalID := runtimePrincipalIDForAgent(agent)
	sessionID := runtimeSessionInstanceIDForAgent(agent)
	if !strings.HasPrefix(sessionID, principalID+"-") {
		t.Fatalf("expected session id %q to use principal prefix %q", sessionID, principalID)
	}
}

func TestIssueRuntimeSessionToken(t *testing.T) {
	var capturedPath string
	var capturedBody runtimeSessionTokenRequest

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		capturedPath = r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		if err := json.NewDecoder(r.Body).Decode(&capturedBody); err != nil {
			t.Fatalf("failed to decode request body: %v", err)
		}
		_ = json.NewEncoder(w).Encode(runtimeSessionTokenResponse{
			RuntimeSessionID:  "session-1",
			Token:             "token-123",
			ExpiresAt:         "2026-03-10T12:00:00Z",
			SessionSourceType: "claude_code",
			SessionSourceID:   "claude-code-abc123",
			SessionReference:  "/tmp/workspace/claude_desktop_config.json",
		})
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	agent := AgentConfig{
		Name:        "Claude Code",
		DisplayName: "Workspace Assistant",
		ConfigPath:  "/tmp/workspace/claude_desktop_config.json",
	}

	result, err := issueRuntimeSessionToken(client, agent, []string{"github", "jira"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if capturedPath != "/api/v1/auth/runtime-sessions/token" {
		t.Fatalf("unexpected path: %q", capturedPath)
	}
	if capturedBody.SessionSourceType != "claude_code" {
		t.Fatalf("unexpected session source type: %q", capturedBody.SessionSourceType)
	}
	if capturedBody.RuntimePrincipalName != "Workspace Assistant" {
		t.Fatalf("unexpected principal name: %q", capturedBody.RuntimePrincipalName)
	}
	if capturedBody.RuntimePrincipalID == "" {
		t.Fatal("expected runtime principal id to be set")
	}
	if !strings.HasPrefix(capturedBody.RuntimePrincipalID, "claude-code-") {
		t.Fatalf("expected v2 source-type principal id, got %q", capturedBody.RuntimePrincipalID)
	}
	if !strings.HasPrefix(capturedBody.SessionSourceID, capturedBody.RuntimePrincipalID+"-") {
		t.Fatalf("expected session source id %q to use principal id prefix %q", capturedBody.SessionSourceID, capturedBody.RuntimePrincipalID)
	}
	if capturedBody.PrincipalIdentity == nil || capturedBody.PrincipalIdentity.Derivation != "v2" {
		t.Fatalf("expected principal_identity derivation=v2, got %#v", capturedBody.PrincipalIdentity)
	}
	if len(capturedBody.AllowedMCPServers) != 2 || capturedBody.AllowedMCPServers[0] != "github" || capturedBody.AllowedMCPServers[1] != "jira" {
		t.Fatalf("unexpected allowed servers: %+v", capturedBody.AllowedMCPServers)
	}
	if result.Token != "token-123" {
		t.Fatalf("unexpected token result: %+v", result)
	}
}

func TestParseGenericMCP_NestedMCPServers(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "openclaw.json")
	if err := os.WriteFile(configPath, []byte(`{
  "mcp": {
    "servers": {
      "preloop": {
        "url": "https://preloop.ai/mcp/v1",
        "transport": "http",
        "headers": {
          "Authorization": "Bearer test-token"
        }
      }
    }
  }
}`), 0644); err != nil {
		t.Fatalf("failed to write config: %v", err)
	}

	servers, err := parseGenericMCP(configPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	preloop, ok := servers["preloop"]
	if !ok {
		t.Fatalf("expected preloop server, got %+v", servers)
	}
	if preloop.Transport != "http" {
		t.Fatalf("expected transport http, got %q", preloop.Transport)
	}
	if preloop.Headers["Authorization"] != "Bearer test-token" {
		t.Fatalf("unexpected headers: %+v", preloop.Headers)
	}
}

func TestParseCodexConfigTOML_NestedMCPServers(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config.toml")
	if err := os.WriteFile(configPath, []byte(`
[mcp.servers.preloop]
url = "https://preloop.ai/mcp/v1"
transport = "http"

[mcp.servers.preloop.auth]
type = "bearer"
token = "test-token"
`), 0644); err != nil {
		t.Fatalf("failed to write config: %v", err)
	}

	servers, err := parseCodexConfig(configPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	preloop, ok := servers["preloop"]
	if !ok {
		t.Fatalf("expected preloop server, got %+v", servers)
	}
	if preloop.Transport != "http" {
		t.Fatalf("expected transport http, got %q", preloop.Transport)
	}
	if preloop.Auth["type"] != "bearer" || preloop.Auth["token"] != "test-token" {
		t.Fatalf("unexpected auth payload: %+v", preloop.Auth)
	}
}

func TestParseCodexConfigTOML_LegacyMCPServers(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config.toml")
	if err := os.WriteFile(configPath, []byte(`
[mcp_servers.preloop]
url = "https://preloop.ai/mcp/v1"
bearer_token_env_var = "PRELOOP_TOKEN"
`), 0644); err != nil {
		t.Fatalf("failed to write config: %v", err)
	}

	servers, err := parseCodexConfig(configPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	preloop, ok := servers["preloop"]
	if !ok {
		t.Fatalf("expected preloop server, got %+v", servers)
	}
	if preloop.URL != "https://preloop.ai/mcp/v1" {
		t.Fatalf("unexpected legacy Codex server: %+v", preloop)
	}
}

func TestDiscoverAgentsFindsClaudeCodeSettingsAndCodexTOML(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	if err := os.MkdirAll(filepath.Join(home, ".claude"), 0755); err != nil {
		t.Fatalf("failed to create claude dir: %v", err)
	}
	if err := os.MkdirAll(filepath.Join(home, ".codex"), 0755); err != nil {
		t.Fatalf("failed to create codex dir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(home, ".claude", "settings.json"), []byte(`{"model":"claude-sonnet-4"}`), 0644); err != nil {
		t.Fatalf("failed to write claude settings: %v", err)
	}
	if err := os.WriteFile(filepath.Join(home, ".codex", "config.toml"), []byte(`
[projects."/tmp/workspace"]
trust_level = "trusted"
`), 0644); err != nil {
		t.Fatalf("failed to write codex config: %v", err)
	}

	discovered, err := discoverAgents(io.Discard, false)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	byName := map[string]AgentConfig{}
	for _, agent := range discovered {
		byName[agent.Name] = agent
	}
	if _, ok := byName["Claude Code"]; !ok {
		t.Fatalf("expected Claude Code to be discovered, got %#v", discovered)
	}
	if got, ok := byName["Codex CLI"]; !ok {
		t.Fatalf("expected Codex CLI to be discovered, got %#v", discovered)
	} else if got.ConfigPath != filepath.Join(home, ".codex", "config.toml") {
		t.Fatalf("expected codex config path to use TOML file, got %q", got.ConfigPath)
	}
}

func TestDiscoverAgentsFindsInstalledOpenCodeWithoutConfig(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	authPath := filepath.Join(home, ".local", "share", "opencode", "auth.json")
	if err := os.MkdirAll(filepath.Dir(authPath), 0755); err != nil {
		t.Fatalf("failed to create opencode auth dir: %v", err)
	}
	if err := os.WriteFile(authPath, []byte(`{"ok":true}`), 0644); err != nil {
		t.Fatalf("failed to write opencode auth marker: %v", err)
	}

	discovered, err := discoverAgents(io.Discard, false)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for _, agent := range discovered {
		if agent.Name != "OpenCode" {
			continue
		}
		wantPath := filepath.Join(home, ".config", "opencode", "config.json")
		if agent.ConfigPath != wantPath {
			t.Fatalf("expected synthesized OpenCode config path %q, got %q", wantPath, agent.ConfigPath)
		}
		if len(agent.MCPServers) != 0 {
			t.Fatalf("expected empty MCP server set for unconfigured OpenCode, got %+v", agent.MCPServers)
		}
		return
	}
	t.Fatalf("expected OpenCode to be discovered from installation markers, got %#v", discovered)
}

func TestParseOpenCodeManagedGatewayUpstreamUsesAuthDefaults(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	authPath := filepath.Join(home, ".local", "share", "opencode", "auth.json")
	if err := os.MkdirAll(filepath.Dir(authPath), 0755); err != nil {
		t.Fatalf("failed to create auth dir: %v", err)
	}
	if err := os.WriteFile(
		authPath,
		[]byte(`{"zai":{"type":"api","key":"zai-secret"}}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write auth file: %v", err)
	}

	upstream, err := parseOpenCodeManagedGatewayUpstream(
		AgentConfig{
			Name:       "OpenCode",
			ConfigPath: filepath.Join(home, ".config", "opencode", "config.json"),
		},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected upstream model to be detected")
	}
	if upstream.SourceProviderID != "zai" {
		t.Fatalf("unexpected provider id: %#v", upstream.SourceProviderID)
	}
	if upstream.ProviderName != "openai" {
		t.Fatalf("unexpected provider name: %#v", upstream.ProviderName)
	}
	if upstream.ModelIdentifier != "glm-5-turbo" {
		t.Fatalf("unexpected model id: %#v", upstream.ModelIdentifier)
	}
	if upstream.ManagedModelAlias != "zai/glm-5-turbo" {
		t.Fatalf("unexpected managed alias: %#v", upstream.ManagedModelAlias)
	}
	if upstream.APIEndpoint != "https://api.z.ai/api/coding/paas/v4" {
		t.Fatalf("unexpected api endpoint: %#v", upstream.APIEndpoint)
	}
	if upstream.APIKey != "zai-secret" {
		t.Fatalf("unexpected api key: %#v", upstream.APIKey)
	}
	if !upstream.CanRouteThroughGateway() {
		t.Fatal("expected OpenCode upstream to be routable through the gateway")
	}
}

func TestParseOpenCodeManagedGatewayUpstreamPrefersDiscoveredState(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, ".config", "opencode", "config.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create config dir: %v", err)
	}
	if err := os.WriteFile(
		configPath,
		[]byte(`{"provider":{"preloop":{"options":{"baseURL":"https://preloop.example/openai/v1","apiKey":"managed"}}},"model":"preloop/openai/gpt-5.4"}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write managed config: %v", err)
	}

	authPath := filepath.Join(home, ".local", "share", "opencode", "auth.json")
	if err := os.MkdirAll(filepath.Dir(authPath), 0755); err != nil {
		t.Fatalf("failed to create auth dir: %v", err)
	}
	if err := os.WriteFile(
		authPath,
		[]byte(`{"zai":{"type":"api","key":"zai-secret"}}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write auth file: %v", err)
	}

	statePath, err := localEnrollmentStatePath("OpenCode", configPath)
	if err != nil {
		t.Fatalf("failed to resolve state path: %v", err)
	}
	if err := os.MkdirAll(filepath.Dir(statePath), 0755); err != nil {
		t.Fatalf("failed to create state dir: %v", err)
	}
	state := localEnrollmentState{
		AgentName:        "OpenCode",
		ConfigPath:       configPath,
		DiscoveredConfig: map[string]interface{}{"model": "zai/glm-5-turbo"},
		ManagedConfig:    map[string]interface{}{"model": "preloop/openai/gpt-5.4"},
	}
	stateData, err := json.Marshal(state)
	if err != nil {
		t.Fatalf("failed to encode state: %v", err)
	}
	if err := os.WriteFile(statePath, stateData, 0644); err != nil {
		t.Fatalf("failed to write state: %v", err)
	}

	upstream, err := parseOpenCodeManagedGatewayUpstream(
		AgentConfig{Name: "OpenCode", ConfigPath: configPath},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected upstream model to be detected")
	}
	if upstream.ManagedModelAlias != "zai/glm-5-turbo" {
		t.Fatalf("expected discovered config alias, got %#v", upstream.ManagedModelAlias)
	}
}

func TestParseOpenCodeManagedGatewayUpstreamIgnoresManagedPreloopConfig(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, ".config", "opencode", "config.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create config dir: %v", err)
	}
	if err := os.WriteFile(
		configPath,
		[]byte(`{"provider":{"preloop":{"options":{"baseURL":"https://ai-model-gateway.review.preloop.ai/openai/v1","apiKey":"managed"}}},"model":"preloop/openai/gpt-5.4"}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write managed config: %v", err)
	}

	authPath := filepath.Join(home, ".local", "share", "opencode", "auth.json")
	if err := os.MkdirAll(filepath.Dir(authPath), 0755); err != nil {
		t.Fatalf("failed to create auth dir: %v", err)
	}
	if err := os.WriteFile(
		authPath,
		[]byte(`{"zai":{"type":"api","key":"zai-secret"}}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write auth file: %v", err)
	}

	upstream, err := parseOpenCodeManagedGatewayUpstream(
		AgentConfig{Name: "OpenCode", ConfigPath: configPath},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected auth-backed upstream model to be detected")
	}
	if upstream.ManagedModelAlias != "zai/glm-5-turbo" {
		t.Fatalf("expected OpenCode to ignore managed preloop config, got %#v", upstream.ManagedModelAlias)
	}
}

func TestParseCodexManagedGatewayUpstreamReturnsNilWithoutResolvedModel(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, ".codex", "config.toml")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create codex dir: %v", err)
	}
	if err := os.WriteFile(
		configPath,
		[]byte("[projects.\"/tmp/project\"]\ntrust_level = \"trusted\"\n"),
		0644,
	); err != nil {
		t.Fatalf("failed to write codex config: %v", err)
	}

	upstream, err := parseCodexManagedGatewayUpstream(
		AgentConfig{Name: "Codex CLI", ConfigPath: configPath},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream != nil {
		t.Fatalf("expected unresolved Codex config to stay direct, got %+v", upstream)
	}
}

func TestParseGeminiManagedGatewayUpstreamUsesRecentChatAndDotEnv(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, ".gemini", "settings.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create gemini dir: %v", err)
	}
	if err := os.WriteFile(
		configPath,
		[]byte(`{"security":{"auth":{"selectedType":"gemini-api-key"}}}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write gemini config: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(home, ".gemini", ".env"),
		[]byte("export GEMINI_API_KEY='gemini-secret'\n"),
		0644,
	); err != nil {
		t.Fatalf("failed to write gemini env file: %v", err)
	}

	chatPath := filepath.Join(home, ".gemini", "tmp", "preloop-ee", "chats", "session.json")
	if err := os.MkdirAll(filepath.Dir(chatPath), 0755); err != nil {
		t.Fatalf("failed to create gemini chat dir: %v", err)
	}
	if err := os.WriteFile(
		chatPath,
		[]byte(`{"lastUpdated":"2026-04-04T10:00:00Z","messages":[{"type":"gemini","timestamp":"2026-04-04T10:00:00Z","model":"gemini-3-flash-preview"}]}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write gemini chat session: %v", err)
	}

	upstream, err := parseGeminiManagedGatewayUpstream(
		AgentConfig{Name: "Gemini CLI", ConfigPath: configPath},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected upstream Gemini model to be detected")
	}
	if upstream.ManagedModelAlias != "google/gemini-3-flash-preview" {
		t.Fatalf("expected inferred Gemini model alias, got %#v", upstream.ManagedModelAlias)
	}
	if upstream.APIKey != "gemini-secret" {
		t.Fatalf("expected Gemini API key from .env, got %#v", upstream.APIKey)
	}
	if len(upstream.Notes) == 0 {
		t.Fatalf("expected explanatory notes, got %#v", upstream)
	}
}

func TestParseGeminiManagedGatewayUpstreamUsesEncryptedStoredAPIKey(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	t.Setenv("USER", "preloop-gemini-test")

	configPath := filepath.Join(home, ".gemini", "settings.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create gemini dir: %v", err)
	}
	if err := os.WriteFile(
		configPath,
		[]byte(`{"security":{"auth":{"selectedType":"gemini-api-key"}}}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write gemini config: %v", err)
	}

	chatPath := filepath.Join(home, ".gemini", "tmp", "preloop-ee", "chats", "session.json")
	if err := os.MkdirAll(filepath.Dir(chatPath), 0755); err != nil {
		t.Fatalf("failed to create gemini chat dir: %v", err)
	}
	if err := os.WriteFile(
		chatPath,
		[]byte(`{"lastUpdated":"2026-04-04T10:00:00Z","messages":[{"type":"gemini","timestamp":"2026-04-04T10:00:00Z","model":"gemini-3-flash-preview"}]}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write gemini chat session: %v", err)
	}

	blob, err := json.Marshal(map[string]interface{}{
		"serverName": "default-api-key",
		"token": map[string]interface{}{
			"accessToken": "gemini-stored-secret",
			"tokenType":   "ApiKey",
		},
		"updatedAt": 1712224800000,
	})
	if err != nil {
		t.Fatalf("failed to encode Gemini credential blob: %v", err)
	}
	store, err := json.Marshal(map[string]map[string]string{
		geminiAPIKeyServiceName: {
			geminiAPIKeyAccountName: string(blob),
		},
	})
	if err != nil {
		t.Fatalf("failed to encode Gemini credential store: %v", err)
	}
	encryptedStore, err := encryptGeminiCredentialStoreForTest(string(store))
	if err != nil {
		t.Fatalf("failed to encrypt Gemini credential store: %v", err)
	}
	credentialsPath := filepath.Join(home, ".gemini", "gemini-credentials.json")
	if err := os.WriteFile(credentialsPath, []byte(encryptedStore), 0600); err != nil {
		t.Fatalf("failed to write Gemini credential store: %v", err)
	}

	upstream, err := parseGeminiManagedGatewayUpstream(
		AgentConfig{Name: "Gemini CLI", ConfigPath: configPath},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected upstream Gemini model to be detected")
	}
	if upstream.APIKey != "gemini-stored-secret" {
		t.Fatalf("expected Gemini API key from secure storage, got %#v", upstream.APIKey)
	}
	if len(upstream.Notes) == 0 || !strings.Contains(strings.Join(upstream.Notes, " "), "gemini-credentials.json") {
		t.Fatalf("expected note about encrypted Gemini credential store, got %#v", upstream.Notes)
	}
}

func TestParseGeminiManagedGatewayUpstreamUsesEncryptedObjectAPIKey(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	t.Setenv("USER", "preloop-gemini-test")

	configPath := filepath.Join(home, ".gemini", "settings.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create gemini dir: %v", err)
	}
	if err := os.WriteFile(
		configPath,
		[]byte(`{"security":{"auth":{"selectedType":"gemini-api-key"}}}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write gemini config: %v", err)
	}
	chatPath := filepath.Join(home, ".gemini", "tmp", "preloop-ee", "chats", "session.json")
	if err := os.MkdirAll(filepath.Dir(chatPath), 0755); err != nil {
		t.Fatalf("failed to create gemini chat dir: %v", err)
	}
	if err := os.WriteFile(
		chatPath,
		[]byte(`{"lastUpdated":"2026-04-04T10:00:00Z","messages":[{"type":"gemini","timestamp":"2026-04-04T10:00:00Z","model":"gemini-3-flash-preview"}]}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write gemini chat session: %v", err)
	}

	store, err := json.Marshal(map[string]map[string]interface{}{
		geminiAPIKeyServiceName: {
			geminiAPIKeyAccountName: map[string]interface{}{
				"serverName": "default-api-key",
				"token": map[string]interface{}{
					"accessToken": "gemini-stored-object-secret",
					"tokenType":   "ApiKey",
				},
				"updatedAt": 1712224800000,
			},
		},
	})
	if err != nil {
		t.Fatalf("failed to encode Gemini credential store: %v", err)
	}
	encryptedStore, err := encryptGeminiCredentialStoreForTest(string(store))
	if err != nil {
		t.Fatalf("failed to encrypt Gemini credential store: %v", err)
	}
	credentialsPath := filepath.Join(home, ".gemini", "gemini-credentials.json")
	if err := os.WriteFile(credentialsPath, []byte(encryptedStore), 0600); err != nil {
		t.Fatalf("failed to write Gemini credential store: %v", err)
	}

	upstream, err := parseGeminiManagedGatewayUpstream(
		AgentConfig{Name: "Gemini CLI", ConfigPath: configPath},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected upstream Gemini model to be detected")
	}
	if upstream.APIKey != "gemini-stored-object-secret" {
		t.Fatalf("expected Gemini API key from secure storage, got %#v", upstream.APIKey)
	}
}

func TestParseGeminiManagedGatewayUpstreamDefaultsFreshAPIKeyInstall(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	t.Setenv("USER", "preloop-gemini-test")

	configPath := filepath.Join(home, ".gemini", "settings.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create gemini dir: %v", err)
	}
	if err := os.WriteFile(
		configPath,
		[]byte(`{"security":{"auth":{"selectedType":"gemini-api-key"}}}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write gemini config: %v", err)
	}

	store, err := json.Marshal(map[string]map[string]interface{}{
		geminiAPIKeyServiceName: {
			geminiAPIKeyAccountName: map[string]interface{}{
				"serverName": "default-api-key",
				"token": map[string]interface{}{
					"accessToken": "gemini-fresh-secret",
					"tokenType":   "ApiKey",
				},
				"updatedAt": 1712224800000,
			},
		},
	})
	if err != nil {
		t.Fatalf("failed to encode Gemini credential store: %v", err)
	}
	encryptedStore, err := encryptGeminiCredentialStoreForTest(string(store))
	if err != nil {
		t.Fatalf("failed to encrypt Gemini credential store: %v", err)
	}
	credentialsPath := filepath.Join(home, ".gemini", "gemini-credentials.json")
	if err := os.WriteFile(credentialsPath, []byte(encryptedStore), 0600); err != nil {
		t.Fatalf("failed to write Gemini credential store: %v", err)
	}

	upstream, err := parseGeminiManagedGatewayUpstream(
		AgentConfig{Name: "Gemini CLI", ConfigPath: configPath},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected upstream Gemini model to be detected")
	}
	if upstream.ManagedModelAlias != "google/gemini-3-flash-preview" {
		t.Fatalf("expected default Gemini model alias, got %#v", upstream.ManagedModelAlias)
	}
	if upstream.APIKey != "gemini-fresh-secret" {
		t.Fatalf("expected Gemini API key from secure storage, got %#v", upstream.APIKey)
	}
	if !strings.Contains(strings.Join(upstream.Notes, " "), "Defaulted fresh Gemini CLI API-key install") {
		t.Fatalf("expected default-model note, got %#v", upstream.Notes)
	}
}

func TestParseGeminiManagedGatewayUpstreamRecoversFromManagedConfig(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	t.Setenv("USER", "preloop-gemini-test")

	configPath := filepath.Join(home, ".gemini", "settings.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create gemini dir: %v", err)
	}
	if err := os.WriteFile(
		configPath,
		[]byte(`{"apiKey":"agt_managed","baseUrl":"https://preloop.example/gemini","model":{"name":"gemini-3-flash-preview"},"security":{"auth":{"selectedType":"gemini-api-key"}}}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write managed gemini config: %v", err)
	}

	blob, err := json.Marshal(map[string]interface{}{
		"serverName": "default-api-key",
		"token": map[string]interface{}{
			"accessToken": "gemini-stored-secret",
			"tokenType":   "ApiKey",
		},
		"updatedAt": 1712224800000,
	})
	if err != nil {
		t.Fatalf("failed to encode Gemini credential blob: %v", err)
	}
	store, err := json.Marshal(map[string]map[string]string{
		geminiAPIKeyServiceName: {
			geminiAPIKeyAccountName: string(blob),
		},
	})
	if err != nil {
		t.Fatalf("failed to encode Gemini credential store: %v", err)
	}
	encryptedStore, err := encryptGeminiCredentialStoreForTest(string(store))
	if err != nil {
		t.Fatalf("failed to encrypt Gemini credential store: %v", err)
	}
	credentialsPath := filepath.Join(home, ".gemini", "gemini-credentials.json")
	if err := os.WriteFile(credentialsPath, []byte(encryptedStore), 0600); err != nil {
		t.Fatalf("failed to write Gemini credential store: %v", err)
	}

	upstream, err := parseGeminiManagedGatewayUpstream(
		AgentConfig{Name: "Gemini CLI", ConfigPath: configPath},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected upstream Gemini model to be recovered")
	}
	if upstream.ManagedModelAlias != "google/gemini-3-flash-preview" {
		t.Fatalf("unexpected managed Gemini model alias: %#v", upstream.ManagedModelAlias)
	}
	if upstream.APIKey != "gemini-stored-secret" {
		t.Fatalf("expected Gemini API key from secure storage, got %#v", upstream.APIKey)
	}
	if upstream.APIEndpoint != "" {
		t.Fatalf("expected managed Gemini config to avoid reusing Preloop baseUrl, got %#v", upstream.APIEndpoint)
	}
	if !strings.Contains(strings.Join(upstream.Notes, " "), "already pointed at Preloop") {
		t.Fatalf("expected note about recovering from managed config, got %#v", upstream.Notes)
	}
}

func TestParseClaudeManagedGatewayUpstreamUsesRecentSessionModel(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	t.Setenv("CLAUDE_TEST_TOKEN", "sk-ant-api03-claude-auth-token")

	configPath := filepath.Join(home, ".claude", "settings.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create claude dir: %v", err)
	}
	if err := os.WriteFile(
		configPath,
		[]byte(`{"model":"opus[1m]","env":{"ANTHROPIC_AUTH_TOKEN":"${CLAUDE_TEST_TOKEN}"}}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write claude config: %v", err)
	}

	sessionPath := filepath.Join(home, ".claude", "projects", "project-a", "session.jsonl")
	if err := os.MkdirAll(filepath.Dir(sessionPath), 0755); err != nil {
		t.Fatalf("failed to create claude session dir: %v", err)
	}
	if err := os.WriteFile(
		sessionPath,
		[]byte("{\"timestamp\":\"2026-04-04T11:00:00Z\",\"message\":{\"model\":\"claude-opus-4-6\"}}\n"),
		0644,
	); err != nil {
		t.Fatalf("failed to write claude session log: %v", err)
	}
	t.Setenv("CLAUDE_CODE_USE_BEDROCK", "")
	t.Setenv("AWS_BEARER_TOKEN_BEDROCK", "")
	t.Setenv("ANTHROPIC_MODEL", "")

	upstream, err := parseClaudeManagedGatewayUpstream(
		AgentConfig{Name: "Claude Code", ConfigPath: configPath},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected upstream Claude model to be detected")
	}
	if upstream.ManagedModelAlias != "anthropic/claude-opus-4-6" {
		t.Fatalf("expected inferred Claude alias, got %#v", upstream.ManagedModelAlias)
	}
	if upstream.APIKey != "sk-ant-api03-claude-auth-token" {
		t.Fatalf("expected Claude auth token, got %#v", upstream.APIKey)
	}
	if len(upstream.Notes) == 0 {
		t.Fatalf("expected explanatory notes, got %#v", upstream)
	}
}

func TestParseClaudeManagedGatewayUpstreamImportsOAuthWithWarning(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	t.Setenv("CLAUDE_TEST_TOKEN", "sk-ant-oat01-claude-code-token")

	configPath := filepath.Join(home, ".claude", "settings.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create claude dir: %v", err)
	}
	if err := os.WriteFile(
		configPath,
		[]byte(`{"model":"opus[1m]","env":{"ANTHROPIC_AUTH_TOKEN":"${CLAUDE_TEST_TOKEN}"}}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write claude config: %v", err)
	}
	sessionPath := filepath.Join(home, ".claude", "projects", "project-a", "session.jsonl")
	if err := os.MkdirAll(filepath.Dir(sessionPath), 0755); err != nil {
		t.Fatalf("failed to create claude session dir: %v", err)
	}
	if err := os.WriteFile(
		sessionPath,
		[]byte("{\"timestamp\":\"2026-04-04T11:00:00Z\",\"message\":{\"model\":\"claude-opus-4-6\"}}\n"),
		0644,
	); err != nil {
		t.Fatalf("failed to write claude session log: %v", err)
	}
	t.Setenv("CLAUDE_CODE_USE_BEDROCK", "")
	t.Setenv("AWS_BEARER_TOKEN_BEDROCK", "")
	t.Setenv("ANTHROPIC_MODEL", "")

	upstream, err := parseClaudeManagedGatewayUpstream(
		AgentConfig{Name: "Claude Code", ConfigPath: configPath},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected upstream Claude model metadata to be detected")
	}
	if upstream.ManagedModelAlias != "anthropic/claude-opus-4-6" {
		t.Fatalf("expected inferred Claude alias, got %#v", upstream.ManagedModelAlias)
	}
	if upstream.APIKey != "" {
		t.Fatalf("expected OAuth credential to stay out of APIKey field, got %#v", upstream.APIKey)
	}
	if upstream.CredentialType != "oauth_anthropic_claude_code" {
		t.Fatalf("expected Claude OAuth credential type, got %#v", upstream.CredentialType)
	}
	if got := upstream.CredentialPayload["access"]; got != "sk-ant-oat01-claude-code-token" {
		t.Fatalf("expected OAuth access token payload, got %#v", got)
	}
	if !upstream.CanRouteThroughGateway() {
		t.Fatalf("expected OAuth-backed Claude model traffic to route through gateway")
	}
	if len(upstream.Notes) == 0 ||
		!strings.Contains(strings.Join(upstream.Notes, "\n"), "ANTHROPIC_API_KEY") ||
		!strings.Contains(strings.Join(upstream.Notes, "\n"), "best-effort") ||
		!strings.Contains(strings.Join(upstream.Notes, "\n"), "subscription/OAuth") {
		t.Fatalf("expected OAuth risk and API billing guidance note, got %#v", upstream.Notes)
	}
}

func encryptGeminiCredentialStoreForTest(plaintext string) (string, error) {
	key, err := deriveGeminiCredentialStoreKey()
	if err != nil {
		return "", err
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return "", err
	}
	iv := bytes.Repeat([]byte{0x2a}, 16)
	gcm, err := cipher.NewGCMWithNonceSize(block, len(iv))
	if err != nil {
		return "", err
	}
	ciphertextWithTag := gcm.Seal(nil, iv, []byte(plaintext), nil)
	authTagStart := len(ciphertextWithTag) - gcm.Overhead()
	ciphertext := ciphertextWithTag[:authTagStart]
	authTag := ciphertextWithTag[authTagStart:]
	return hex.EncodeToString(iv) + ":" + hex.EncodeToString(authTag) + ":" + hex.EncodeToString(ciphertext), nil
}

func TestParseCodexManagedGatewayUpstreamNotesChatGPTOAuth(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, ".codex", "config.toml")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create codex dir: %v", err)
	}
	if err := os.WriteFile(
		configPath,
		[]byte("model = 'gpt-5.4'\nmodel_provider = 'openai'\n"),
		0644,
	); err != nil {
		t.Fatalf("failed to write codex config: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(home, ".codex", "auth.json"),
		[]byte(`{"auth_mode":"chatgpt","OPENAI_API_KEY":null}`),
		0644,
	); err != nil {
		t.Fatalf("failed to write codex auth file: %v", err)
	}

	upstream, err := parseCodexManagedGatewayUpstream(
		AgentConfig{Name: "Codex CLI", ConfigPath: configPath},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected Codex upstream to resolve model metadata")
	}
	if upstream.APIKey != "" {
		t.Fatalf("expected no importable Codex API key, got %#v", upstream.APIKey)
	}
	if len(upstream.Notes) == 0 || !strings.Contains(upstream.Notes[0], "could not be resolved") {
		t.Fatalf("expected ChatGPT OAuth note, got %#v", upstream.Notes)
	}
}

func TestParseCodexManagedGatewayUpstreamImportsChatGPTOAuth(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, ".codex", "config.toml")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create codex dir: %v", err)
	}
	if err := os.WriteFile(
		configPath,
		[]byte("model = 'gpt-5.4'\nmodel_provider = 'openai'\n"),
		0644,
	); err != nil {
		t.Fatalf("failed to write codex config: %v", err)
	}

	jwtPayload := base64.RawURLEncoding.EncodeToString([]byte(`{"exp":1893456000,"https://api.openai.com/auth":{"chatgpt_account_id":"acct-test"}}`))
	accessToken := "header." + jwtPayload + ".sig"
	authJSON := `{
		"auth_mode":"chatgpt",
		"tokens":{
			"access_token":"` + accessToken + `",
			"refresh_token":"refresh-token"
		}
	}`
	if err := os.WriteFile(
		filepath.Join(home, ".codex", "auth.json"),
		[]byte(authJSON),
		0644,
	); err != nil {
		t.Fatalf("failed to write codex auth file: %v", err)
	}

	upstream, err := parseCodexManagedGatewayUpstream(
		AgentConfig{Name: "Codex CLI", ConfigPath: configPath},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected Codex upstream to resolve model metadata")
	}
	if upstream.CredentialType != "oauth_openai_codex" {
		t.Fatalf("expected oauth_openai_codex credentials, got %#v", upstream.CredentialType)
	}
	if upstream.ProviderName != "openai-codex" {
		t.Fatalf("expected openai-codex provider, got %#v", upstream.ProviderName)
	}
	if upstream.APIEndpoint != "https://chatgpt.com/backend-api/codex" {
		t.Fatalf("unexpected Codex endpoint: %#v", upstream.APIEndpoint)
	}
	if got := upstream.CredentialPayload["account_id"]; got != "acct-test" {
		t.Fatalf("expected account_id from JWT, got %#v", got)
	}
	if got := upstream.CredentialPayload["access"]; got != accessToken {
		t.Fatalf("expected access token in payload, got %#v", got)
	}
}

func TestParseCodexManagedGatewayUpstreamInfersOpenAIForBareChatGPTModel(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, ".codex", "config.toml")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create codex dir: %v", err)
	}
	if err := os.WriteFile(configPath, []byte("model = 'gpt-5.5'\n"), 0644); err != nil {
		t.Fatalf("failed to write codex config: %v", err)
	}

	jwtPayload := base64.RawURLEncoding.EncodeToString([]byte(`{"exp":1893456000}`))
	accessToken := "header." + jwtPayload + ".sig"
	authJSON := `{
		"auth_mode":"chatgpt",
		"tokens":{
			"access_token":"` + accessToken + `",
			"refresh_token":"refresh-token"
		}
	}`
	if err := os.WriteFile(
		filepath.Join(home, ".codex", "auth.json"),
		[]byte(authJSON),
		0644,
	); err != nil {
		t.Fatalf("failed to write codex auth file: %v", err)
	}

	upstream, err := parseCodexManagedGatewayUpstream(
		AgentConfig{Name: "Codex CLI", ConfigPath: configPath},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected Codex upstream to resolve bare ChatGPT model")
	}
	if upstream.ProviderName != "openai-codex" {
		t.Fatalf("expected openai-codex provider, got %#v", upstream.ProviderName)
	}
	if upstream.ManagedModelAlias != "openai/gpt-5.5" {
		t.Fatalf("expected openai-prefixed alias, got %#v", upstream.ManagedModelAlias)
	}
}

func TestCodexGatewayResolutionPrefersCurrentDirectModel(t *testing.T) {
	current := map[string]interface{}{
		"model": "gpt-5.5",
		"mcp_servers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"url": "https://preloop.example/mcp/v1",
			},
		},
	}
	if !shouldPreferCurrentConfigForGatewayResolution(
		AgentConfig{Name: "Codex CLI"},
		current,
	) {
		t.Fatal("expected direct Codex model to override stale discovered config")
	}

	current["model_provider"] = "preloop"
	if shouldPreferCurrentConfigForGatewayResolution(
		AgentConfig{Name: "Codex CLI"},
		current,
	) {
		t.Fatal("expected managed Codex model provider to use discovered backup")
	}
}

func TestParseCodexManagedGatewayUpstreamRecoversFromPreloopProviderSnapshot(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, ".codex", "config.toml")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create codex dir: %v", err)
	}
	if err := os.WriteFile(
		configPath,
		[]byte("model = 'openai/gpt-5.5'\nmodel_provider = 'preloop'\n"),
		0644,
	); err != nil {
		t.Fatalf("failed to write codex config: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(home, ".codex", "auth.json"),
		[]byte(`{"auth_mode":"chatgpt","tokens":{"access_token":"access","refresh_token":"refresh","account_id":"acct"}}`),
		0600,
	); err != nil {
		t.Fatalf("failed to write codex auth file: %v", err)
	}

	upstream, err := parseCodexManagedGatewayUpstream(
		AgentConfig{Name: "Codex CLI", ConfigPath: configPath},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil || !upstream.CanRouteThroughGateway() {
		t.Fatalf("expected recoverable upstream, got %#v", upstream)
	}
	if upstream.ManagedModelAlias != "openai/gpt-5.5" {
		t.Fatalf("expected openai/gpt-5.5 alias, got %#v", upstream.ManagedModelAlias)
	}
}

func TestParseCodexManagedGatewayUpstreamForcesOpenAIProviderForChatGPTOAuth(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, ".codex", "config.toml")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create codex dir: %v", err)
	}
	if err := os.WriteFile(
		configPath,
		[]byte("model = 'gpt-5.5'\nmodel_provider = 'anthropic'\n"),
		0644,
	); err != nil {
		t.Fatalf("failed to write codex config: %v", err)
	}

	jwtPayload := base64.RawURLEncoding.EncodeToString([]byte(`{"exp":1893456000}`))
	accessToken := "header." + jwtPayload + ".sig"
	authJSON := `{
		"auth_mode":"chatgpt",
		"tokens":{
			"access_token":"` + accessToken + `",
			"refresh_token":"refresh-token"
		}
	}`
	if err := os.WriteFile(
		filepath.Join(home, ".codex", "auth.json"),
		[]byte(authJSON),
		0644,
	); err != nil {
		t.Fatalf("failed to write codex auth file: %v", err)
	}

	upstream, err := parseCodexManagedGatewayUpstream(
		AgentConfig{Name: "Codex CLI", ConfigPath: configPath},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected Codex upstream to resolve model metadata")
	}
	if upstream.SourceProviderID != "openai" {
		t.Fatalf("expected stale provider to be ignored, got %#v", upstream.SourceProviderID)
	}
	if upstream.ManagedModelAlias != "openai/gpt-5.5" {
		t.Fatalf("expected OpenAI Codex alias, got %#v", upstream.ManagedModelAlias)
	}
}

func TestParseCodexManagedGatewayUpstreamInfersModelFromRecentSession(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, ".codex", "config.toml")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create codex dir: %v", err)
	}
	if err := os.WriteFile(
		configPath,
		[]byte("[projects.\"/tmp/project\"]\ntrust_level = \"trusted\"\n"),
		0644,
	); err != nil {
		t.Fatalf("failed to write codex config: %v", err)
	}

	sessionPath := filepath.Join(
		home,
		".codex",
		"sessions",
		"2026",
		"04",
		"04",
		"rollout.jsonl",
	)
	if err := os.MkdirAll(filepath.Dir(sessionPath), 0755); err != nil {
		t.Fatalf("failed to create codex session dir: %v", err)
	}
	if err := os.WriteFile(
		sessionPath,
		[]byte("{\"payload\":{\"model\":\"openai/gpt-5.4\"}}\n"),
		0644,
	); err != nil {
		t.Fatalf("failed to write codex session file: %v", err)
	}

	jwtPayload := base64.RawURLEncoding.EncodeToString([]byte(`{"exp":1893456000,"https://api.openai.com/auth":{"chatgpt_account_id":"acct-test"}}`))
	accessToken := "header." + jwtPayload + ".sig"
	authJSON := `{
		"auth_mode":"chatgpt",
		"tokens":{
			"access_token":"` + accessToken + `",
			"refresh_token":"refresh-token"
		}
	}`
	if err := os.WriteFile(
		filepath.Join(home, ".codex", "auth.json"),
		[]byte(authJSON),
		0644,
	); err != nil {
		t.Fatalf("failed to write codex auth file: %v", err)
	}

	upstream, err := parseCodexManagedGatewayUpstream(
		AgentConfig{Name: "Codex CLI", ConfigPath: configPath},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected Codex upstream to resolve from session history")
	}
	if upstream.ManagedModelAlias != "openai/gpt-5.4" {
		t.Fatalf("expected model inferred from session history, got %#v", upstream.ManagedModelAlias)
	}
	if len(upstream.Notes) == 0 || !strings.Contains(upstream.Notes[0], "recent session history") {
		t.Fatalf("expected session-history note, got %#v", upstream.Notes)
	}
}

func TestEnrichDiscoveredAgentMarksManagedConfigDrift(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, ".claude", "settings.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create claude dir: %v", err)
	}
	currentConfig := `{
  "servers": {
    "preloop": {
      "url": "https://preloop.example/mcp/v1",
      "transport": "http-streaming",
      "headers": {
        "Authorization": "Bearer durable-token"
      }
    },
    "github": {
      "url": "https://github.example/mcp"
    }
  }
}`
	if err := os.WriteFile(configPath, []byte(currentConfig), 0644); err != nil {
		t.Fatalf("failed to write current config: %v", err)
	}
	state := &localEnrollmentState{
		AgentName:          "Claude Code",
		DisplayName:        "Claude",
		RuntimePrincipalID: "claude-123",
		ConfigPath:         configPath,
		BackupPath:         filepath.Join(home, "backup.json"),
		ManagedServerName:  "preloop",
		ManagedServerURL:   "https://preloop.example/mcp/v1",
		AppliedAt:          time.Now().UTC(),
		ManagedConfig: map[string]interface{}{
			"servers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"url":       "https://preloop.example/mcp/v1",
					"transport": "http-streaming",
					"headers": map[string]interface{}{
						"Authorization": "<redacted>",
					},
				},
			},
		},
	}
	if err := saveLocalEnrollmentState(state); err != nil {
		t.Fatalf("failed to seed local enrollment state: %v", err)
	}

	enriched, err := enrichDiscoveredAgent(
		AgentConfig{Name: "Claude Code", ConfigPath: configPath},
		nil,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !enriched.IsOnboarded {
		t.Fatalf("expected discovered agent to be marked onboarded, got %#v", enriched)
	}
	if !enriched.ConfigDrift {
		t.Fatalf("expected config drift to be detected, got %#v", enriched)
	}
	if !enriched.ReonboardRecommended {
		t.Fatalf("expected reonboard recommendation, got %#v", enriched)
	}
	if enriched.OnboardingState != "mcp_proxy_only" {
		t.Fatalf("expected mcp_proxy_only state, got %#v", enriched)
	}
	if len(enriched.DriftReasons) == 0 {
		t.Fatalf("expected drift reasons, got %#v", enriched)
	}
}

func TestFilterAgentsPendingEnrollmentSkipsRemoteManagedAgent(t *testing.T) {
	managedAgent := managedAgentListResponse{
		Items: []managedAgentSummary{
			{
				ID:                "agent-1",
				DisplayName:       "OpenClaw",
				SessionSourceType: "openclaw",
				SessionSourceID:   runtimePrincipalIDForAgent(AgentConfig{Name: "OpenClaw", ConfigPath: "/tmp/openclaw.json"}),
				LifecycleState:    "active",
				OnboardingState:   "fully_onboarded",
			},
		},
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(managedAgent)
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	discovered := []AgentConfig{
		{Name: "OpenClaw", ConfigPath: "/tmp/openclaw.json"},
		{Name: "Codex CLI", ConfigPath: "/tmp/codex.json"},
	}

	candidates, err := filterAgentsPendingEnrollment(client, discovered)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(candidates) != 1 || candidates[0].Name != "Codex CLI" {
		t.Fatalf("expected only the unenrolled agent to remain, got %#v", candidates)
	}
}

func TestFilterAgentsPendingEnrollmentKeepsIncompleteRemoteManagedAgent(t *testing.T) {
	managedAgent := managedAgentListResponse{
		Items: []managedAgentSummary{
			{
				ID:                "agent-1",
				DisplayName:       "Codex CLI",
				SessionSourceType: "codex",
				SessionSourceID:   runtimePrincipalIDForAgent(AgentConfig{Name: "Codex CLI", ConfigPath: "/tmp/codex.toml"}),
				LifecycleState:    "active",
				OnboardingState:   "incomplete",
			},
		},
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(managedAgent)
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	discovered := []AgentConfig{
		{Name: "Codex CLI", ConfigPath: "/tmp/codex.toml"},
	}

	candidates, err := filterAgentsPendingEnrollment(client, discovered)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(candidates) != 1 || candidates[0].Name != "Codex CLI" {
		t.Fatalf("expected incomplete remote agent to be re-onboarded, got %#v", candidates)
	}
}

func TestGetManagedAgentForDiscoveredFallsBackToLegacyDesktopAgentSourceType(
	t *testing.T,
) {
	tests := []struct {
		name      string
		agentName string
	}{
		{name: "Gemini CLI", agentName: "Gemini CLI"},
		{name: "OpenCode", agentName: "OpenCode"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			agent := AgentConfig{Name: tc.agentName, ConfigPath: "/tmp/config.json"}
			expectedID := runtimePrincipalIDForAgent(agent)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(w).Encode(managedAgentListResponse{
					Items: []managedAgentSummary{
						{
							ID:                "agent-legacy",
							DisplayName:       tc.agentName,
							SessionSourceType: "desktop_agent",
							SessionSourceID:   expectedID,
							LifecycleState:    "active",
						},
					},
				})
			}))
			defer server.Close()

			client := api.NewClientWithToken(server.URL, "tok")
			managed, err := getManagedAgentForDiscovered(client, agent)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if managed.ID != "agent-legacy" {
				t.Fatalf("expected legacy desktop agent match, got %#v", managed)
			}
		})
	}
}

func TestFilterAgentsPendingEnrollmentKeepsReonboardCandidate(t *testing.T) {
	candidates, err := filterAgentsPendingEnrollment(nil, []AgentConfig{
		{
			Name:                 "OpenClaw",
			ConfigPath:           "/tmp/openclaw.json",
			IsOnboarded:          true,
			ReonboardRecommended: true,
		},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(candidates) != 1 || !candidates[0].ReonboardRecommended {
		t.Fatalf("expected reonboard candidate to remain, got %#v", candidates)
	}
}

func TestPromptToOnboardCandidates_DeclineContinuesToNextAgent(t *testing.T) {
	input := strings.NewReader("n\ny\n\n")
	output := &bytes.Buffer{}
	candidates := []AgentConfig{
		{Name: "OpenClaw", ConfigPath: "/tmp/openclaw.json"},
		{Name: "Codex CLI", ConfigPath: "/tmp/codex.json"},
	}

	var enrolled []AgentConfig
	_, err := promptToOnboardCandidates(
		input,
		output,
		candidates,
		false,
		false,
		func(agent AgentConfig, _ bool) error {
			enrolled = append(enrolled, agent)
			return nil
		},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(enrolled) != 1 || enrolled[0].Name != "Codex CLI" {
		t.Fatalf("expected only the second agent to be enrolled, got %#v", enrolled)
	}
	if enrolled[0].DisplayName != "Codex CLI" {
		t.Fatalf("expected default confirmed name for second agent, got %#v", enrolled[0])
	}
	rendered := output.String()
	if !strings.Contains(rendered, "Onboard OpenClaw (OpenClaw) into managed Preloop access now?") {
		t.Fatalf("expected first prompt in output, got %q", rendered)
	}
	if !strings.Contains(rendered, "Onboard Codex CLI (Codex CLI) into managed Preloop access now?") {
		t.Fatalf("expected second prompt in output, got %q", rendered)
	}
	if !strings.Contains(rendered, "Agent name [Codex CLI]: ") {
		t.Fatalf("expected name prompt in output, got %q", rendered)
	}
}

func TestPromptToOnboardCandidates_AsksApprovalsForSupportedAgents(t *testing.T) {
	// OpenClaw: confirm + name (no approvals question — its hook ships via the
	// runtime plugin, not the local adapter). Codex CLI: confirm + name +
	// approvals yes.
	input := strings.NewReader("y\n\ny\n\ny\n")
	output := &bytes.Buffer{}
	candidates := []AgentConfig{
		{Name: "OpenClaw", ConfigPath: "/tmp/openclaw.json"},
		{Name: "Codex CLI", ConfigPath: "/tmp/codex.toml"},
	}

	approvalsByAgent := map[string]bool{}
	_, err := promptToOnboardCandidates(
		input,
		output,
		candidates,
		false,
		true,
		func(agent AgentConfig, approvals bool) error {
			approvalsByAgent[agent.Name] = approvals
			return nil
		},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if approvalsByAgent["OpenClaw"] {
		t.Fatal("expected no approvals opt-in for unsupported agent")
	}
	if !approvalsByAgent["Codex CLI"] {
		t.Fatal("expected approvals opt-in for Codex CLI")
	}
	rendered := output.String()
	if strings.Count(rendered, "through Preloop approvals?") != 1 {
		t.Fatalf("expected exactly one approvals question, got %q", rendered)
	}
}

func TestPromptForApprovalsOptIn(t *testing.T) {
	t.Run("default yes for supported agent", func(t *testing.T) {
		output := &bytes.Buffer{}
		optIn, err := promptForApprovalsOptIn(
			strings.NewReader("\n"),
			output,
			AgentConfig{Name: "Claude Code", ConfigPath: "/tmp/settings.json"},
		)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if !optIn {
			t.Fatal("expected empty answer to default to yes")
		}
		if !strings.Contains(output.String(), "Route Claude Code's native tool calls") {
			t.Fatalf("expected approvals prompt in output, got %q", output.String())
		}
	})

	t.Run("explicit no is honored", func(t *testing.T) {
		optIn, err := promptForApprovalsOptIn(
			strings.NewReader("n\n"),
			&bytes.Buffer{},
			AgentConfig{Name: "Cursor", ConfigPath: "/tmp/mcp.json"},
		)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if optIn {
			t.Fatal("expected explicit no to decline the hook")
		}
	})

	t.Run("unsupported agent never prompts", func(t *testing.T) {
		output := &bytes.Buffer{}
		optIn, err := promptForApprovalsOptIn(
			strings.NewReader(""),
			output,
			AgentConfig{Name: "Gemini CLI", ConfigPath: "/tmp/settings.json"},
		)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if optIn {
			t.Fatal("expected unsupported agent to skip the hook")
		}
		if output.Len() != 0 {
			t.Fatalf("expected no prompt output, got %q", output.String())
		}
	})
}

func TestShouldPromptForEnrollmentName(t *testing.T) {
	cases := []struct {
		name string
		opts managedEnrollmentOptions
		want bool
	}{
		{
			name: "interactive single-agent onboard prompts once",
			opts: managedEnrollmentOptions{},
			want: true,
		},
		{
			name: "auto-approve never prompts",
			opts: managedEnrollmentOptions{AutoApprove: true},
			want: false,
		},
		{
			name: "skip-confirmation never prompts",
			opts: managedEnrollmentOptions{SkipConfirmation: true},
			want: false,
		},
		{
			// Regression: the discover/onboard batch loops prepare the agent
			// (name prompt) before calling executeManagedEnrollment; the
			// enrollment must not ask for the agent name a second time even
			// when its own confirmation prompts stay interactive.
			name: "already-prepared agent is not re-prompted",
			opts: managedEnrollmentOptions{AgentPrepared: true},
			want: false,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := shouldPromptForEnrollmentName(tc.opts); got != tc.want {
				t.Fatalf("shouldPromptForEnrollmentName(%+v) = %v, want %v", tc.opts, got, tc.want)
			}
		})
	}
}

func TestPrepareAgentForEnrollment_AllowsEditingAgentName(t *testing.T) {
	agent, err := prepareAgentForEnrollment(
		bufio.NewReader(strings.NewReader("Octavia\n")),
		&bytes.Buffer{},
		AgentConfig{Name: "OpenClaw", ConfigPath: "/tmp/openclaw.json"},
		false,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if agent.DisplayName != "Octavia" {
		t.Fatalf("expected edited display name, got %#v", agent)
	}
	// Display name is decoupled from identity under v2.
	wantID := stableRuntimePrincipalIDForAgent(
		AgentConfig{Name: "OpenClaw", ConfigPath: "/tmp/openclaw.json"},
		"",
	)
	if agent.RuntimePrincipalID != wantID {
		t.Fatalf("expected v2 principal id %q, got %q", wantID, agent.RuntimePrincipalID)
	}
	if !strings.HasPrefix(agent.RuntimePrincipalID, "openclaw-") {
		t.Fatalf("expected source-type v2 prefix, got %q", agent.RuntimePrincipalID)
	}
}

func TestInferAgentDisplayNameFromIdentityFile(t *testing.T) {
	dir := t.TempDir()
	configDir := filepath.Join(dir, ".openclaw")
	if err := os.MkdirAll(configDir, 0755); err != nil {
		t.Fatalf("failed to create config dir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "IDENTITY.md"), []byte("# Octavia\n\nMission details"), 0644); err != nil {
		t.Fatalf("failed to write identity file: %v", err)
	}
	agent := AgentConfig{
		Name:       "OpenClaw",
		ConfigPath: filepath.Join(configDir, "openclaw.json"),
	}
	if got := inferAgentDisplayName(agent); got != "Octavia" {
		t.Fatalf("expected inferred name Octavia, got %q", got)
	}
}

func TestPrepareAgentForRemoteServerSync_RestoresDiscoveredServersFromLocalState(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	agent := AgentConfig{
		Name:               "OpenClaw",
		DisplayName:        "Octavia",
		RuntimePrincipalID: "octavia-123456789abc",
		ConfigPath:         filepath.Join(home, ".openclaw", "openclaw.json"),
		MCPServers: map[string]MCPDef{
			"preloop": {
				URL:       "https://preloop.example/mcp/v1",
				Transport: "http",
				Headers: map[string]string{
					"Authorization": "Bearer durable-token",
				},
			},
		},
	}

	if err := saveLocalEnrollmentState(&localEnrollmentState{
		AgentName:          agent.Name,
		DisplayName:        agent.DisplayName,
		RuntimePrincipalID: runtimePrincipalIDForAgent(agent),
		ConfigPath:         agent.ConfigPath,
		BackupPath:         filepath.Join(home, "backup.json"),
		ManagedServerName:  "preloop",
		ManagedServerURL:   "https://preloop.example/mcp/v1",
		AppliedAt:          time.Now().UTC(),
		DiscoveredConfig: map[string]interface{}{
			"mcp": map[string]interface{}{
				"servers": map[string]interface{}{
					"github": map[string]interface{}{
						"url":       "https://github.example/mcp",
						"transport": "http",
					},
				},
			},
		},
	}); err != nil {
		t.Fatalf("failed to save local enrollment state: %v", err)
	}

	prepared := prepareAgentForRemoteServerSync(agent, "https://preloop.example")
	if _, ok := prepared.MCPServers["github"]; !ok {
		t.Fatalf("expected recovered github server, got %#v", prepared.MCPServers)
	}
	if _, ok := prepared.MCPServers["preloop"]; ok {
		t.Fatalf("expected managed preloop proxy to be excluded from recovered servers, got %#v", prepared.MCPServers)
	}
}

func TestCreateLocalEnrollmentBackup_ReusesOriginalBackupState(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	agent := AgentConfig{
		Name:        "OpenClaw",
		DisplayName: "Claw",
		ConfigPath:  filepath.Join(home, ".openclaw", "openclaw.json"),
	}
	backupDir := filepath.Join(home, ".preloop", "agents", "backups", runtimePrincipalIDForAgent(agent))
	if err := os.MkdirAll(backupDir, 0o755); err != nil {
		t.Fatalf("failed to create backup dir: %v", err)
	}
	backupPath := filepath.Join(backupDir, "original-openclaw.json")
	originalBackup := []byte(`{"models":{"providers":{"bedrock":{"apiKey":"upstream"}}}}`)
	if err := os.WriteFile(backupPath, originalBackup, 0o600); err != nil {
		t.Fatalf("failed to write backup file: %v", err)
	}

	discovered := map[string]interface{}{
		"models": map[string]interface{}{
			"providers": map[string]interface{}{
				"bedrock": map[string]interface{}{"baseUrl": "https://bedrock-runtime.us-east-1.amazonaws.com"},
			},
		},
	}
	if err := saveLocalEnrollmentState(&localEnrollmentState{
		AgentName:          agent.Name,
		DisplayName:        agent.DisplayName,
		RuntimePrincipalID: runtimePrincipalIDForAgent(agent),
		ConfigPath:         agent.ConfigPath,
		BackupPath:         backupPath,
		ManagedServerName:  "preloop",
		ManagedServerURL:   "https://preloop.example/mcp/v1",
		AppliedAt:          time.Now().UTC(),
		DiscoveredConfig:   discovered,
		ManagedConfig: map[string]interface{}{
			"models": map[string]interface{}{"providers": map[string]interface{}{"preloop": map[string]interface{}{"apiKey": "<redacted>"}}},
		},
	}); err != nil {
		t.Fatalf("failed to seed local enrollment state: %v", err)
	}

	plan := managedMCPEnrollmentPlan{
		ManagedServerName: "preloop",
		ManagedServerURL:  "https://preloop.example/mcp/v1",
		SanitizedDiscovered: map[string]interface{}{
			"models": map[string]interface{}{"providers": map[string]interface{}{"preloop": map[string]interface{}{"apiKey": "<redacted>"}}},
		},
		SanitizedManaged: map[string]interface{}{
			"models": map[string]interface{}{"providers": map[string]interface{}{"preloop": map[string]interface{}{"apiKey": "<redacted>"}}},
		},
	}

	state, err := createLocalEnrollmentBackup(agent, true, []byte(`{"managed":true}`), plan)
	if err != nil {
		t.Fatalf("createLocalEnrollmentBackup returned error: %v", err)
	}
	if state.BackupPath != backupPath {
		t.Fatalf("expected original backup path %q, got %q", backupPath, state.BackupPath)
	}
	if _, ok := state.DiscoveredConfig["models"].(map[string]interface{})["providers"].(map[string]interface{})["bedrock"]; !ok {
		t.Fatalf("expected original discovered config to be preserved, got %#v", state.DiscoveredConfig)
	}
	gotBackup, err := os.ReadFile(backupPath)
	if err != nil {
		t.Fatalf("failed to read preserved backup: %v", err)
	}
	if string(gotBackup) != string(originalBackup) {
		t.Fatalf("expected backup file to remain unchanged, got %q", string(gotBackup))
	}
}

func TestBuildManagedMCPEnrollmentPlan_AddsPreloopAndRedactsSecrets(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "settings.json")
	if err := os.WriteFile(configPath, []byte(`{
  "mcpServers": {
    "github": {
      "url": "https://github.example/mcp",
      "headers": {
        "Authorization": "Bearer upstream-secret"
      }
    }
  }
}`), 0644); err != nil {
		t.Fatalf("failed to write config: %v", err)
	}

	plan, err := buildManagedMCPEnrollmentPlan(AgentConfig{
		Name:       "Gemini CLI",
		ConfigPath: configPath,
		MCPServers: map[string]MCPDef{},
	}, "https://preloop.example", "durable-secret-token")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	managedServers := plan.ManagedDocument["mcpServers"].(map[string]interface{})
	preloop := managedServers["preloop"].(map[string]interface{})
	if preloop["type"] != "http" {
		t.Fatalf("expected Gemini managed MCP type http, got %+v", preloop)
	}
	headers := preloop["headers"].(map[string]interface{})
	if headers["Authorization"] != "Bearer durable-secret-token" {
		t.Fatalf("expected durable token in managed config, got %+v", headers)
	}

	sanitizedServers := plan.SanitizedManaged["mcpServers"].(map[string]interface{})
	sanitizedPreloop := sanitizedServers["preloop"].(map[string]interface{})
	sanitizedHeaders := sanitizedPreloop["headers"].(map[string]interface{})
	if sanitizedHeaders["Authorization"] != "<redacted>" {
		t.Fatalf("expected redacted managed auth header, got %+v", sanitizedHeaders)
	}

	sanitizedExisting := sanitizedServers["github"].(map[string]interface{})
	existingHeaders := sanitizedExisting["headers"].(map[string]interface{})
	if existingHeaders["Authorization"] != "<redacted>" {
		t.Fatalf("expected redacted upstream auth header, got %+v", existingHeaders)
	}
}

func TestBuildManagedMCPEnrollmentPlan_DisablesStalePreloopServers(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "settings.json")
	if err := os.WriteFile(configPath, []byte(`{
  "mcpServers": {
    "review": {
      "url": "https://ai-model-gateway.review.preloop.ai/mcp/v1"
    },
    "preloop": {
      "url": "https://old.example/mcp/v1"
    },
    "github": {
      "url": "https://github.example/mcp"
    }
  }
}`), 0644); err != nil {
		t.Fatalf("failed to write config: %v", err)
	}

	plan, err := buildManagedMCPEnrollmentPlan(AgentConfig{
		Name:       "Gemini CLI",
		ConfigPath: configPath,
	}, "https://preloop.example", "durable-secret-token")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	managedServers := plan.ManagedDocument["mcpServers"].(map[string]interface{})
	if _, ok := managedServers["review"]; ok {
		t.Fatalf("expected stale preloop.ai MCP server to be disabled, got %#v", managedServers)
	}
	if _, ok := managedServers["github"]; !ok {
		t.Fatalf("expected non-Preloop MCP server to remain, got %#v", managedServers)
	}
	preloop := managedServers["preloop"].(map[string]interface{})
	if preloop["url"] != "https://preloop.example/mcp/v1" {
		t.Fatalf("expected current managed Preloop MCP URL, got %#v", preloop)
	}
	if len(plan.Notes) == 0 || !strings.Contains(plan.Notes[0], "Disabled existing Preloop MCP entries") {
		t.Fatalf("expected notice about disabled Preloop MCP entries, got %#v", plan.Notes)
	}
}

func TestBuildManagedRemoteServerRequestSkipsPreloopOwnedServers(t *testing.T) {
	for name, server := range map[string]MCPDef{
		"preloop": {URL: "https://external.example/mcp"},
		"review":  {URL: "https://ai-model-gateway.review.preloop.ai/mcp/v1"},
		"bare":    {URL: "ai-model-gateway.review.preloop.ai/mcp/v1"},
	} {
		request, warning, _, ok := buildManagedRemoteServerRequest(name, server)
		if ok || request != nil {
			t.Fatalf("expected %q to be skipped, got ok=%v request=%#v", name, ok, request)
		}
		if !strings.Contains(warning, "skipped") {
			t.Fatalf("expected skip warning for %q, got %q", name, warning)
		}
	}
}

func TestBuildManagedMCPEnrollmentPlan_OpenClawUsesStreamableHTTPServer(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "openclaw.json")
	if err := os.WriteFile(configPath, []byte(`{
  "mcp": {
    "servers": {
      "github": {
        "url": "https://github.example/mcp",
        "transport": "http"
      }
    }
  }
}`), 0644); err != nil {
		t.Fatalf("failed to write config: %v", err)
	}

	plan, err := buildManagedMCPEnrollmentPlan(AgentConfig{
		Name:       "OpenClaw",
		ConfigPath: configPath,
	}, "https://preloop.example", "openclaw-durable-token")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	mcp := plan.ManagedDocument["mcp"].(map[string]interface{})
	servers := mcp["servers"].(map[string]interface{})
	preloop := servers["preloop"].(map[string]interface{})
	if preloop["transport"] != "streamable-http" {
		t.Fatalf("expected OpenClaw transport streamable-http, got %+v", preloop)
	}
	if preloop["url"] != "https://preloop.example/mcp/v1" {
		t.Fatalf("unexpected OpenClaw managed URL: %+v", preloop)
	}
	headers := preloop["headers"].(map[string]interface{})
	if headers["Authorization"] != "Bearer openclaw-durable-token" {
		t.Fatalf("unexpected OpenClaw auth header: %+v", headers)
	}
}

func TestBuildManagedMCPEnrollmentPlan_CodexCLIWritesSingleMCPServersBlock(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config.toml")
	if err := os.WriteFile(configPath, []byte(`
[projects."/tmp/workspace"]
trust_level = "trusted"
`), 0644); err != nil {
		t.Fatalf("failed to write codex config: %v", err)
	}

	plan, err := buildManagedMCPEnrollmentPlan(AgentConfig{
		Name:       "Codex CLI",
		ConfigPath: configPath,
	}, "https://preloop.example", "codex-durable-token")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Codex 0.142.5 only reads the top-level [mcp_servers] table, so onboarding
	// must produce exactly one preloop MCP block there and never write a bogus
	// [mcp.servers] table.
	if _, ok := plan.ManagedDocument["mcp"]; ok {
		t.Fatalf("expected no [mcp] table for Codex, got %#v", plan.ManagedDocument["mcp"])
	}

	servers, ok := plan.ManagedDocument["mcp_servers"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected [mcp_servers] table for Codex, got %#v", plan.ManagedDocument)
	}
	preloop, ok := servers["preloop"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected [mcp_servers.preloop] entry for Codex, got %#v", servers)
	}
	if preloop["url"] != "https://preloop.example/mcp/v1" {
		t.Fatalf("unexpected Codex managed URL: %+v", preloop)
	}
	if _, hasTransport := preloop["transport"]; hasTransport {
		t.Fatalf("Codex entry must not carry a transport field, got %+v", preloop)
	}
	if _, hasAuth := preloop["auth"]; hasAuth {
		t.Fatalf("Codex entry must not carry an [auth] sub-table, got %+v", preloop)
	}
	headers, ok := preloop["http_headers"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected Codex http_headers, got %+v", preloop)
	}
	if headers["Authorization"] != "Bearer codex-durable-token" {
		t.Fatalf("unexpected Codex Authorization header: %+v", headers)
	}
}

func TestWriteCodexConfigKeepsModelAvailabilityNUXAsInteger(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config.toml")
	agent := AgentConfig{Name: "Codex CLI", ConfigPath: configPath}
	doc := map[string]interface{}{
		"tui": map[string]interface{}{
			"model_availability_nux": map[string]interface{}{
				"gpt-5.5": float64(1),
			},
		},
	}

	if err := writeAgentConfigDocument(agent, doc); err != nil {
		t.Fatalf("failed to write Codex config: %v", err)
	}

	data, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatalf("failed to read Codex config: %v", err)
	}
	body := string(data)
	if strings.Contains(body, "gpt-5.5' = 1.0") ||
		strings.Contains(body, "\"gpt-5.5\" = 1.0") {
		t.Fatalf("expected Codex NUX value to be serialized as an integer, got:\n%s", body)
	}
	if !strings.Contains(body, "gpt-5.5' = 1") &&
		!strings.Contains(body, "\"gpt-5.5\" = 1") {
		t.Fatalf("expected Codex NUX value to remain present, got:\n%s", body)
	}
}

func TestRestoreCodexGatewaySelectionFromOriginalPreservesMCP(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config.toml")
	original := []byte(`
model = "gpt-5.5"

[projects."/tmp/workspace"]
trust_level = "trusted"
`)
	if err := os.WriteFile(configPath, original, 0644); err != nil {
		t.Fatalf("failed to seed Codex config: %v", err)
	}
	agent := AgentConfig{Name: "Codex CLI", ConfigPath: configPath}
	doc, err := loadAgentConfigDocument(agent)
	if err != nil {
		t.Fatalf("failed to load Codex config: %v", err)
	}
	doc["model"] = "openai/gpt-5.4"
	doc["model_provider"] = "preloop"
	doc["model_providers"] = map[string]interface{}{
		"preloop": map[string]interface{}{
			"base_url":                  "https://preloop.example/openai/v1",
			"experimental_bearer_token": "codex-durable-token",
		},
	}
	doc["mcp"] = map[string]interface{}{
		"servers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"url": "https://preloop.example/mcp/v1",
			},
		},
	}
	if err := writeAgentConfigDocument(agent, doc); err != nil {
		t.Fatalf("failed to write managed Codex config: %v", err)
	}

	if err := restoreCodexGatewaySelectionFromOriginal(agent, original, io.Discard); err != nil {
		t.Fatalf("restoreCodexGatewaySelectionFromOriginal returned error: %v", err)
	}

	restored, err := loadAgentConfigDocument(agent)
	if err != nil {
		t.Fatalf("failed to load restored Codex config: %v", err)
	}
	if restored["model"] != "gpt-5.5" {
		t.Fatalf("expected original Codex model restored, got %#v", restored["model"])
	}
	if _, exists := restored["model_provider"]; exists {
		t.Fatalf("expected Preloop model_provider removed, got %#v", restored)
	}
	if _, exists := restored["model_providers"]; exists {
		t.Fatalf("expected Preloop model_providers removed, got %#v", restored)
	}
	if _, ok := restored["mcp"].(map[string]interface{})["servers"].(map[string]interface{})["preloop"]; !ok {
		t.Fatalf("expected Preloop MCP server to remain present, got %#v", restored)
	}
}

func TestRestoreCodexGatewaySelectionStripsPreloopFromContaminatedBackup(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config.toml")
	contaminatedOriginal := []byte(`
model = "openai/gpt-5.4"
model_provider = "preloop"

[model_providers.preloop]
base_url = "https://preloop.example/openai/v1"
experimental_bearer_token = "codex-durable-token"
`)
	if err := os.WriteFile(configPath, contaminatedOriginal, 0644); err != nil {
		t.Fatalf("failed to seed Codex config: %v", err)
	}
	agent := AgentConfig{Name: "Codex CLI", ConfigPath: configPath}

	if err := restoreCodexGatewaySelectionFromOriginal(
		agent,
		contaminatedOriginal,
		io.Discard,
	); err != nil {
		t.Fatalf("restoreCodexGatewaySelectionFromOriginal returned error: %v", err)
	}

	restored, err := loadAgentConfigDocument(agent)
	if err != nil {
		t.Fatalf("failed to load restored Codex config: %v", err)
	}
	if restored["model"] != "gpt-5.4" {
		t.Fatalf("expected direct Codex model restored, got %#v", restored["model"])
	}
	if _, exists := restored["model_provider"]; exists {
		t.Fatalf("expected contaminated Preloop model_provider removed, got %#v", restored)
	}
	if _, exists := restored["model_providers"]; exists {
		t.Fatalf("expected contaminated Preloop model_providers removed, got %#v", restored)
	}
}

func TestRemoveCodexManagedGatewaySelectionCleansPlan(t *testing.T) {
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{
			"model":          "openai/gpt-5.4",
			"model_provider": "preloop",
			"model_providers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"base_url": "https://preloop.example/openai/v1",
				},
			},
			"mcp": map[string]interface{}{
				"servers": map[string]interface{}{
					"preloop": map[string]interface{}{
						"url": "https://preloop.example/mcp/v1",
					},
				},
			},
		},
	}

	cleaned, err := removeCodexManagedGatewaySelection(plan)
	if err != nil {
		t.Fatalf("removeCodexManagedGatewaySelection returned error: %v", err)
	}

	if cleaned.ManagedDocument["model"] != "gpt-5.4" {
		t.Fatalf("expected direct Codex model, got %#v", cleaned.ManagedDocument["model"])
	}
	if _, exists := cleaned.ManagedDocument["model_provider"]; exists {
		t.Fatalf("expected model_provider removed, got %#v", cleaned.ManagedDocument)
	}
	if _, exists := cleaned.ManagedDocument["model_providers"]; exists {
		t.Fatalf("expected model_providers removed, got %#v", cleaned.ManagedDocument)
	}
	if cleaned.ManagedModelAlias != "" || cleaned.ManagedProviderName != "" {
		t.Fatalf("expected managed model metadata cleared, got %#v", cleaned)
	}
	if _, ok := cleaned.ManagedDocument["mcp"].(map[string]interface{})["servers"].(map[string]interface{})["preloop"]; !ok {
		t.Fatalf("expected Preloop MCP server to remain present, got %#v", cleaned.ManagedDocument)
	}
}

func TestBuildManagedMCPEnrollmentPlan_OpenCodeAllowsMissingConfig(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, ".config", "opencode", "config.json")

	plan, err := buildManagedMCPEnrollmentPlan(AgentConfig{
		Name:       "OpenCode",
		ConfigPath: configPath,
	}, "https://preloop.example", "opencode-durable-token")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	preloop := plan.ManagedDocument["mcp"].(map[string]interface{})["preloop"].(map[string]interface{})
	headers := preloop["headers"].(map[string]interface{})
	if preloop["type"] != "remote" {
		t.Fatalf("unexpected OpenCode managed server type: %+v", preloop)
	}
	if preloop["url"] != "https://preloop.example/mcp/v1" {
		t.Fatalf("unexpected OpenCode managed URL: %+v", preloop)
	}
	if headers["Authorization"] != "Bearer opencode-durable-token" {
		t.Fatalf("unexpected OpenCode auth header: %+v", headers)
	}
}

func TestParseServerMapFromDocumentSupportsOpenCodeRootMCP(t *testing.T) {
	servers := parseServerMapFromDocument(map[string]interface{}{
		"mcp": map[string]interface{}{
			"preloop": map[string]interface{}{
				"type": "remote",
				"url":  "https://preloop.example/mcp/v1",
				"headers": map[string]interface{}{
					"Authorization": "Bearer durable-token",
				},
			},
		},
	})
	preloop, ok := servers["preloop"]
	if !ok {
		t.Fatalf("expected preloop server to be parsed from root mcp map, got %#v", servers)
	}
	if preloop.URL != "https://preloop.example/mcp/v1" {
		t.Fatalf("unexpected parsed OpenCode URL: %+v", preloop)
	}
	if preloop.Transport != "remote" {
		t.Fatalf("expected OpenCode transport to reflect type, got %+v", preloop)
	}
	if preloop.Headers["Authorization"] != "Bearer durable-token" {
		t.Fatalf("unexpected parsed OpenCode headers: %+v", preloop)
	}
}

func TestParseServerMapFromDocumentSupportsGeminiHTTPType(t *testing.T) {
	servers := parseServerMapFromDocument(map[string]interface{}{
		"mcpServers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"type": "http",
				"url":  "https://preloop.example/mcp/v1",
				"headers": map[string]interface{}{
					"Authorization": "Bearer durable-token",
				},
			},
		},
	})
	preloop, ok := servers["preloop"]
	if !ok {
		t.Fatalf("expected preloop server to be parsed from Gemini settings, got %#v", servers)
	}
	if preloop.URL != "https://preloop.example/mcp/v1" {
		t.Fatalf("unexpected parsed Gemini URL: %+v", preloop)
	}
	if preloop.Transport != "http" {
		t.Fatalf("expected Gemini transport to reflect type, got %+v", preloop)
	}
}

func TestGenericAdapterValidateManagedConfigSupportsCodexAuthBlock(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: "Codex CLI"})
	result := adapter.ValidateManagedConfig(map[string]interface{}{
		"mcp": map[string]interface{}{
			"servers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"url":       "https://preloop.example/mcp/v1",
					"transport": "http",
					"auth": map[string]interface{}{
						"type":  "bearer",
						"token": "durable-token",
					},
				},
			},
		},
		"model_provider": "preloop",
		"model":          "openai/gpt-5.4",
		"model_providers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"base_url":                  "https://preloop.example/openai/v1",
				"experimental_bearer_token": "durable-token",
				"wire_api":                  "responses",
			},
		},
	}, "https://preloop.example")
	if result["validation_passed"] != true {
		t.Fatalf("expected codex validation to pass, got %+v", result)
	}
}

func TestApplyCodexManagedGatewayConfiguresCustomProvider(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, ".codex", "config.toml")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create codex dir: %v", err)
	}
	if err := os.WriteFile(configPath, []byte(""), 0644); err != nil {
		t.Fatalf("failed to seed codex config: %v", err)
	}

	plan, err := buildManagedMCPEnrollmentPlan(AgentConfig{
		Name:       "Codex CLI",
		ConfigPath: configPath,
	}, "https://preloop.example", "codex-durable-token")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	plan, err = applyCodexManagedGateway(
		plan,
		"https://preloop.example",
		"codex-durable-token",
		"openai/gpt-5.4",
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}

	if got := plan.ManagedDocument["model_provider"]; got != "preloop" {
		t.Fatalf("unexpected codex model_provider: %#v", got)
	}
	if got := plan.ManagedDocument["model"]; got != "openai/gpt-5.4" {
		t.Fatalf("unexpected codex model: %#v", got)
	}
	providers := plan.ManagedDocument["model_providers"].(map[string]interface{})
	preloop := providers["preloop"].(map[string]interface{})
	if preloop["base_url"] != "https://preloop.example/openai/v1" {
		t.Fatalf("unexpected codex gateway base_url: %#v", preloop)
	}
	if preloop["experimental_bearer_token"] != "codex-durable-token" {
		t.Fatalf("unexpected codex gateway token: %#v", preloop)
	}
	if preloop["wire_api"] != "responses" {
		t.Fatalf("unexpected codex gateway wire_api: %#v", preloop)
	}
	legacyServers := plan.ManagedDocument["mcp_servers"].(map[string]interface{})
	legacyPreloop := legacyServers["preloop"].(map[string]interface{})
	if legacyPreloop["url"] != "https://preloop.example/mcp/v1" {
		t.Fatalf("unexpected Codex legacy MCP URL: %#v", legacyPreloop)
	}
	legacyHeaders := legacyPreloop["http_headers"].(map[string]interface{})
	if legacyHeaders["Authorization"] != "Bearer codex-durable-token" {
		t.Fatalf("unexpected Codex legacy Authorization header: %#v", legacyPreloop)
	}
}

// TestManagedServerSchemaAntigravity asserts the Antigravity MCP entry uses
// the `serverUrl` key (not `url`) with a literal bearer header, and that a
// freshly built entry round-trips through ValidateManagedConfig.
func TestManagedServerSchemaAntigravity(t *testing.T) {
	name := antigravityAgentName
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: name})
	entry := adapter.BuildManagedServer("https://preloop.example", "durable-token")
	if entry["serverUrl"] != "https://preloop.example/mcp/v1" {
		t.Fatalf("%s: expected serverUrl key, got %#v", name, entry)
	}
	if _, hasURL := entry["url"]; hasURL {
		t.Fatalf("%s: Antigravity must not use the `url` key, got %#v", name, entry)
	}
	headers, _ := entry["headers"].(map[string]interface{})
	if headers["Authorization"] != "Bearer durable-token" {
		t.Fatalf("%s: expected literal bearer header, got %#v", name, headers)
	}
	result := adapter.ValidateManagedConfig(map[string]interface{}{
		"mcpServers": map[string]interface{}{"preloop": entry},
	}, "https://preloop.example")
	if result["validation_passed"] != true {
		t.Fatalf("%s: expected antigravity validation to pass, got %+v", name, result)
	}
}

// TestManagedServerSchemaDevin asserts the Devin MCP entry uses `url` +
// headers with no transport field, and round-trips through validation.
func TestManagedServerSchemaDevin(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: devinAgentName})
	entry := adapter.BuildManagedServer("https://preloop.example", "durable-token")
	if entry["url"] != "https://preloop.example/mcp/v1" {
		t.Fatalf("expected url key for Devin, got %#v", entry)
	}
	if _, hasTransport := entry["transport"]; hasTransport {
		t.Fatalf("Devin entry must not carry a transport field, got %#v", entry)
	}
	headers, _ := entry["headers"].(map[string]interface{})
	if headers["Authorization"] != "Bearer durable-token" {
		t.Fatalf("expected literal bearer header, got %#v", headers)
	}
	result := adapter.ValidateManagedConfig(map[string]interface{}{
		"mcpServers": map[string]interface{}{"preloop": entry},
	}, "https://preloop.example")
	if result["validation_passed"] != true {
		t.Fatalf("expected devin validation to pass, got %+v", result)
	}
}

// TestManagedServerSchemaClaudeDesktop asserts the Claude Desktop MCP entry
// is an mcp-remote stdio bridge (command/args/env) — never a remote
// url/headers entry, which claude_desktop_config.json cannot express and
// which can wedge the app's MCP subsystem — and that a freshly built entry
// round-trips through ValidateManagedConfig.
func TestManagedServerSchemaClaudeDesktop(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: "Claude Desktop"})
	entry := adapter.BuildManagedServer("https://preloop.example", "durable-token")
	if entry["command"] != "npx" {
		t.Fatalf("expected npx bridge command for Claude Desktop, got %#v", entry)
	}
	for _, forbidden := range []string{"url", "transport", "headers"} {
		if _, has := entry[forbidden]; has {
			t.Fatalf("Claude Desktop entry must not carry %q (remote shape), got %#v", forbidden, entry)
		}
	}
	args := stringArgsFromConfigValue(entry["args"])
	expectedArgs := []string{
		"-y",
		"mcp-remote",
		"https://preloop.example/mcp/v1",
		"--header",
		"Authorization:${AUTH_HEADER}",
	}
	if len(args) != len(expectedArgs) {
		t.Fatalf("unexpected bridge args: %#v", args)
	}
	for i, expected := range expectedArgs {
		if args[i] != expected {
			t.Fatalf("bridge arg %d: expected %q, got %q (full: %#v)", i, expected, args[i], args)
		}
	}
	// The Authorization value lives in env (spaces intact) while the arg
	// stays space-free — the mcp-remote README workaround for Claude
	// Desktop's arg-space mangling.
	env, _ := entry["env"].(map[string]interface{})
	if env["AUTH_HEADER"] != "Bearer durable-token" {
		t.Fatalf("expected AUTH_HEADER env with bearer token, got %#v", entry)
	}
	result := adapter.ValidateManagedConfig(map[string]interface{}{
		"mcpServers": map[string]interface{}{"preloop": entry},
	}, "https://preloop.example")
	if result["validation_passed"] != true {
		t.Fatalf("expected claude desktop bridge validation to pass, got %+v", result)
	}
}

// TestClaudeDesktopValidateAcceptsJSONRoundTrippedBridge ensures validation
// still passes after the entry has been serialized to disk and reloaded
// (args become []interface{}).
func TestClaudeDesktopValidateAcceptsJSONRoundTrippedBridge(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: "Claude Desktop"})
	entry := adapter.BuildManagedServer("https://preloop.example", "durable-token")
	raw, err := json.Marshal(map[string]interface{}{
		"mcpServers": map[string]interface{}{"preloop": entry},
	})
	if err != nil {
		t.Fatalf("failed to marshal document: %v", err)
	}
	var doc map[string]interface{}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("failed to unmarshal document: %v", err)
	}
	result := adapter.ValidateManagedConfig(doc, "https://preloop.example")
	if result["validation_passed"] != true {
		t.Fatalf("expected round-tripped bridge validation to pass, got %+v", result)
	}
}

// TestClaudeDesktopValidateRejectsLegacyRemoteShape ensures the remote
// url/headers shape that bricked Claude Desktop is treated as invalid.
func TestClaudeDesktopValidateRejectsLegacyRemoteShape(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: "Claude Desktop"})
	result := adapter.ValidateManagedConfig(map[string]interface{}{
		"mcpServers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"url":       "https://preloop.example/mcp/v1",
				"transport": "http",
				"headers": map[string]interface{}{
					"Authorization": "Bearer durable-token",
				},
			},
		},
	}, "https://preloop.example")
	if result["validation_passed"] != false {
		t.Fatalf("expected legacy remote shape to fail validation, got %+v", result)
	}
	if result["transport_ok"] != false {
		t.Fatalf("expected transport_ok=false for legacy remote shape, got %+v", result)
	}
}

// TestClaudeDesktopPlanWritesBridgeWhenNpxPresent asserts onboarding writes
// the stdio bridge entry when the npx/node runtime resolves.
func TestClaudeDesktopPlanWritesBridgeWhenNpxPresent(t *testing.T) {
	original := resolveClaudeDesktopBridgeRuntime
	t.Cleanup(func() { resolveClaudeDesktopBridgeRuntime = original })
	resolveClaudeDesktopBridgeRuntime = func() error { return nil }

	configPath := filepath.Join(t.TempDir(), "claude_desktop_config.json")
	if err := os.WriteFile(configPath, []byte(`{"mcpServers": {}}`), 0o600); err != nil {
		t.Fatalf("failed to seed config: %v", err)
	}
	agent := AgentConfig{Name: "Claude Desktop", ConfigPath: configPath}
	plan, err := buildManagedMCPEnrollmentPlan(agent, "https://preloop.example", "durable-token")
	if err != nil {
		t.Fatalf("failed to build plan: %v", err)
	}
	if plan.MCPConfigSkipped {
		t.Fatalf("expected MCP config not to be skipped when npx resolves, plan: %+v", plan)
	}
	servers, _ := plan.ManagedDocument["mcpServers"].(map[string]interface{})
	preloop, _ := servers["preloop"].(map[string]interface{})
	if preloop == nil || preloop["command"] != "npx" {
		t.Fatalf("expected npx bridge entry in managed document, got %#v", plan.ManagedDocument)
	}
}

// TestClaudeDesktopPlanSkipsMCPEntryWhenNpxMissing asserts onboarding writes
// NO Preloop entry when npx is unavailable (Claude Desktop cannot read a
// remote fallback shape), prunes any broken entry left by an earlier
// onboard, and tells the user their options.
func TestClaudeDesktopPlanSkipsMCPEntryWhenNpxMissing(t *testing.T) {
	original := resolveClaudeDesktopBridgeRuntime
	t.Cleanup(func() { resolveClaudeDesktopBridgeRuntime = original })
	resolveClaudeDesktopBridgeRuntime = func() error {
		return fmt.Errorf("npx is not available on PATH")
	}

	configPath := filepath.Join(t.TempDir(), "claude_desktop_config.json")
	// Seed with the broken remote-shaped entry an earlier CLI version wrote.
	seed := `{"mcpServers": {"preloop": {"url": "https://old.example/mcp/v1", "transport": "http", "headers": {"Authorization": "Bearer stale"}}}}`
	if err := os.WriteFile(configPath, []byte(seed), 0o600); err != nil {
		t.Fatalf("failed to seed config: %v", err)
	}
	agent := AgentConfig{Name: "Claude Desktop", ConfigPath: configPath}
	plan, err := buildManagedMCPEnrollmentPlan(agent, "https://preloop.example", "durable-token")
	if err != nil {
		t.Fatalf("failed to build plan: %v", err)
	}
	if !plan.MCPConfigSkipped {
		t.Fatalf("expected MCP config to be skipped without npx, plan: %+v", plan)
	}
	servers, _ := plan.ManagedDocument["mcpServers"].(map[string]interface{})
	if _, has := servers["preloop"]; has {
		t.Fatalf("expected no preloop entry without npx, got %#v", plan.ManagedDocument)
	}
	foundOptionsNote := false
	for _, note := range plan.Notes {
		if strings.Contains(note, "Settings → Connectors") &&
			strings.Contains(note, "Node.js") &&
			strings.Contains(note, "https://preloop.example/mcp/v1") {
			foundOptionsNote = true
		}
	}
	if !foundOptionsNote {
		t.Fatalf("expected a skip note listing user options, got %#v", plan.Notes)
	}
}

// TestManagedServerSchemaCodex asserts the Codex MCP entry uses `url` +
// http_headers (the format Codex actually reads) with no transport field and
// no [auth] sub-table, and that no-token enrollment falls back to
// bearer_token_env_var.
func TestManagedServerSchemaCodex(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: "Codex CLI"})
	entry := adapter.BuildManagedServer("https://preloop.example", "durable-token")
	if entry["url"] != "https://preloop.example/mcp/v1" {
		t.Fatalf("expected url key for Codex, got %#v", entry)
	}
	if _, hasTransport := entry["transport"]; hasTransport {
		t.Fatalf("Codex entry must not carry a transport field, got %#v", entry)
	}
	if _, hasAuth := entry["auth"]; hasAuth {
		t.Fatalf("Codex entry must not carry an [auth] sub-table, got %#v", entry)
	}
	headers, _ := entry["http_headers"].(map[string]interface{})
	if headers["Authorization"] != "Bearer durable-token" {
		t.Fatalf("expected literal bearer header, got %#v", entry)
	}

	tokenless := adapter.BuildManagedServer("https://preloop.example", "")
	if _, hasHeaders := tokenless["http_headers"]; hasHeaders {
		t.Fatalf("expected no http_headers without a token, got %#v", tokenless)
	}
	if tokenless["bearer_token_env_var"] != "PRELOOP_TOKEN" {
		t.Fatalf("expected bearer_token_env_var fallback, got %#v", tokenless)
	}
}

// TestMCPOnlyAgentModelNote verifies each model-incapable agent surfaces a
// clear, distinct explanation and that gateway-capable agents get none.
func TestMCPOnlyAgentModelNote(t *testing.T) {
	for _, name := range []string{"Cursor", antigravityAgentName, devinAgentName} {
		if note := mcpOnlyAgentModelNote(AgentConfig{Name: name}); note == "" {
			t.Fatalf("expected an MCP-only model note for %s", name)
		}
	}
	if note := mcpOnlyAgentModelNote(AgentConfig{Name: "Claude Code"}); note != "" {
		t.Fatalf("expected no MCP-only note for a gateway-capable agent, got %q", note)
	}
}

func TestGenericAdapterValidateManagedConfigSupportsCodexGateway(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: "Codex CLI"})
	result := adapter.ValidateManagedConfig(map[string]interface{}{
		"mcp": map[string]interface{}{
			"servers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"url":       "https://preloop.example/mcp/v1",
					"transport": "http",
					"auth": map[string]interface{}{
						"type":  "bearer",
						"token": "durable-token",
					},
				},
			},
		},
		"model_provider": "preloop",
		"model":          "openai/gpt-5.4",
		"model_providers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"base_url":                  "https://preloop.example/openai/v1",
				"experimental_bearer_token": "durable-token",
				"wire_api":                  "responses",
			},
		},
	}, "https://preloop.example")
	if result["gateway_provider_ok"] != true || result["model_provider_rewritten"] != true {
		t.Fatalf("expected codex gateway validation to pass, got %+v", result)
	}
	if got := result["gateway_model_alias"]; got != "openai/gpt-5.4" {
		t.Fatalf("unexpected gateway model alias: %#v", got)
	}
}

func TestGenericAdapterValidateManagedConfigSupportsCodexLegacyMCPConfig(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: "Codex CLI"})
	result := adapter.ValidateManagedConfig(map[string]interface{}{
		"mcp_servers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"url":                  "https://preloop.example/mcp/v1",
				"bearer_token_env_var": "PRELOOP_TOKEN",
			},
		},
		"model_provider": "preloop",
		"model":          "openai/gpt-5.4",
		"model_providers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"base_url":                  "https://preloop.example/openai/v1",
				"experimental_bearer_token": "durable-token",
				"wire_api":                  "responses",
			},
		},
	}, "https://preloop.example")
	if result["validation_passed"] != true {
		t.Fatalf("expected legacy codex validation to pass, got %+v", result)
	}
	if result["legacy_mcp_server_present"] != true {
		t.Fatalf("expected legacy codex MCP marker, got %+v", result)
	}
}

func TestGenericAdapterValidateManagedConfigSupportsCodexLegacyMCPHeaders(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: "Codex CLI"})
	result := adapter.ValidateManagedConfig(map[string]interface{}{
		"mcp_servers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"url": "https://preloop.example/mcp/v1",
				"http_headers": map[string]interface{}{
					"Authorization": "Bearer durable-token",
				},
			},
		},
		"model_provider": "preloop",
		"model":          "openai/gpt-5.4",
		"model_providers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"base_url":                  "https://preloop.example/openai/v1",
				"experimental_bearer_token": "durable-token",
				"wire_api":                  "responses",
			},
		},
	}, "https://preloop.example")
	if result["validation_passed"] != true {
		t.Fatalf("expected header-backed legacy codex validation to pass, got %+v", result)
	}
}

func TestGenericAdapterValidateManagedConfigSupportsOpenCodeRootMCP(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: "OpenCode"})
	result := adapter.ValidateManagedConfig(map[string]interface{}{
		"mcp": map[string]interface{}{
			"preloop": map[string]interface{}{
				"type": "remote",
				"url":  "https://preloop.example/mcp/v1",
				"headers": map[string]interface{}{
					"Authorization": "Bearer durable-token",
				},
			},
		},
	}, "https://preloop.example")
	if result["validation_passed"] != true {
		t.Fatalf("expected OpenCode validation to pass, got %+v", result)
	}
}

func TestApplyOpenCodeManagedGatewayConfiguresProvider(t *testing.T) {
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{
			"mcp": map[string]interface{}{
				"preloop": map[string]interface{}{
					"type": "remote",
					"url":  "https://preloop.example/mcp/v1",
				},
			},
		},
	}
	plan, err := applyOpenCodeManagedGateway(
		plan,
		"https://preloop.example",
		"opencode-durable-token",
		"openai/gpt-5.4",
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}

	if got := plan.ManagedDocument["model"]; got != "preloop/openai/gpt-5.4" {
		t.Fatalf("unexpected OpenCode model: %#v", got)
	}
	providers := plan.ManagedDocument["provider"].(map[string]interface{})
	preloop := providers["preloop"].(map[string]interface{})
	options := preloop["options"].(map[string]interface{})
	if options["baseURL"] != "https://preloop.example/openai/v1" {
		t.Fatalf("unexpected OpenCode gateway baseURL: %#v", options)
	}
	if options["apiKey"] != "opencode-durable-token" {
		t.Fatalf("unexpected OpenCode gateway token: %#v", options)
	}
}

func TestGenericAdapterValidateManagedConfigSupportsOpenCodeGateway(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: "OpenCode"})
	result := adapter.ValidateManagedConfig(map[string]interface{}{
		"mcp": map[string]interface{}{
			"preloop": map[string]interface{}{
				"type": "remote",
				"url":  "https://preloop.example/mcp/v1",
				"headers": map[string]interface{}{
					"Authorization": "Bearer durable-token",
				},
			},
		},
		"model": "preloop/openai/gpt-5.4",
		"provider": map[string]interface{}{
			"preloop": map[string]interface{}{
				"options": map[string]interface{}{
					"baseURL": "https://preloop.example/openai/v1",
					"apiKey":  "durable-token",
				},
			},
		},
	}, "https://preloop.example")
	if result["gateway_provider_ok"] != true || result["model_provider_rewritten"] != true {
		t.Fatalf("expected OpenCode gateway validation to pass, got %+v", result)
	}
	if got := result["gateway_model_alias"]; got != "openai/gpt-5.4" {
		t.Fatalf("unexpected OpenCode gateway model alias: %#v", got)
	}
}

func TestApplyClaudeManagedGatewayConfiguresEnv(t *testing.T) {
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{
			"mcpServers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"url": "https://preloop.example/mcp/v1",
				},
			},
		},
	}
	plan, err := applyClaudeManagedGateway(
		plan,
		"https://preloop.example",
		"claude-durable-token",
		"amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0",
		nil,
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}

	env := plan.ManagedDocument["env"].(map[string]interface{})
	if env["ANTHROPIC_BASE_URL"] != "https://preloop.example/anthropic" {
		t.Fatalf("unexpected Claude base URL: %#v", env)
	}
	if env["ANTHROPIC_API_KEY"] != "claude-durable-token" {
		t.Fatalf("unexpected Claude token: %#v", env)
	}
	if _, exists := env["ANTHROPIC_AUTH_TOKEN"]; exists {
		t.Fatalf("did not expect legacy Claude auth token key: %#v", env)
	}
	if env["CLAUDE_CODE_USE_BEDROCK"] != "0" {
		t.Fatalf("expected managed Claude config to disable Bedrock mode: %#v", env)
	}
	if env["CLAUDE_CODE_SIMPLE"] != "1" {
		t.Fatalf("expected managed Claude config to enable simple mode: %#v", env)
	}
	if env["CLAUDE_CODE_ENABLE_TELEMETRY"] != "0" || env["DISABLE_TELEMETRY"] != "1" {
		t.Fatalf("expected managed Claude config to disable telemetry: %#v", env)
	}
	if env["OTEL_METRICS_EXPORTER"] != "none" ||
		env["OTEL_LOGS_EXPORTER"] != "none" ||
		env["OTEL_TRACES_EXPORTER"] != "none" {
		t.Fatalf("expected managed Claude config to disable OTEL exporters: %#v", env)
	}
	if env["ANTHROPIC_MODEL"] != "haiku" {
		t.Fatalf("expected managed Claude config to select haiku alias: %#v", env)
	}
	if env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] != "amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0" {
		t.Fatalf("unexpected Claude model: %#v", env)
	}
	if got := plan.ManagedDocument["model"]; got != "haiku" {
		t.Fatalf("expected Claude settings model to stay on haiku, got %#v", got)
	}
	// Anthropic-family models keep the single-family pinning: the other
	// family selectors and the subagent override must NOT be invented.
	for _, key := range []string{
		"ANTHROPIC_DEFAULT_OPUS_MODEL",
		"ANTHROPIC_DEFAULT_SONNET_MODEL",
		"CLAUDE_CODE_SUBAGENT_MODEL",
	} {
		if _, exists := env[key]; exists {
			t.Fatalf("did not expect %s for an Anthropic-family model: %#v", key, env)
		}
	}
}

// A managed model outside the Claude families (the Kimi K3 case) must map
// every selector Claude Code can emit at the managed alias. ANTHROPIC_MODEL
// alone only covers the main loop: background/fast-path calls request
// built-in claude-haiku-* identifiers and subagents resolve
// CLAUDE_CODE_SUBAGENT_MODEL, and the gateway 404s both unless they are
// pointed at the managed alias too.
func TestApplyClaudeManagedGatewayNonAnthropicModelMapsAllSelectors(t *testing.T) {
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{
			"mcpServers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"url": "https://preloop.example/mcp/v1",
				},
			},
		},
	}
	plan, err := applyClaudeManagedGateway(
		plan,
		"https://preloop.example",
		"claude-durable-token",
		"moonshot/kimi-k3-0905",
		nil,
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}

	env := plan.ManagedDocument["env"].(map[string]interface{})
	if env["ANTHROPIC_MODEL"] != "moonshot/kimi-k3-0905" {
		t.Fatalf("expected main model pinned to managed alias: %#v", env)
	}
	if env["ANTHROPIC_CUSTOM_MODEL_OPTION"] != "moonshot/kimi-k3-0905" {
		t.Fatalf("expected custom model option pinned to managed alias: %#v", env)
	}
	for _, key := range []string{
		"ANTHROPIC_DEFAULT_OPUS_MODEL",
		"ANTHROPIC_DEFAULT_SONNET_MODEL",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL",
		"CLAUDE_CODE_SUBAGENT_MODEL",
	} {
		if env[key] != "moonshot/kimi-k3-0905" {
			t.Fatalf("expected %s pinned to managed alias, got %#v", key, env[key])
		}
	}
	// A stale settings.model outranks the env pin in API-key mode, so
	// non-family aliases must also replace the root model key.
	if plan.ManagedDocument["model"] != "moonshot/kimi-k3-0905" {
		t.Fatalf(
			"expected root model pinned to managed alias, got %#v",
			plan.ManagedDocument["model"],
		)
	}
	if plan.ManagedModelAlias != "moonshot/kimi-k3-0905" {
		t.Fatalf("unexpected managed model alias: %q", plan.ManagedModelAlias)
	}
}

// Re-onboarding from a non-family model to an Anthropic-family model must not
// leave the all-selector mapping behind: a stale CLAUDE_CODE_SUBAGENT_MODEL or
// sibling-family key would keep routing part of the traffic at the old model.
func TestApplyClaudeManagedGatewayReonboardToAnthropicClearsNonFamilySelectors(t *testing.T) {
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{},
	}
	plan, err := applyClaudeManagedGateway(
		plan,
		"https://preloop.example",
		"claude-durable-token",
		"moonshot/kimi-k3-0905",
		nil,
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}
	plan, err = applyClaudeManagedGateway(
		plan,
		"https://preloop.example",
		"claude-durable-token",
		"anthropic/claude-sonnet-4-5",
		nil,
	)
	if err != nil {
		t.Fatalf("unexpected gateway re-apply error: %v", err)
	}

	env := plan.ManagedDocument["env"].(map[string]interface{})
	if env["ANTHROPIC_MODEL"] != "sonnet" {
		t.Fatalf("expected family selection key after re-onboard, got %#v", env["ANTHROPIC_MODEL"])
	}
	if env["ANTHROPIC_DEFAULT_SONNET_MODEL"] != "anthropic/claude-sonnet-4-5" {
		t.Fatalf("expected sonnet family key after re-onboard: %#v", env)
	}
	for _, key := range []string{
		"ANTHROPIC_DEFAULT_OPUS_MODEL",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL",
		"CLAUDE_CODE_SUBAGENT_MODEL",
	} {
		if value, exists := env[key]; exists {
			t.Fatalf("stale non-family selector %s survived re-onboard: %#v", key, value)
		}
	}
}

// Switching between two non-family models must refresh every selector — no
// env key may still point at the previous alias.
func TestApplyClaudeManagedGatewayReonboardBetweenNonFamilyModelsRefreshes(t *testing.T) {
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{},
	}
	plan, err := applyClaudeManagedGateway(
		plan,
		"https://preloop.example",
		"claude-durable-token",
		"moonshot/kimi-k3-0905",
		nil,
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}
	plan, err = applyClaudeManagedGateway(
		plan,
		"https://preloop.example",
		"claude-durable-token",
		"openai/gpt-5.4",
		nil,
	)
	if err != nil {
		t.Fatalf("unexpected gateway re-apply error: %v", err)
	}

	env := plan.ManagedDocument["env"].(map[string]interface{})
	for key, value := range env {
		text, ok := value.(string)
		if !ok {
			continue
		}
		if strings.Contains(text, "kimi-k3") {
			t.Fatalf("env key %s still points at the previous alias: %q", key, text)
		}
	}
	for _, key := range []string{
		"ANTHROPIC_MODEL",
		"ANTHROPIC_DEFAULT_OPUS_MODEL",
		"ANTHROPIC_DEFAULT_SONNET_MODEL",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL",
		"CLAUDE_CODE_SUBAGENT_MODEL",
	} {
		if env[key] != "openai/gpt-5.4" {
			t.Fatalf("expected %s refreshed to new alias, got %#v", key, env[key])
		}
	}
}

// Offboard-style env restore must remove the all-selector mapping written for
// a non-family model, mirroring how ANTHROPIC_MODEL itself is cleaned.
func TestRestoreClaudeGatewayEnvFromOriginalRemovesNonFamilySelectors(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "settings.json")
	original := []byte(`{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-local"
  }
}`)
	if err := os.WriteFile(configPath, original, 0644); err != nil {
		t.Fatalf("failed to write original config: %v", err)
	}

	agent := AgentConfig{Name: "Claude Code", ConfigPath: configPath}
	plan := managedMCPEnrollmentPlan{
		Agent: agent,
		ManagedDocument: map[string]interface{}{
			"env": map[string]interface{}{
				"ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-local",
			},
		},
	}
	managed, err := applyClaudeManagedGateway(
		plan,
		"https://preloop.example",
		"preloop-token",
		"moonshot/kimi-k3-0905",
		nil,
	)
	if err != nil {
		t.Fatalf("applyClaudeManagedGateway returned error: %v", err)
	}
	if err := writeAgentConfigDocument(agent, managed.ManagedDocument); err != nil {
		t.Fatalf("failed to write managed Claude config: %v", err)
	}

	if err := restoreClaudeGatewayEnvFromOriginal(agent, original, io.Discard); err != nil {
		t.Fatalf("restoreClaudeGatewayEnvFromOriginal returned error: %v", err)
	}

	restored, err := loadAgentConfigDocument(agent)
	if err != nil {
		t.Fatalf("failed to load restored config: %v", err)
	}
	env := restored["env"].(map[string]interface{})
	if env["ANTHROPIC_AUTH_TOKEN"] != "sk-ant-oat01-local" {
		t.Fatalf("expected original Claude auth restored, got %#v", env)
	}
	for _, key := range []string{
		"ANTHROPIC_MODEL",
		"ANTHROPIC_DEFAULT_OPUS_MODEL",
		"ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
		"ANTHROPIC_DEFAULT_SONNET_MODEL",
		"ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
		"CLAUDE_CODE_SUBAGENT_MODEL",
	} {
		if value, exists := env[key]; exists {
			t.Fatalf("expected %s removed by restore, got %#v", key, value)
		}
	}
}

func TestRestoreClaudeGatewayEnvFromOriginalRestoresLocalAuth(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "settings.json")
	original := []byte(`{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-local",
    "ANTHROPIC_MODEL": "sonnet"
  },
  "model": "sonnet",
  "mcpServers": {
    "preloop": {
      "url": "https://preloop.example/mcp/v1"
    }
  }
}`)
	if err := os.WriteFile(configPath, original, 0644); err != nil {
		t.Fatalf("failed to write original config: %v", err)
	}

	agent := AgentConfig{Name: "Claude Code", ConfigPath: configPath}
	plan := managedMCPEnrollmentPlan{
		Agent: agent,
		ManagedDocument: map[string]interface{}{
			"env": map[string]interface{}{
				"ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-local",
				"ANTHROPIC_MODEL":      "sonnet",
			},
			"model": "sonnet",
			"mcpServers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"url": "https://preloop.example/mcp/v1",
				},
			},
		},
	}
	managed, err := applyClaudeManagedGateway(
		plan,
		"https://preloop.example",
		"preloop-token",
		"anthropic/claude-haiku-4-5-20251001",
		nil,
	)
	if err != nil {
		t.Fatalf("applyClaudeManagedGateway returned error: %v", err)
	}
	if err := writeAgentConfigDocument(agent, managed.ManagedDocument); err != nil {
		t.Fatalf("failed to write managed Claude config: %v", err)
	}

	if err := restoreClaudeGatewayEnvFromOriginal(agent, original, io.Discard); err != nil {
		t.Fatalf("restoreClaudeGatewayEnvFromOriginal returned error: %v", err)
	}

	restored, err := loadAgentConfigDocument(agent)
	if err != nil {
		t.Fatalf("failed to load restored config: %v", err)
	}
	env := restored["env"].(map[string]interface{})
	if env["ANTHROPIC_AUTH_TOKEN"] != "sk-ant-oat01-local" ||
		env["ANTHROPIC_MODEL"] != "sonnet" {
		t.Fatalf("expected original Claude auth/model env restored, got %#v", env)
	}
	if _, exists := env["ANTHROPIC_BASE_URL"]; exists {
		t.Fatalf("expected Preloop Anthropic base URL removed, got %#v", env)
	}
	if _, exists := env["ANTHROPIC_API_KEY"]; exists {
		t.Fatalf("expected Preloop Anthropic API key removed, got %#v", env)
	}
	if restored["model"] != "sonnet" {
		t.Fatalf("expected root model restored, got %#v", restored["model"])
	}
	if _, ok := restored["mcpServers"].(map[string]interface{})["preloop"]; !ok {
		t.Fatalf("expected managed MCP config to remain present, got %#v", restored)
	}
}

func TestGenericAdapterValidateManagedConfigSupportsClaudeGateway(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: "Claude Code"})
	result := adapter.ValidateManagedConfig(map[string]interface{}{
		"mcpServers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"url": "https://preloop.example/mcp/v1",
				"headers": map[string]interface{}{
					"Authorization": "Bearer durable-token",
				},
				"transport": "http",
			},
		},
		"env": map[string]interface{}{
			"ANTHROPIC_BASE_URL":            "https://preloop.example/anthropic",
			"ANTHROPIC_API_KEY":             "durable-token",
			"ANTHROPIC_DEFAULT_HAIKU_MODEL": "amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0",
			"ANTHROPIC_CUSTOM_MODEL_OPTION": "amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0",
		},
		"model": "haiku",
	}, "https://preloop.example")
	if result["gateway_provider_ok"] != true || result["model_provider_rewritten"] != true {
		t.Fatalf("expected Claude gateway validation to pass, got %+v", result)
	}
	if got := result["gateway_model_alias"]; got != "amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0" {
		t.Fatalf("unexpected Claude gateway model alias: %#v", got)
	}
}

func TestApplyGeminiManagedGatewayConfiguresSettings(t *testing.T) {
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{
			"mcpServers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"url": "https://preloop.example/mcp/v1",
				},
			},
		},
	}
	plan, err := applyGeminiManagedGateway(
		plan,
		"https://preloop.example",
		"gemini-durable-token",
		"google/gemini-3.1-pro-preview",
	)
	if err != nil {
		t.Fatalf("unexpected gateway apply error: %v", err)
	}

	if got := plan.ManagedDocument["baseUrl"]; got != "https://preloop.example/gemini" {
		t.Fatalf("unexpected Gemini baseUrl: %#v", got)
	}
	if got := plan.ManagedDocument["apiKey"]; got != "gemini-durable-token" {
		t.Fatalf("unexpected Gemini apiKey: %#v", got)
	}
	if _, exists := plan.ManagedDocument["apiKeyHeader"]; exists {
		t.Fatalf("did not expect Gemini apiKeyHeader override, got %#v", plan.ManagedDocument["apiKeyHeader"])
	}
	modelConfig := plan.ManagedDocument["model"].(map[string]interface{})
	if modelConfig["name"] != "gemini-3.1-pro-preview" {
		t.Fatalf("unexpected Gemini model config: %#v", modelConfig)
	}
}

func TestRemoveGeminiManagedGatewaySelectionCleansPlan(t *testing.T) {
	plan := managedMCPEnrollmentPlan{
		ManagedDocument: map[string]interface{}{
			"baseUrl": "https://preloop.example/gemini",
			"apiKey":  "durable-token",
			"model": map[string]interface{}{
				"name": "gemini-3.1-pro-preview",
			},
			"mcpServers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"url": "https://preloop.example/mcp/v1",
				},
			},
		},
		ManagedModelAlias:   "google/gemini-3.1-pro-preview",
		ManagedProviderName: "preloop",
	}

	cleaned, err := removeGeminiManagedGatewaySelection(plan)
	if err != nil {
		t.Fatalf("removeGeminiManagedGatewaySelection returned error: %v", err)
	}
	if _, exists := cleaned.ManagedDocument["baseUrl"]; exists {
		t.Fatalf("expected Preloop Gemini baseUrl to be removed, got %#v", cleaned.ManagedDocument)
	}
	if _, exists := cleaned.ManagedDocument["apiKey"]; exists {
		t.Fatalf("expected Preloop Gemini apiKey to be removed, got %#v", cleaned.ManagedDocument)
	}
	if _, ok := cleaned.ManagedDocument["mcpServers"].(map[string]interface{})["preloop"]; !ok {
		t.Fatalf("expected managed MCP config to remain present, got %#v", cleaned.ManagedDocument)
	}
	if cleaned.ManagedModelAlias != "" || cleaned.ManagedProviderName != "" {
		t.Fatalf("expected gateway metadata cleared, got %#v", cleaned)
	}
}

func TestRestoreGeminiGatewaySelectionFromOriginalRemovesContaminatedGateway(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "settings.json")
	contaminatedOriginal := []byte(`{
		"baseUrl": "https://preloop.example/gemini",
		"apiKey": "preloop-token",
		"model": {"name": "gemini-3-flash-preview"},
		"mcpServers": {"preloop": {"url": "https://preloop.example/mcp/v1"}}
	}`)
	if err := os.WriteFile(configPath, contaminatedOriginal, 0644); err != nil {
		t.Fatalf("failed to write Gemini config: %v", err)
	}
	agent := AgentConfig{Name: "Gemini CLI", ConfigPath: configPath}

	if err := restoreGeminiGatewaySelectionFromOriginal(
		agent,
		contaminatedOriginal,
		io.Discard,
	); err != nil {
		t.Fatalf("restoreGeminiGatewaySelectionFromOriginal returned error: %v", err)
	}

	restored, err := loadAgentConfigDocument(agent)
	if err != nil {
		t.Fatalf("failed to reload Gemini config: %v", err)
	}
	if _, exists := restored["baseUrl"]; exists {
		t.Fatalf("expected Preloop Gemini baseUrl removed, got %#v", restored)
	}
	if _, exists := restored["apiKey"]; exists {
		t.Fatalf("expected Preloop Gemini apiKey removed, got %#v", restored)
	}
	if _, ok := restored["mcpServers"].(map[string]interface{})["preloop"]; !ok {
		t.Fatalf("expected managed MCP config to remain present, got %#v", restored)
	}
}

func TestGenericAdapterValidateManagedConfigSupportsGeminiGateway(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: "Gemini CLI"})
	result := adapter.ValidateManagedConfig(map[string]interface{}{
		"mcpServers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"url":  "https://preloop.example/mcp/v1",
				"type": "http",
				"headers": map[string]interface{}{
					"Authorization": "Bearer durable-token",
				},
			},
		},
		"baseUrl": "https://preloop.example/gemini",
		"apiKey":  "durable-token",
		"model": map[string]interface{}{
			"name": "gemini-3.1-pro-preview",
		},
	}, "https://preloop.example")
	if result["gateway_provider_ok"] != true || result["model_provider_rewritten"] != true {
		t.Fatalf("expected Gemini gateway validation to pass, got %+v", result)
	}
	if got := result["gateway_model_alias"]; got != "google/gemini-3.1-pro-preview" {
		t.Fatalf("unexpected Gemini gateway model alias: %#v", got)
	}
}

func TestGenericAdapterValidateManagedConfigKeepsDirectGeminiModelMCPOnly(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: "Gemini CLI"})
	result := adapter.ValidateManagedConfig(map[string]interface{}{
		"mcpServers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"url":  "https://preloop.example/mcp/v1",
				"type": "http",
				"headers": map[string]interface{}{
					"Authorization": "Bearer durable-token",
				},
			},
		},
		"model": map[string]interface{}{
			"name": "gemini-3-flash-preview",
		},
	}, "https://preloop.example")

	if result["gateway_provider_ok"] == true || result["model_provider_rewritten"] == true {
		t.Fatalf("expected direct Gemini model to remain MCP-only, got %+v", result)
	}
	if got := result["gateway_model_alias"]; got != "google/gemini-3-flash-preview" {
		t.Fatalf("expected model alias metadata to remain available, got %#v", got)
	}
}

func TestSyncManagedAgentRuntimeArtifactsInstallsGeminiLauncher(t *testing.T) {
	skipManagedLauncherOnWindows(t, "installing the Gemini managed launcher")
	home := t.TempDir()
	testenv.SetHome(t, home)

	launcherDir := filepath.Join(home, ".local", "bin")
	if err := os.MkdirAll(launcherDir, 0o755); err != nil {
		t.Fatalf("failed to create launcher dir: %v", err)
	}
	originalDir := filepath.Join(home, "orig-bin")
	if err := os.MkdirAll(originalDir, 0o755); err != nil {
		t.Fatalf("failed to create original bin dir: %v", err)
	}
	originalPath := filepath.Join(originalDir, "gemini")
	if err := os.WriteFile(
		originalPath,
		[]byte("#!/usr/bin/env bash\nprintf '%s|%s' \"$GEMINI_API_KEY\" \"$GOOGLE_GEMINI_BASE_URL\"\n"),
		0o755,
	); err != nil {
		t.Fatalf("failed to write fake gemini executable: %v", err)
	}
	t.Setenv(
		"PATH",
		launcherDir+string(os.PathListSeparator)+originalDir+string(os.PathListSeparator)+os.Getenv("PATH"),
	)

	if err := syncManagedAgentRuntimeArtifacts(
		AgentConfig{Name: "Gemini CLI"},
		"https://preloop.example",
		"gemini-durable-token",
	); err != nil {
		t.Fatalf("unexpected gemini launcher install error: %v", err)
	}

	wrapperPath := filepath.Join(launcherDir, "gemini")
	output, err := exec.Command(wrapperPath).CombinedOutput()
	if err != nil {
		t.Fatalf("managed gemini launcher failed: %v (%s)", err, string(output))
	}
	if got := string(output); got != "gemini-durable-token|https://preloop.example/gemini" {
		t.Fatalf("unexpected gemini launcher env output: %q", got)
	}
}

func TestSyncManagedAgentRuntimeArtifactsReplacesLegacyGeminiLauncher(t *testing.T) {
	skipManagedLauncherOnWindows(t, "replacing a legacy Gemini managed launcher")
	home := t.TempDir()
	testenv.SetHome(t, home)

	launcherDir := filepath.Join(home, ".local", "bin")
	if err := os.MkdirAll(launcherDir, 0o755); err != nil {
		t.Fatalf("failed to create launcher dir: %v", err)
	}
	originalDir := filepath.Join(home, "orig-bin")
	if err := os.MkdirAll(originalDir, 0o755); err != nil {
		t.Fatalf("failed to create original bin dir: %v", err)
	}
	originalPath := filepath.Join(originalDir, "gemini")
	if err := os.WriteFile(
		originalPath,
		[]byte("#!/usr/bin/env bash\nprintf '%s|%s' \"$GEMINI_API_KEY\" \"$GOOGLE_GEMINI_BASE_URL\"\n"),
		0o755,
	); err != nil {
		t.Fatalf("failed to write fake gemini executable: %v", err)
	}
	legacyWrapper := filepath.Join(launcherDir, "gemini")
	if err := os.WriteFile(
		legacyWrapper,
		[]byte("#!/bin/zsh\nset -eu\nif [[ -f \"$HOME/.preloop/agents/runtime/gemini-cli.env\" ]]; then\n  source \"$HOME/.preloop/agents/runtime/gemini-cli.env\"\nfi\nexec "+shellSingleQuote(originalPath)+" \"$@\"\n"),
		0o755,
	); err != nil {
		t.Fatalf("failed to write legacy gemini wrapper: %v", err)
	}
	t.Setenv(
		"PATH",
		launcherDir+string(os.PathListSeparator)+originalDir+string(os.PathListSeparator)+os.Getenv("PATH"),
	)

	if err := syncManagedAgentRuntimeArtifacts(
		AgentConfig{Name: "Gemini CLI"},
		"https://preloop.example",
		"gemini-durable-token",
	); err != nil {
		t.Fatalf("expected legacy gemini wrapper to be replaced, got: %v", err)
	}

	data, err := os.ReadFile(legacyWrapper)
	if err != nil {
		t.Fatalf("failed to read replaced gemini wrapper: %v", err)
	}
	if !strings.Contains(string(data), preloopManagedLauncherMarker) {
		t.Fatalf("expected replaced gemini wrapper to include managed marker, got %q", string(data))
	}
}

func TestSyncManagedAgentRuntimeArtifactsSkipsCodexLauncher(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	originalDir := filepath.Join(home, "orig-bin")
	if err := os.MkdirAll(originalDir, 0o755); err != nil {
		t.Fatalf("failed to create original bin dir: %v", err)
	}
	originalPath := filepath.Join(originalDir, "codex")
	if err := os.WriteFile(
		originalPath,
		[]byte("#!/usr/bin/env bash\nprintf '%s' \"$PRELOOP_TOKEN\"\n"),
		0o755,
	); err != nil {
		t.Fatalf("failed to write fake codex executable: %v", err)
	}
	t.Setenv("PATH", originalDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	if err := syncManagedAgentRuntimeArtifacts(
		AgentConfig{Name: "Codex CLI"},
		"https://preloop.example",
		"codex-durable-token",
	); err != nil {
		t.Fatalf("unexpected Codex runtime sync error: %v", err)
	}

	wrapperPath := filepath.Join(home, ".local", "bin", "codex")
	if _, err := os.Stat(wrapperPath); !os.IsNotExist(err) {
		t.Fatalf("expected Codex onboarding not to write a PATH wrapper, stat err=%v", err)
	}
	envPath := filepath.Join(home, ".preloop", "agents", "runtime", "codex-cli.env")
	if _, err := os.Stat(envPath); !os.IsNotExist(err) {
		t.Fatalf("expected Codex onboarding not to write a runtime env file, stat err=%v", err)
	}
}

func TestSyncManagedAgentRuntimeArtifactsRemovesLegacyCodexLauncher(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	runtimeDir := filepath.Join(home, ".preloop", "agents", "runtime")
	if err := os.MkdirAll(runtimeDir, 0o700); err != nil {
		t.Fatalf("failed to create runtime dir: %v", err)
	}
	envPath := filepath.Join(runtimeDir, "codex-cli.env")
	if err := os.WriteFile(envPath, []byte("export PRELOOP_TOKEN='legacy'\n"), 0o600); err != nil {
		t.Fatalf("failed to write runtime env file: %v", err)
	}

	launcherDir := filepath.Join(home, ".local", "bin")
	if err := os.MkdirAll(launcherDir, 0o755); err != nil {
		t.Fatalf("failed to create launcher dir: %v", err)
	}
	wrapperPath := filepath.Join(launcherDir, "codex")
	if err := os.WriteFile(
		wrapperPath,
		[]byte("#!/usr/bin/env bash\n"+preloopManagedLauncherMarker+"\nexec /usr/bin/env true\n"),
		0o755,
	); err != nil {
		t.Fatalf("failed to write legacy Codex wrapper: %v", err)
	}

	if err := syncManagedAgentRuntimeArtifacts(
		AgentConfig{Name: "Codex CLI"},
		"https://preloop.example",
		"codex-durable-token",
	); err != nil {
		t.Fatalf("unexpected Codex runtime sync error: %v", err)
	}
	if _, err := os.Stat(wrapperPath); !os.IsNotExist(err) {
		t.Fatalf("expected leftover Codex wrapper to be removed, got err=%v", err)
	}
	if _, err := os.Stat(envPath); !os.IsNotExist(err) {
		t.Fatalf("expected leftover Codex runtime env file to be removed, got err=%v", err)
	}
}

func TestRemoveManagedAgentRuntimeArtifactsRemovesLegacyGeminiLauncher(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	runtimeDir := filepath.Join(home, ".preloop", "agents", "runtime")
	if err := os.MkdirAll(runtimeDir, 0o700); err != nil {
		t.Fatalf("failed to create runtime dir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(runtimeDir, "gemini-cli.env"), []byte("export GEMINI_API_KEY='x'\n"), 0o600); err != nil {
		t.Fatalf("failed to write runtime env file: %v", err)
	}

	launcherDir := filepath.Join(home, ".local", "bin")
	if err := os.MkdirAll(launcherDir, 0o755); err != nil {
		t.Fatalf("failed to create launcher dir: %v", err)
	}
	legacyWrapper := filepath.Join(launcherDir, "gemini")
	if err := os.WriteFile(
		legacyWrapper,
		[]byte("#!/bin/zsh\nsource \"$HOME/.preloop/agents/runtime/gemini-cli.env\"\nexec /usr/bin/env true\n"),
		0o755,
	); err != nil {
		t.Fatalf("failed to write legacy gemini wrapper: %v", err)
	}

	if err := removeManagedAgentRuntimeArtifacts(AgentConfig{Name: "Gemini CLI"}); err != nil {
		t.Fatalf("unexpected remove error: %v", err)
	}
	if _, err := os.Stat(legacyWrapper); !os.IsNotExist(err) {
		t.Fatalf("expected legacy gemini wrapper to be removed, got err=%v", err)
	}
	if _, err := os.Stat(filepath.Join(runtimeDir, "gemini-cli.env")); !os.IsNotExist(err) {
		t.Fatalf("expected gemini runtime env file to be removed, got err=%v", err)
	}
}

func TestParseClaudeManagedGatewayUpstreamUsesShellExportsForBedrock(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	t.Setenv("AWS_BEARER_TOKEN_BEDROCK", "")
	t.Setenv("ANTHROPIC_MODEL", "")
	t.Setenv("CLAUDE_CODE_USE_BEDROCK", "")

	configPath := filepath.Join(home, ".claude", "settings.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0o755); err != nil {
		t.Fatalf("failed to create claude dir: %v", err)
	}
	if err := os.WriteFile(
		configPath,
		[]byte(`{"model":"haiku"}`),
		0o644,
	); err != nil {
		t.Fatalf("failed to write claude config: %v", err)
	}
	if err := os.WriteFile(
		filepath.Join(home, ".zshrc"),
		[]byte(
			"export CLAUDE_CODE_USE_BEDROCK=1\n"+
				"export ANTHROPIC_MODEL='us.anthropic.claude-haiku-4-5-20251001-v1:0' # comment\n"+
				"export AWS_BEARER_TOKEN_BEDROCK=bedrock-token\n",
		),
		0o644,
	); err != nil {
		t.Fatalf("failed to write zshrc: %v", err)
	}

	upstream, err := parseClaudeManagedGatewayUpstream(
		AgentConfig{Name: "Claude Code", ConfigPath: configPath},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected Claude Bedrock upstream to be detected")
	}
	if upstream.ProviderName != "amazon-bedrock" {
		t.Fatalf("expected Bedrock provider, got %#v", upstream.ProviderName)
	}
	if upstream.ManagedModelAlias != "amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0" {
		t.Fatalf("unexpected Claude Bedrock alias: %#v", upstream.ManagedModelAlias)
	}
	if upstream.APIKey != "bedrock-token" {
		t.Fatalf("expected Claude Bedrock bearer token, got %#v", upstream.APIKey)
	}
	if len(upstream.Notes) == 0 || !strings.Contains(strings.Join(upstream.Notes, " "), ".zshrc") {
		t.Fatalf("expected Claude Bedrock note to mention shell config, got %#v", upstream.Notes)
	}
}

func TestOpenClawValidateManagedConfigSupportsAnthropicGateway(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: "OpenClaw"})
	result := adapter.ValidateManagedConfig(map[string]interface{}{
		"mcp": map[string]interface{}{
			"servers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"url":       "https://preloop.example/mcp/v1",
					"transport": "http",
					"headers": map[string]interface{}{
						"Authorization": "Bearer durable-token",
					},
				},
			},
		},
		"agents": map[string]interface{}{
			"defaults": map[string]interface{}{
				"model": map[string]interface{}{
					"primary": "preloop/google/gemini-3.1-pro-preview",
				},
			},
		},
		"models": map[string]interface{}{
			"providers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"api":     "anthropic-messages",
					"baseUrl": "https://preloop.example/anthropic/v1",
				},
			},
		},
	}, "https://preloop.example")
	if result["validation_passed"] != true {
		t.Fatalf("expected OpenClaw anthropic gateway validation to pass, got %+v", result)
	}
}

func TestDiscoverAgentsFindsInstalledOpenClawWithoutConfig(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	openClawDir := filepath.Join(home, ".openclaw")
	if err := os.MkdirAll(openClawDir, 0755); err != nil {
		t.Fatalf("failed to create openclaw dir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(openClawDir, "openclaw.json.bak"), []byte(`{}`), 0644); err != nil {
		t.Fatalf("failed to write openclaw backup marker: %v", err)
	}

	discovered, err := discoverAgents(io.Discard, false)
	if err != nil {
		t.Fatalf("discoverAgents returned error: %v", err)
	}
	for _, agent := range discovered {
		if agent.Name != "OpenClaw" {
			continue
		}
		wantPath := filepath.Join(home, ".openclaw", "openclaw.json")
		if agent.ConfigPath != wantPath {
			t.Fatalf("expected synthesized OpenClaw config path %q, got %q", wantPath, agent.ConfigPath)
		}
		if len(agent.MCPServers) != 0 {
			t.Fatalf("expected empty MCP server set for detected OpenClaw install, got %+v", agent.MCPServers)
		}
		return
	}
	t.Fatalf("expected OpenClaw to be discovered from install markers, got %#v", discovered)
}

func TestRestoreAgentFromBackupRemovesSynthesizedConfig(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, ".config", "opencode", "config.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create config dir: %v", err)
	}
	if err := os.WriteFile(configPath, []byte(`{"mcpServers":{"preloop":{"url":"https://preloop.example/mcp/v1"}}}`), 0644); err != nil {
		t.Fatalf("failed to seed synthesized config: %v", err)
	}
	backupPath := filepath.Join(home, "backup.json")
	if err := os.WriteFile(backupPath, []byte{}, 0600); err != nil {
		t.Fatalf("failed to write backup placeholder: %v", err)
	}

	state := &localEnrollmentState{
		AgentName:          "OpenCode",
		RuntimePrincipalID: runtimePrincipalIDForAgent(AgentConfig{Name: "OpenCode", ConfigPath: configPath}),
		ConfigPath:         configPath,
		ConfigExisted:      false,
		BackupPath:         backupPath,
		ManagedServerName:  "preloop",
		ManagedServerURL:   "https://preloop.example/mcp/v1",
		AppliedAt:          time.Now().UTC(),
	}
	if _, err := restoreAgentFromBackup(AgentConfig{Name: "OpenCode", ConfigPath: configPath}, state); err != nil {
		t.Fatalf("unexpected restore error: %v", err)
	}
	if _, err := os.Stat(configPath); !os.IsNotExist(err) {
		t.Fatalf("expected synthesized config to be removed, stat err=%v", err)
	}
}

func TestRestoreAgentFromBackupRestoresNonEmptyBackupEvenWhenStateSaysSynthetic(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	configPath := filepath.Join(home, ".openclaw", "openclaw.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0755); err != nil {
		t.Fatalf("failed to create config dir: %v", err)
	}
	if err := os.WriteFile(configPath, []byte(`{"managed":true}`), 0644); err != nil {
		t.Fatalf("failed to seed managed config: %v", err)
	}
	backupPath := filepath.Join(home, "backup-openclaw.json")
	backup := []byte(`{"agents":{"defaults":{"model":{"primary":"openai/gpt-5"}}}}`)
	if err := os.WriteFile(backupPath, backup, 0600); err != nil {
		t.Fatalf("failed to write backup: %v", err)
	}

	state := &localEnrollmentState{
		AgentName:          "OpenClaw",
		RuntimePrincipalID: runtimePrincipalIDForAgent(AgentConfig{Name: "OpenClaw", ConfigPath: configPath}),
		ConfigPath:         configPath,
		ConfigExisted:      false,
		BackupPath:         backupPath,
		ManagedServerName:  "preloop",
		ManagedServerURL:   "https://preloop.example/mcp/v1",
		AppliedAt:          time.Now().UTC(),
	}
	if _, err := restoreAgentFromBackup(AgentConfig{Name: "OpenClaw", ConfigPath: configPath}, state); err != nil {
		t.Fatalf("unexpected restore error: %v", err)
	}
	restored, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatalf("failed to read restored config: %v", err)
	}
	if string(restored) != string(backup) {
		t.Fatalf("expected backup bytes to be restored, got %q", string(restored))
	}
}

func TestLocalEnrollmentStateRoundTrip(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	agent := AgentConfig{
		Name:               "Claude Code",
		DisplayName:        "Repo Assistant",
		RuntimePrincipalID: "repo-assistant-abcdef123456",
		ConfigPath:         filepath.Join(home, ".claude", "mcp-servers.json"),
	}
	state := &localEnrollmentState{
		AgentName:          "Claude Code",
		DisplayName:        "Repo Assistant",
		RuntimePrincipalID: runtimePrincipalIDForAgent(agent),
		ConfigPath:         agent.ConfigPath,
		BackupPath:         filepath.Join(home, ".preloop", "agents", "backups", "claude-code-abc123", "backup.json"),
		ManagedServerName:  "preloop",
		ManagedServerURL:   "https://preloop.ai/mcp/v1",
		AppliedAt:          time.Now().UTC().Round(time.Second),
	}
	if err := saveLocalEnrollmentState(state); err != nil {
		t.Fatalf("failed to save local enrollment state: %v", err)
	}

	loaded, err := loadLocalEnrollmentState(agent)
	if err != nil {
		t.Fatalf("failed to load local enrollment state: %v", err)
	}
	if loaded.BackupPath != state.BackupPath {
		t.Fatalf("expected backup path %q, got %q", state.BackupPath, loaded.BackupPath)
	}
	if loaded.ManagedServerURL != state.ManagedServerURL {
		t.Fatalf("expected managed server URL %q, got %q", state.ManagedServerURL, loaded.ManagedServerURL)
	}
}

func TestRemoveLocalEnrollmentStateDeletesPersistedState(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	agent := AgentConfig{
		Name:       "OpenClaw",
		ConfigPath: filepath.Join(home, ".openclaw", "openclaw.json"),
	}
	state := &localEnrollmentState{
		AgentName:          agent.Name,
		RuntimePrincipalID: runtimePrincipalIDForAgent(agent),
		ConfigPath:         agent.ConfigPath,
		BackupPath:         filepath.Join(home, "backup.json"),
		ManagedServerName:  "preloop",
		ManagedServerURL:   "https://preloop.example/mcp/v1",
		AppliedAt:          time.Now().UTC(),
	}
	if err := saveLocalEnrollmentState(state); err != nil {
		t.Fatalf("failed to save local enrollment state: %v", err)
	}

	if err := removeLocalEnrollmentState(agent); err != nil {
		t.Fatalf("unexpected error removing state: %v", err)
	}
	if _, err := loadLocalEnrollmentState(agent); err == nil {
		t.Fatal("expected local enrollment state to be removed")
	}
}

func TestCollectOffboardCleanupCandidatesHonorsRecentUsage(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/api/v1/agents":
			_ = json.NewEncoder(w).Encode(managedAgentListResponse{
				Items: []managedAgentSummary{
					{
						ID:                "agent-current",
						DisplayName:       "OpenClaw",
						SessionSourceType: "openclaw",
						SessionSourceID:   "openclaw-current",
						ManagedMCPServers: []string{"github", "jira"},
						LatestModelAlias:  "google/gemini-3.1-pro-preview",
					},
					{
						ID:                "agent-other",
						DisplayName:       "Codex",
						SessionSourceType: "codex",
						SessionSourceID:   "codex-other",
						ActivityStatus:    "recently_active",
						ManagedMCPServers: []string{"github"},
						LatestModelAlias:  "google/gemini-3.1-pro-preview",
					},
				},
			})
		case "/api/v1/mcp-servers":
			_ = json.NewEncoder(w).Encode([]mcpServerResponse{
				{ID: "srv-github", Name: "github"},
				{ID: "srv-jira", Name: "jira"},
			})
		case "/api/v1/ai-models":
			_ = json.NewEncoder(w).Encode([]aiModelResponse{
				{
					ID:   "model-1",
					Name: "OpenClaw google/gemini-3.1-pro-preview",
					MetaData: map[string]interface{}{
						"gateway": map[string]interface{}{
							"model_alias": "google/gemini-3.1-pro-preview",
						},
					},
				},
			})
		case "/api/v1/flows":
			_ = json.NewEncoder(w).Encode([]flowSummaryResponse{})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	candidates, err := collectOffboardCleanupCandidates(client, managedAgentSummary{
		ID:                "agent-current",
		DisplayName:       "OpenClaw",
		SessionSourceType: "openclaw",
		SessionSourceID:   "openclaw-current",
		ManagedMCPServers: []string{"github", "jira"},
		LatestModelAlias:  "google/gemini-3.1-pro-preview",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(candidates) != 3 {
		t.Fatalf("expected three cleanup candidates, got %#v", candidates)
	}

	byName := map[string]offboardCleanupCandidate{}
	for _, candidate := range candidates {
		byName[candidate.Name] = candidate
	}

	if got := byName["github"].RecentlyUsedBy; len(got) != 1 || got[0] != "Codex" {
		t.Fatalf("expected github to be protected by recent usage, got %#v", got)
	}
	if got := byName["jira"].RecentlyUsedBy; len(got) != 0 {
		t.Fatalf("expected jira to be removable, got %#v", got)
	}
	if got := byName["google/gemini-3.1-pro-preview"].RecentlyUsedBy; len(got) != 1 || got[0] != "Codex" {
		t.Fatalf("expected model alias to be protected by recent usage, got %#v", got)
	}
}

func TestCollectOffboardCleanupCandidatesProtectsModelsUsedByFlows(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/api/v1/agents":
			_ = json.NewEncoder(w).Encode(managedAgentListResponse{
				Items: []managedAgentSummary{
					{
						ID:                "agent-current",
						DisplayName:       "OpenClaw",
						SessionSourceType: "openclaw",
						SessionSourceID:   "openclaw-current",
						LatestModelAlias:  "google/gemini-3.1-pro-preview",
					},
				},
			})
		case "/api/v1/mcp-servers":
			_ = json.NewEncoder(w).Encode([]mcpServerResponse{})
		case "/api/v1/ai-models":
			_ = json.NewEncoder(w).Encode([]aiModelResponse{
				{
					ID:   "model-1",
					Name: "OpenClaw google/gemini-3.1-pro-preview",
					MetaData: map[string]interface{}{
						"gateway": map[string]interface{}{
							"model_alias": "google/gemini-3.1-pro-preview",
						},
					},
				},
			})
		case "/api/v1/flows":
			_ = json.NewEncoder(w).Encode([]flowSummaryResponse{
				{ID: "flow-1", Name: "Nightly Review", AIModelID: "model-1"},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	candidates, err := collectOffboardCleanupCandidates(client, managedAgentSummary{
		ID:                "agent-current",
		DisplayName:       "OpenClaw",
		SessionSourceType: "openclaw",
		SessionSourceID:   "openclaw-current",
		LatestModelAlias:  "google/gemini-3.1-pro-preview",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(candidates) != 1 {
		t.Fatalf("expected one cleanup candidate, got %#v", candidates)
	}
	if got := candidates[0].FlowReferences; len(got) != 1 || got[0] != "Nightly Review" {
		t.Fatalf("expected model candidate to be protected by flow reference, got %#v", got)
	}
}

func TestPromptOffboardCleanupSkipsAIModelRemovalWhenPolicyIsNo(t *testing.T) {
	deleteCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodDelete && r.URL.Path == "/api/v1/ai-models/model-1" {
			deleteCalls++
			w.WriteHeader(http.StatusNoContent)
			return
		}
		http.NotFound(w, r)
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	output := &bytes.Buffer{}
	err := promptOffboardCleanup(
		strings.NewReader(""),
		output,
		true,
		offboardCleanupNo,
		offboardCleanupAsk,
		client,
		[]offboardCleanupCandidate{
			{
				Kind:       "ai_model",
				Name:       "google/gemini-3.1-pro-preview",
				ResourceID: "model-1",
			},
		},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if deleteCalls != 0 {
		t.Fatalf("expected AI model deletion to be skipped, got %d delete call(s)", deleteCalls)
	}
}

func TestPromptOffboardCleanupSkipsAIModelUsedByFlowEvenWithRemoveModelYes(t *testing.T) {
	deleteCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodDelete && r.URL.Path == "/api/v1/ai-models/model-1" {
			deleteCalls++
			w.WriteHeader(http.StatusNoContent)
			return
		}
		http.NotFound(w, r)
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	output := &bytes.Buffer{}
	err := promptOffboardCleanup(
		strings.NewReader(""),
		output,
		true,
		offboardCleanupYes,
		offboardCleanupAsk,
		client,
		[]offboardCleanupCandidate{
			{
				Kind:           "ai_model",
				Name:           "google/gemini-3.1-pro-preview",
				ResourceID:     "model-1",
				FlowReferences: []string{"Nightly Review"},
			},
		},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if deleteCalls != 0 {
		t.Fatalf("expected AI model deletion to be skipped, got %d delete call(s)", deleteCalls)
	}
	if !strings.Contains(output.String(), "Nightly Review") {
		t.Fatalf("expected output to mention blocking flow reference, got %q", output.String())
	}
}

func TestPromptOffboardCleanupDeletesEligibleAIModelWhenPolicyIsYes(t *testing.T) {
	deleteCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodDelete && r.URL.Path == "/api/v1/ai-models/model-1" {
			deleteCalls++
			w.WriteHeader(http.StatusNoContent)
			return
		}
		http.NotFound(w, r)
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	output := &bytes.Buffer{}
	err := promptOffboardCleanup(
		strings.NewReader(""),
		output,
		true,
		offboardCleanupYes,
		offboardCleanupAsk,
		client,
		[]offboardCleanupCandidate{
			{
				Kind:       "ai_model",
				Name:       "google/gemini-3.1-pro-preview",
				ResourceID: "model-1",
			},
		},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if deleteCalls != 1 {
		t.Fatalf("expected AI model deletion, got %d delete call(s)", deleteCalls)
	}
	if !strings.Contains(output.String(), "Removed AI model") {
		t.Fatalf("expected output to confirm AI model removal, got %q", output.String())
	}
}

func TestPromptOffboardCleanupSkipsMCPServerRemovalWhenPolicyIsNo(t *testing.T) {
	deleteCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodDelete && r.URL.Path == "/api/v1/mcp-servers/srv-1" {
			deleteCalls++
			w.WriteHeader(http.StatusNoContent)
			return
		}
		http.NotFound(w, r)
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	output := &bytes.Buffer{}
	err := promptOffboardCleanup(
		strings.NewReader(""),
		output,
		true,
		offboardCleanupAsk,
		offboardCleanupNo,
		client,
		[]offboardCleanupCandidate{
			{
				Kind:       "mcp_server",
				Name:       "github",
				ResourceID: "srv-1",
			},
		},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if deleteCalls != 0 {
		t.Fatalf("expected MCP server deletion to be skipped, got %d delete call(s)", deleteCalls)
	}
}

func TestPromptOffboardCleanupSkipsMCPServerUsedByOtherAgentEvenWithYes(t *testing.T) {
	deleteCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodDelete && r.URL.Path == "/api/v1/mcp-servers/srv-1" {
			deleteCalls++
			w.WriteHeader(http.StatusNoContent)
			return
		}
		http.NotFound(w, r)
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	output := &bytes.Buffer{}
	err := promptOffboardCleanup(
		strings.NewReader(""),
		output,
		true,
		offboardCleanupAsk,
		offboardCleanupYes,
		client,
		[]offboardCleanupCandidate{
			{
				Kind:         "mcp_server",
				Name:         "github",
				ResourceID:   "srv-1",
				ReferencedBy: []string{"Codex"},
			},
		},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if deleteCalls != 0 {
		t.Fatalf("expected MCP server deletion to be skipped, got %d delete call(s)", deleteCalls)
	}
	if !strings.Contains(output.String(), "Codex") {
		t.Fatalf("expected output to mention blocking agent reference, got %q", output.String())
	}
}

func TestPromptOffboardCleanupDeletesEligibleMCPServerWhenPolicyIsYes(t *testing.T) {
	deleteCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodDelete && r.URL.Path == "/api/v1/mcp-servers/srv-1" {
			deleteCalls++
			w.WriteHeader(http.StatusNoContent)
			return
		}
		http.NotFound(w, r)
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	output := &bytes.Buffer{}
	err := promptOffboardCleanup(
		strings.NewReader(""),
		output,
		true,
		offboardCleanupAsk,
		offboardCleanupYes,
		client,
		[]offboardCleanupCandidate{
			{
				Kind:       "mcp_server",
				Name:       "github",
				ResourceID: "srv-1",
			},
		},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if deleteCalls != 1 {
		t.Fatalf("expected MCP server deletion, got %d delete call(s)", deleteCalls)
	}
	if !strings.Contains(output.String(), "Removed MCP server") {
		t.Fatalf("expected output to confirm MCP server removal, got %q", output.String())
	}
}

func TestLookupMCPServerContainerSupportsNestedConfig(t *testing.T) {
	container := lookupMCPServerContainer(map[string]interface{}{
		"mcp": map[string]interface{}{
			"servers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"url": "https://preloop.ai/mcp/v1",
				},
			},
		},
	})

	preloop, ok := container["preloop"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected nested preloop server, got %+v", container)
	}
	if preloop["url"] != "https://preloop.ai/mcp/v1" {
		t.Fatalf("unexpected preloop server: %+v", preloop)
	}
}

func TestOpenClawAdapterValidateManagedConfig(t *testing.T) {
	adapter := managedMCPAdapterForAgent(AgentConfig{Name: "OpenClaw"})
	result := adapter.ValidateManagedConfig(map[string]interface{}{
		"mcp": map[string]interface{}{
			"servers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"transport": "streamable-http",
					"url":       "https://preloop.example/mcp/v1",
					"headers": map[string]interface{}{
						"Authorization": "Bearer durable-token",
					},
				},
			},
		},
		"models": map[string]interface{}{
			"providers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"baseUrl": "https://preloop.example/openai/v1",
					"api":     "openai-responses",
				},
			},
		},
		"agents": map[string]interface{}{
			"defaults": map[string]interface{}{
				"model": map[string]interface{}{
					"primary": "preloop/openai/gpt-5",
				},
			},
		},
	}, "https://preloop.example")

	if result["adapter_key"] != "openclaw" {
		t.Fatalf("expected OpenClaw adapter key, got %+v", result)
	}
	if result["nested_mcp_servers_ok"] != true {
		t.Fatalf("expected nested mcp.servers validation, got %+v", result)
	}
	if result["transport_ok"] != true || result["authorization_header_ok"] != true {
		t.Fatalf("expected OpenClaw validation to pass, got %+v", result)
	}
	if result["validation_passed"] != true {
		t.Fatalf("expected OpenClaw validation to pass, got %+v", result)
	}
}
