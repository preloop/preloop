package cmd

import (
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func clearClaudeAuthEnv(t *testing.T) {
	t.Helper()
	for _, key := range []string{"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_MODEL"} {
		t.Setenv(key, "")
	}
}

func stubAnthropicModels(t *testing.T, ids ...string) {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		parts := make([]string, 0, len(ids))
		for _, id := range ids {
			parts = append(parts, `{"id":"`+id+`"}`)
		}
		_, _ = io.WriteString(w, `{"data":[`+strings.Join(parts, ",")+`]}`)
	}))
	t.Cleanup(server.Close)
	previous := anthropicModelsURL
	anthropicModelsURL = server.URL
	t.Cleanup(func() { anthropicModelsURL = previous })
}

func stubClaudeVersion(t *testing.T, version string) {
	t.Helper()
	previous := claudeCodeInstalledVersion
	claudeCodeInstalledVersion = func() string { return version }
	t.Cleanup(func() { claudeCodeInstalledVersion = previous })
}

// An empty settings file with a working subscription login is Claude Code's
// stock setup. The picker must offer the subscription first, not only the
// account's API-key models.
func TestSubscriptionLoginWithEmptySettingsIsOfferedFirst(t *testing.T) {
	for _, settings := range []string{`{}`, "missing"} {
		home := testenv.SetTempHome(t)
		t.Setenv("PATH", t.TempDir())
		clearClaudeAuthEnv(t)
		stubClaudeVersion(t, "2.1.332")
		stubAnthropicModels(t, "claude-fable-5-1", "claude-fable-5-2", "claude-opus-4-6")
		path := filepath.Join(home, ".claude", "settings.json")
		if settings != "missing" {
			mustWriteRecoveryFile(t, path, settings)
		}
		writeClaudeLoginFixture(t, home, "sk-ant-oat01-synthetic-subscription")

		upstream, err := parseClaudeManagedGatewayUpstream(AgentConfig{Name: "Claude Code", ConfigPath: path})
		if err != nil || upstream == nil {
			t.Fatalf("settings %q: upstream = %v, err = %v", settings, upstream, err)
		}
		if upstream.ManagedModelAlias != "anthropic/claude-fable-5-2" {
			t.Fatalf("alias = %q, want the newest fable", upstream.ManagedModelAlias)
		}
		if !isOAuthCredentialType(upstream.CredentialType) {
			t.Fatalf("credential type = %q, want subscription OAuth", upstream.CredentialType)
		}
		choices := listGatewayModelChoices(nil, upstream, io.Discard)
		if len(choices) == 0 || !choices[0].Inferred || !strings.Contains(choices[0].Label, "subscription/OAuth") {
			t.Fatalf("first choice = %+v, want the inferred subscription", choices)
		}
	}
}

func TestSubscriptionDefaultIsOpusBeforeBuiltinFable(t *testing.T) {
	home := testenv.SetTempHome(t)
	t.Setenv("PATH", t.TempDir())
	clearClaudeAuthEnv(t)
	stubClaudeVersion(t, "2.1.300")
	stubAnthropicModels(t, "claude-opus-4-6", "claude-fable-5-2")
	path := filepath.Join(home, ".claude", "settings.json")
	mustWriteRecoveryFile(t, path, `{}`)
	writeClaudeLoginFixture(t, home, "sk-ant-oat01-synthetic-subscription")

	upstream, err := parseClaudeManagedGatewayUpstream(AgentConfig{Name: "Claude Code", ConfigPath: path})
	if err != nil || upstream == nil || upstream.ManagedModelAlias != "anthropic/claude-opus-4-6" {
		t.Fatalf("upstream = %+v, err = %v", upstream, err)
	}
}

// Without a subscription login an empty settings file still infers nothing:
// an API key alone does not say which model to route.
func TestEmptySettingsWithoutSubscriptionInfersNothing(t *testing.T) {
	home := testenv.SetTempHome(t)
	t.Setenv("PATH", t.TempDir())
	clearClaudeAuthEnv(t)
	t.Setenv("ANTHROPIC_API_KEY", "sk-ant-api03-synthetic")
	stubClaudeVersion(t, "2.1.332")
	path := filepath.Join(home, ".claude", "settings.json")
	mustWriteRecoveryFile(t, path, `{}`)

	upstream, err := parseClaudeManagedGatewayUpstream(AgentConfig{Name: "Claude Code", ConfigPath: path})
	if err != nil || upstream != nil {
		t.Fatalf("upstream = %+v, err = %v; want none", upstream, err)
	}
}
