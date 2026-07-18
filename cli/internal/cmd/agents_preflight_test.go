package cmd

import (
	"bytes"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// setPreflightTestEnv isolates auth-state detection from the developer's
// machine: fresh HOME, no ambient Anthropic/Codex credentials, no keychain,
// no PRELOOP_CONFIRM auto-answers.
func setPreflightTestEnv(t *testing.T) string {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	t.Setenv("CODEX_HOME", filepath.Join(home, ".codex"))
	t.Setenv("ANTHROPIC_API_KEY", "")
	t.Setenv("ANTHROPIC_AUTH_TOKEN", "")
	t.Setenv("PRELOOP_CONFIRM", "")

	originalClaudeProbe := claudeKeychainPresenceProbe
	originalCodexProbe := codexKeychainPresenceProbe
	claudeKeychainPresenceProbe = func() bool { return false }
	codexKeychainPresenceProbe = func() bool { return false }
	t.Cleanup(func() {
		claudeKeychainPresenceProbe = originalClaudeProbe
		codexKeychainPresenceProbe = originalCodexProbe
	})
	return home
}

func writePreflightFixture(t *testing.T, path string, contents string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("failed to create fixture dir: %v", err)
	}
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatalf("failed to write fixture %s: %v", path, err)
	}
}

const openClawAuthedConfigFixture = `{
  "agents": {
    "defaults": {
      "model": {
        "primary": "bedrock/anthropic.claude-opus-4-6-2025-11-01-v1:0"
      }
    }
  },
  "models": {
    "providers": {
      "bedrock": {
        "baseUrl": "https://bedrock-runtime.us-east-1.amazonaws.com",
        "apiKey": "ABSKQ-preflight-test",
        "auth": "api-key",
        "api": "bedrock-converse-stream",
        "models": [
          {"id": "anthropic.claude-opus-4-6-2025-11-01-v1:0", "name": "Claude Opus 4.6 (Bedrock)"}
        ]
      }
    }
  }
}`

func TestDetectAgentAuthStateMatrix(t *testing.T) {
	tests := []struct {
		name          string
		agent         AgentConfig
		setup         func(t *testing.T, home string) AgentConfig
		claudeProbe   bool
		codexProbe    bool
		expectedState agentAuthState
	}{
		{
			name:  "claude code with oauth credentials file is ready",
			agent: AgentConfig{Name: "Claude Code"},
			setup: func(t *testing.T, home string) AgentConfig {
				writePreflightFixture(
					t,
					filepath.Join(home, ".claude", ".credentials.json"),
					`{"claudeAiOauth": {"accessToken": "tok-live", "refreshToken": "tok-refresh"}}`,
				)
				return AgentConfig{Name: "Claude Code"}
			},
			expectedState: agentAuthStateReady,
		},
		{
			name:  "claude code credentials file without token is not logged in",
			agent: AgentConfig{Name: "Claude Code"},
			setup: func(t *testing.T, home string) AgentConfig {
				writePreflightFixture(
					t,
					filepath.Join(home, ".claude", ".credentials.json"),
					`{}`,
				)
				return AgentConfig{Name: "Claude Code"}
			},
			expectedState: agentAuthStateNotLoggedIn,
		},
		{
			name:  "claude code with primary api key in claude.json is ready",
			agent: AgentConfig{Name: "Claude Code"},
			setup: func(t *testing.T, home string) AgentConfig {
				writePreflightFixture(
					t,
					filepath.Join(home, ".claude.json"),
					`{"primaryApiKey": "sk-ant-test"}`,
				)
				return AgentConfig{Name: "Claude Code"}
			},
			expectedState: agentAuthStateReady,
		},
		{
			name:          "claude code with keychain entry only is ready",
			agent:         AgentConfig{Name: "Claude Code"},
			claudeProbe:   true,
			expectedState: agentAuthStateReady,
		},
		{
			name:          "claude code with no artifacts is not logged in",
			agent:         AgentConfig{Name: "Claude Code"},
			expectedState: agentAuthStateNotLoggedIn,
		},
		{
			name:  "claude code with anthropic api key env is ready",
			agent: AgentConfig{Name: "Claude Code"},
			setup: func(t *testing.T, home string) AgentConfig {
				t.Setenv("ANTHROPIC_API_KEY", "sk-ant-env")
				return AgentConfig{Name: "Claude Code"}
			},
			expectedState: agentAuthStateReady,
		},
		{
			name:          "codex without auth.json is not logged in",
			agent:         AgentConfig{Name: "Codex CLI"},
			expectedState: agentAuthStateNotLoggedIn,
		},
		{
			name:          "codex without auth.json but keychain entry is ready",
			agent:         AgentConfig{Name: "Codex CLI"},
			codexProbe:    true,
			expectedState: agentAuthStateReady,
		},
		{
			name:  "codex logged-out auth.json shape is not logged in",
			agent: AgentConfig{Name: "Codex CLI"},
			setup: func(t *testing.T, home string) AgentConfig {
				writePreflightFixture(
					t,
					filepath.Join(home, ".codex", "auth.json"),
					`{"OPENAI_API_KEY": null, "tokens": null}`,
				)
				return AgentConfig{Name: "Codex CLI"}
			},
			expectedState: agentAuthStateNotLoggedIn,
		},
		{
			name:  "codex api key auth.json is ready",
			agent: AgentConfig{Name: "Codex CLI"},
			setup: func(t *testing.T, home string) AgentConfig {
				writePreflightFixture(
					t,
					filepath.Join(home, ".codex", "auth.json"),
					`{"OPENAI_API_KEY": "sk-test"}`,
				)
				return AgentConfig{Name: "Codex CLI"}
			},
			expectedState: agentAuthStateReady,
		},
		{
			name:  "codex chatgpt oauth tokens are ready",
			agent: AgentConfig{Name: "Codex CLI"},
			setup: func(t *testing.T, home string) AgentConfig {
				writePreflightFixture(
					t,
					filepath.Join(home, ".codex", "auth.json"),
					`{"tokens": {"access_token": "a", "refresh_token": "r", "account_id": "acct"}, "last_refresh": "2026-07-17T00:00:00Z"}`,
				)
				return AgentConfig{Name: "Codex CLI"}
			},
			expectedState: agentAuthStateReady,
		},
		{
			name:  "codex malformed auth.json is unknown",
			agent: AgentConfig{Name: "Codex CLI"},
			setup: func(t *testing.T, home string) AgentConfig {
				writePreflightFixture(
					t,
					filepath.Join(home, ".codex", "auth.json"),
					`{not json`,
				)
				return AgentConfig{Name: "Codex CLI"}
			},
			expectedState: agentAuthStateUnknown,
		},
		{
			name:          "opencode without auth.json is not logged in",
			agent:         AgentConfig{Name: "OpenCode"},
			expectedState: agentAuthStateNotLoggedIn,
		},
		{
			name:  "opencode empty auth.json is not logged in",
			agent: AgentConfig{Name: "OpenCode"},
			setup: func(t *testing.T, home string) AgentConfig {
				writePreflightFixture(
					t,
					filepath.Join(home, ".local", "share", "opencode", "auth.json"),
					`{}`,
				)
				return AgentConfig{Name: "OpenCode"}
			},
			expectedState: agentAuthStateNotLoggedIn,
		},
		{
			name:  "opencode with provider auth profile is ready",
			agent: AgentConfig{Name: "OpenCode"},
			setup: func(t *testing.T, home string) AgentConfig {
				writePreflightFixture(
					t,
					filepath.Join(home, ".local", "share", "opencode", "auth.json"),
					`{"anthropic": {"type": "oauth", "access": "a", "refresh": "r"}}`,
				)
				return AgentConfig{Name: "OpenCode"}
			},
			expectedState: agentAuthStateReady,
		},
		{
			name:  "opencode malformed auth.json is unknown",
			agent: AgentConfig{Name: "OpenCode"},
			setup: func(t *testing.T, home string) AgentConfig {
				writePreflightFixture(
					t,
					filepath.Join(home, ".local", "share", "opencode", "auth.json"),
					`nope`,
				)
				return AgentConfig{Name: "OpenCode"}
			},
			expectedState: agentAuthStateUnknown,
		},
		{
			name:  "openclaw with provider api key is ready",
			agent: AgentConfig{Name: "OpenClaw"},
			setup: func(t *testing.T, home string) AgentConfig {
				configPath := filepath.Join(home, ".openclaw", "openclaw.json")
				writePreflightFixture(t, configPath, openClawAuthedConfigFixture)
				return AgentConfig{Name: "OpenClaw", ConfigPath: configPath}
			},
			expectedState: agentAuthStateReady,
		},
		{
			name:  "openclaw without provider entries is not logged in",
			agent: AgentConfig{Name: "OpenClaw"},
			setup: func(t *testing.T, home string) AgentConfig {
				configPath := filepath.Join(home, ".openclaw", "openclaw.json")
				writePreflightFixture(t, configPath, `{"mcp": {"servers": {}}}`)
				return AgentConfig{Name: "OpenClaw", ConfigPath: configPath}
			},
			expectedState: agentAuthStateNotLoggedIn,
		},
		{
			name: "openclaw with missing config file is not logged in",
			setup: func(t *testing.T, home string) AgentConfig {
				return AgentConfig{
					Name:       "OpenClaw",
					ConfigPath: filepath.Join(home, ".openclaw", "openclaw.json"),
				}
			},
			expectedState: agentAuthStateNotLoggedIn,
		},
		{
			name:  "openclaw with unparseable config is unknown",
			agent: AgentConfig{Name: "OpenClaw"},
			setup: func(t *testing.T, home string) AgentConfig {
				configPath := filepath.Join(home, ".openclaw", "openclaw.json")
				writePreflightFixture(t, configPath, `{{{`)
				return AgentConfig{Name: "OpenClaw", ConfigPath: configPath}
			},
			expectedState: agentAuthStateUnknown,
		},
		{
			name:          "cursor is unknown by design",
			agent:         AgentConfig{Name: "Cursor", ConfigPath: "/tmp/mcp.json"},
			expectedState: agentAuthStateUnknown,
		},
		{
			name:          "claude desktop is unknown by design",
			agent:         AgentConfig{Name: "Claude Desktop", ConfigPath: "/tmp/claude_desktop_config.json"},
			expectedState: agentAuthStateUnknown,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			home := setPreflightTestEnv(t)
			claudeKeychainPresenceProbe = func() bool { return tt.claudeProbe }
			codexKeychainPresenceProbe = func() bool { return tt.codexProbe }
			agent := tt.agent
			if tt.setup != nil {
				agent = tt.setup(t, home)
			}
			state, detail := detectAgentAuthState(agent)
			if state != tt.expectedState {
				t.Fatalf(
					"detectAgentAuthState(%s) = %q (%s), expected %q",
					agent.Name,
					state,
					detail,
					tt.expectedState,
				)
			}
			if strings.TrimSpace(detail) == "" {
				t.Fatal("expected a non-empty auth detail")
			}
		})
	}
}

func TestSupportLevelForAgentMatrix(t *testing.T) {
	fullAgents := []string{"Claude Code", "Codex CLI", "OpenCode", "Gemini CLI", "Hermes", "OpenClaw"}
	mcpOnlyAgents := []string{"Cursor", "Claude Desktop", "Windsurf", "VSCode / Copilot", "Antigravity", "Devin"}
	for _, name := range fullAgents {
		if level := supportLevelForAgent(AgentConfig{Name: name}); level != agentSupportLevelFull {
			t.Fatalf("expected %s to be full support, got %q", name, level)
		}
	}
	for _, name := range mcpOnlyAgents {
		if level := supportLevelForAgent(AgentConfig{Name: name}); level != agentSupportLevelMCPOnly {
			t.Fatalf("expected %s to be mcp-only, got %q", name, level)
		}
	}
}

func TestAgentSupportListingLabel(t *testing.T) {
	if label := agentSupportListingLabel(AgentConfig{Name: "Claude Desktop"}); label != mcpOnlySupportLabel {
		t.Fatalf("expected mcp-only label for Claude Desktop, got %q", label)
	}
	if label := agentSupportListingLabel(AgentConfig{Name: "Codex CLI"}); label != fullSupportLabel {
		t.Fatalf("expected full label for Codex CLI, got %q", label)
	}
}

func TestAgentAuthListingLabel(t *testing.T) {
	tests := []struct {
		agent    AgentConfig
		expected string
	}{
		{AgentConfig{Name: "Codex CLI", AuthState: string(agentAuthStateReady)}, "Ready"},
		{AgentConfig{Name: "Codex CLI", AuthState: string(agentAuthStateNotLoggedIn)}, "Not logged in (run `codex login`)"},
		{AgentConfig{Name: "Claude Desktop", AuthState: string(agentAuthStateUnknown)}, "Unknown"},
		{AgentConfig{Name: "Cursor"}, "Unknown"},
	}
	for _, tt := range tests {
		if label := agentAuthListingLabel(tt.agent); label != tt.expected {
			t.Fatalf("agentAuthListingLabel(%s/%s) = %q, expected %q", tt.agent.Name, tt.agent.AuthState, label, tt.expected)
		}
	}
}

func TestPrintAgentAuthPreflightNotice(t *testing.T) {
	var buffer bytes.Buffer
	printAgentAuthPreflightNotice(&buffer, AgentConfig{
		Name:      "Codex CLI",
		AuthState: string(agentAuthStateNotLoggedIn),
	})
	output := buffer.String()
	expected := "Codex CLI: not logged in — MCP/model config will be written now; " +
		"run `codex login` then `preloop agents validate 'Codex CLI' --live`"
	if !strings.Contains(output, expected) {
		t.Fatalf("expected notice %q, got %q", expected, output)
	}
	if strings.Count(output, "\n") != 1 {
		t.Fatalf("expected exactly one notice line, got %q", output)
	}

	buffer.Reset()
	printAgentAuthPreflightNotice(&buffer, AgentConfig{
		Name:      "Codex CLI",
		AuthState: string(agentAuthStateReady),
	})
	if buffer.Len() != 0 {
		t.Fatalf("expected no notice for a ready agent, got %q", buffer.String())
	}
}

func TestClassifySuccessfulOnboardingReflectsAuthAndSupport(t *testing.T) {
	notLoggedIn := classifyAgentOnboardingOutcome(AgentConfig{
		Name:      "Codex CLI",
		AuthState: string(agentAuthStateNotLoggedIn),
	}, nil)
	if notLoggedIn.Status != agentOnboardingStatusOnboarded {
		t.Fatalf("expected onboarded status, got %q", notLoggedIn.Status)
	}
	if !strings.Contains(notLoggedIn.Reason, "not logged in") ||
		!strings.Contains(notLoggedIn.Reason, "codex login") {
		t.Fatalf("expected not-logged-in reason with login command, got %q", notLoggedIn.Reason)
	}

	mcpOnly := classifyAgentOnboardingOutcome(AgentConfig{
		Name:      "Claude Desktop",
		AuthState: string(agentAuthStateUnknown),
	}, nil)
	if mcpOnly.Reason != mcpOnlySupportLabel {
		t.Fatalf("expected mcp-only support reason, got %q", mcpOnly.Reason)
	}

	ready := classifyAgentOnboardingOutcome(AgentConfig{
		Name:      "Codex CLI",
		AuthState: string(agentAuthStateReady),
	}, nil)
	if ready.Reason != "" {
		t.Fatalf("expected empty reason for a ready full-support agent, got %q", ready.Reason)
	}
}

func TestPrintAgentOnboardingSummaryShowsSupportLevelReason(t *testing.T) {
	var buffer bytes.Buffer
	printAgentOnboardingSummary(&buffer, []agentOnboardingOutcome{
		classifyAgentOnboardingOutcome(AgentConfig{
			Name:      "Claude Desktop",
			AuthState: string(agentAuthStateUnknown),
		}, nil),
	})
	if !strings.Contains(buffer.String(), mcpOnlySupportLabel) {
		t.Fatalf("expected summary to include %q, got %q", mcpOnlySupportLabel, buffer.String())
	}
}

func TestMCPOnlyAgentModelNoteLeadsWithSupportLabel(t *testing.T) {
	for _, name := range []string{"Cursor", "Claude Desktop", "Antigravity", "Devin", "Windsurf"} {
		note := mcpOnlyAgentModelNote(AgentConfig{Name: name})
		if !strings.HasPrefix(note, mcpOnlySupportLabel) {
			t.Fatalf("expected %s note to lead with the support label, got %q", name, note)
		}
	}
	for _, name := range []string{"Claude Code", "Codex CLI", "OpenClaw"} {
		if note := mcpOnlyAgentModelNote(AgentConfig{Name: name}); note != "" {
			t.Fatalf("expected no mcp-only note for %s, got %q", name, note)
		}
	}
}

func staleOpenClawFixtureDocument() map[string]interface{} {
	return map[string]interface{}{
		"agents": map[string]interface{}{
			"defaults": map[string]interface{}{
				"model": map[string]interface{}{"primary": "anthropic/claude-opus-4-6"},
			},
		},
		"plugins": map[string]interface{}{
			"entries": map[string]interface{}{
				"preloop-plugin": map[string]interface{}{
					"enabled": true,
					"source":  "npm:preloop-plugin",
				},
				"unrelated-plugin": map[string]interface{}{
					"enabled": true,
				},
			},
		},
	}
}

func TestDetectStaleOpenClawPluginEntries(t *testing.T) {
	openclaw := AgentConfig{Name: "OpenClaw"}

	stale := detectStaleOpenClawPluginEntries(openclaw, staleOpenClawFixtureDocument())
	if !reflect.DeepEqual(stale, []string{"preloop-plugin"}) {
		t.Fatalf("expected [preloop-plugin], got %v", stale)
	}

	// The CLI's own config stash under a legacy id is not a loader entry and
	// must never be flagged.
	configStash := map[string]interface{}{
		"plugins": map[string]interface{}{
			"entries": map[string]interface{}{
				"preloop-plugin": map[string]interface{}{
					"config": map[string]interface{}{"control_ws_url": "wss://example"},
				},
			},
		},
	}
	if stale := detectStaleOpenClawPluginEntries(openclaw, configStash); len(stale) != 0 {
		t.Fatalf("expected config stash to be preserved, got %v", stale)
	}

	// An entry under a non-stale id whose source references a stale package
	// is flagged too.
	renamed := map[string]interface{}{
		"plugins": map[string]interface{}{
			"entries": map[string]interface{}{
				"my-preloop": map[string]interface{}{
					"enabled": true,
					"source":  "npm:@preloop/openclaw-plugin",
				},
			},
		},
	}
	if stale := detectStaleOpenClawPluginEntries(openclaw, renamed); !reflect.DeepEqual(stale, []string{"my-preloop"}) {
		t.Fatalf("expected [my-preloop], got %v", stale)
	}

	// The canonical package is never stale.
	canonical := map[string]interface{}{
		"plugins": map[string]interface{}{
			"entries": map[string]interface{}{
				canonicalOpenClawPluginPackage: map[string]interface{}{"enabled": true},
			},
		},
	}
	if stale := detectStaleOpenClawPluginEntries(openclaw, canonical); len(stale) != 0 {
		t.Fatalf("expected canonical plugin entry to be kept, got %v", stale)
	}

	// Non-OpenClaw agents are never touched.
	if stale := detectStaleOpenClawPluginEntries(AgentConfig{Name: "Codex CLI"}, staleOpenClawFixtureDocument()); len(stale) != 0 {
		t.Fatalf("expected no detection for non-OpenClaw agent, got %v", stale)
	}
}

func staleCleanupTestPlan(t *testing.T) (managedMCPEnrollmentPlan, string, []byte) {
	t.Helper()
	home := setPreflightTestEnv(t)
	configPath := filepath.Join(home, ".openclaw", "openclaw.json")
	originalConfig := `{
  "plugins": {
    "entries": {
      "preloop-plugin": {"enabled": true, "source": "npm:preloop-plugin"},
      "unrelated-plugin": {"enabled": true}
    }
  }
}`
	writePreflightFixture(t, configPath, originalConfig)
	return managedMCPEnrollmentPlan{
		Agent:           AgentConfig{Name: "OpenClaw", ConfigPath: configPath},
		ManagedDocument: staleOpenClawFixtureDocument(),
	}, configPath, []byte(originalConfig)
}

func pluginEntriesFromDocument(t *testing.T, doc map[string]interface{}) map[string]interface{} {
	t.Helper()
	plugins, ok := asObjectMap(doc["plugins"])
	if !ok {
		t.Fatal("expected plugins object in managed document")
	}
	entries, ok := asObjectMap(plugins["entries"])
	if !ok {
		t.Fatal("expected plugins.entries object in managed document")
	}
	return entries
}

func TestMaybeRemoveStaleOpenClawPluginEntriesAutoYes(t *testing.T) {
	plan, configPath, originalConfig := staleCleanupTestPlan(t)
	var output bytes.Buffer

	updated, err := maybeRemoveStaleOpenClawPluginEntries(
		plan,
		plan.Agent,
		managedEnrollmentOptions{AutoApprove: true},
		strings.NewReader(""),
		&output,
	)
	if err != nil {
		t.Fatalf("maybeRemoveStaleOpenClawPluginEntries returned error: %v", err)
	}

	entries := pluginEntriesFromDocument(t, updated.ManagedDocument)
	if _, exists := entries["preloop-plugin"]; exists {
		t.Fatal("expected stale preloop-plugin entry to be removed under -y")
	}
	if _, exists := entries["unrelated-plugin"]; !exists {
		t.Fatal("expected unrelated plugin entry to survive cleanup")
	}
	if !strings.Contains(output.String(), "Removing stale OpenClaw plugin entry preloop-plugin") {
		t.Fatalf("expected removal message, got %q", output.String())
	}
	if !strings.Contains(output.String(), canonicalOpenClawPluginPackage) {
		t.Fatalf("expected message to mention the superseding package, got %q", output.String())
	}

	// The backup source — the on-disk config — is untouched by cleanup.
	onDisk, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatalf("failed to re-read config: %v", err)
	}
	if !bytes.Equal(onDisk, originalConfig) {
		t.Fatal("expected the on-disk config (backup source) to remain unchanged")
	}
}

func TestMaybeRemoveStaleOpenClawPluginEntriesPromptAccepted(t *testing.T) {
	plan, _, _ := staleCleanupTestPlan(t)
	var output bytes.Buffer

	updated, err := maybeRemoveStaleOpenClawPluginEntries(
		plan,
		plan.Agent,
		managedEnrollmentOptions{},
		strings.NewReader("y\n"),
		&output,
	)
	if err != nil {
		t.Fatalf("maybeRemoveStaleOpenClawPluginEntries returned error: %v", err)
	}
	entries := pluginEntriesFromDocument(t, updated.ManagedDocument)
	if _, exists := entries["preloop-plugin"]; exists {
		t.Fatal("expected stale entry to be removed after accepting the prompt")
	}
	if !strings.Contains(output.String(), "Remove stale OpenClaw plugin entry preloop-plugin") {
		t.Fatalf("expected cleanup prompt, got %q", output.String())
	}
}

func TestMaybeRemoveStaleOpenClawPluginEntriesDeclined(t *testing.T) {
	plan, configPath, originalConfig := staleCleanupTestPlan(t)
	var output bytes.Buffer

	updated, err := maybeRemoveStaleOpenClawPluginEntries(
		plan,
		plan.Agent,
		managedEnrollmentOptions{},
		strings.NewReader("n\n"),
		&output,
	)
	if err != nil {
		t.Fatalf("maybeRemoveStaleOpenClawPluginEntries returned error: %v", err)
	}
	entries := pluginEntriesFromDocument(t, updated.ManagedDocument)
	if _, exists := entries["preloop-plugin"]; !exists {
		t.Fatal("expected stale entry to be preserved when the user declines")
	}
	if !strings.Contains(output.String(), "Keeping stale plugin entry") {
		t.Fatalf("expected decline message, got %q", output.String())
	}
	onDisk, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatalf("failed to re-read config: %v", err)
	}
	if !bytes.Equal(onDisk, originalConfig) {
		t.Fatal("expected the on-disk config to remain unchanged on decline")
	}
}

func TestStaleOpenClawPluginEntriesNote(t *testing.T) {
	note := staleOpenClawPluginEntriesNote([]string{"preloop-plugin"})
	if !strings.Contains(note, "preloop-plugin") ||
		!strings.Contains(note, canonicalOpenClawPluginPackage) {
		t.Fatalf("expected note to name the stale entry and its replacement, got %q", note)
	}
}
