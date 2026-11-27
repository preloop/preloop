package cmd

import (
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func writeClaudeLoginFixture(t *testing.T, home, access string) {
	t.Helper()
	blob := claudeKeychainBlob(access, "sk-ant-ort01-synthetic-refresh", time.Now().Add(4*time.Hour).UnixMilli())
	mustWriteRecoveryFile(t, filepath.Join(home, ".claude", ".credentials.json"), blob)
}

// Claude Code prefers CLAUDE_CODE_OAUTH_TOKEN over a stored login, so the
// import must too: a stale credentials file mounted into a container must not
// win over the live token in the environment.
func TestClaudeImportPrefersEnvTokenOverStaleCredentialsFile(t *testing.T) {
	home := testenv.SetTempHome(t)
	t.Setenv("PATH", t.TempDir())
	mustWriteRecoveryFile(t, filepath.Join(home, ".claude", "settings.json"), `{"model":"claude-sonnet-4-5"}`)
	writeClaudeLoginFixture(t, home, "sk-ant-oat01-synthetic-stale-file")
	t.Setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-synthetic-live-env")

	upstream, err := parseClaudeManagedGatewayUpstream(AgentConfig{
		Name:       "Claude Code",
		ConfigPath: filepath.Join(home, ".claude", "settings.json"),
	})
	if err != nil || upstream == nil {
		t.Fatalf("upstream: %v %v", upstream, err)
	}
	if upstream.CredentialSource != claudeOAuthSourceEnv {
		t.Fatalf("source = %q, want env", upstream.CredentialSource)
	}
	if upstream.CredentialPayload["access"] != "sk-ant-oat01-synthetic-live-env" {
		t.Fatal("the environment token must be the imported credential")
	}
	if _, hasRefresh := upstream.CredentialPayload["refresh"]; hasRefresh {
		t.Fatal("the stale file's refresh token must not be attached to the environment token")
	}
}

func TestClaudeImportKeepsFileBundleWhenEnvTokenIsTheSameLogin(t *testing.T) {
	home := testenv.SetTempHome(t)
	t.Setenv("PATH", t.TempDir())
	mustWriteRecoveryFile(t, filepath.Join(home, ".claude", "settings.json"), `{"model":"claude-sonnet-4-5"}`)
	writeClaudeLoginFixture(t, home, "sk-ant-oat01-synthetic-same")
	t.Setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-synthetic-same")

	upstream, err := parseClaudeManagedGatewayUpstream(AgentConfig{
		Name:       "Claude Code",
		ConfigPath: filepath.Join(home, ".claude", "settings.json"),
	})
	if err != nil || upstream == nil {
		t.Fatalf("upstream: %v %v", upstream, err)
	}
	if upstream.CredentialSource != claudeOAuthSourceFile {
		t.Fatalf("source = %q, want file", upstream.CredentialSource)
	}
}

func TestRejectedLoginAdviceNamesTheSource(t *testing.T) {
	home := testenv.SetTempHome(t)
	agent := AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}

	file := strings.Join(rejectedSubscriptionLoginAdvice(agent, claudeOAuthSourceFile), "\n")
	for _, want := range []string{
		filepath.Join(".claude", ".credentials.json"),
		"looks stale",
		"claude auth login --claudeai",
		`preloop agents reconnect "Claude Code" --from-local`,
	} {
		if !strings.Contains(file, want) {
			t.Fatalf("file advice misses %q:\n%s", want, file)
		}
	}
	env := strings.Join(rejectedSubscriptionLoginAdvice(agent, claudeOAuthSourceEnv), "\n")
	for _, want := range []string{"environment variable CLAUDE_CODE_OAUTH_TOKEN", "looks stale", "claude setup-token"} {
		if !strings.Contains(env, want) {
			t.Fatalf("env advice misses %q:\n%s", want, env)
		}
	}
	keychain := strings.Join(rejectedSubscriptionLoginAdvice(agent, claudeOAuthSourceKeychain), "\n")
	if !strings.Contains(keychain, "Keychain") {
		t.Fatalf("keychain advice: %s", keychain)
	}
	for _, text := range []string{file, env, keychain} {
		if strings.Contains(text, "sk-ant") {
			t.Fatal("advice must never contain token material")
		}
	}
}

func TestEnvSourcedLoginNeedsNoOffboardStore(t *testing.T) {
	home := testenv.SetTempHome(t)
	agent := AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}
	if err := saveLocalEnrollmentState(&localEnrollmentState{
		AgentName:                   agent.Name,
		ConfigPath:                  agent.ConfigPath,
		RuntimePrincipalID:          "claude-code-synthetic",
		ClaudeOAuthCredentialSource: claudeOAuthSourceEnv,
	}); err != nil {
		t.Fatal(err)
	}
	if !claudeLoginIsEnvSourced(agent) {
		t.Fatal("env-sourced login not recognised")
	}
}
