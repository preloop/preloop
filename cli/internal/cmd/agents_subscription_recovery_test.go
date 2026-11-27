package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestClaudeSubscriptionRecoveryAdvice(t *testing.T) {
	home := testenv.SetTempHome(t)
	agent := AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}
	mustWriteRecoveryFile(t, agent.ConfigPath, `{"env":{"ANTHROPIC_BASE_URL":"https://gateway.example.com/anthropic","ANTHROPIC_API_KEY":"synthetic"}}`)
	var out bytes.Buffer
	if err := restoreClaudeGatewayEnvFromOriginal(agent, []byte(`{}`), &out); err != nil {
		t.Fatal(err)
	}
	for _, advice := range []string{claudeCodeOAuthGatewayWarningNote(), out.String()} {
		syncAt, reconnectAt, apiAt := strings.Index(advice, `preloop agents sync-credentials "Claude Code"`), strings.Index(advice, `preloop agents reconnect "Claude Code"`), strings.Index(advice, "ANTHROPIC_API_KEY")
		if syncAt < 0 || reconnectAt <= syncAt || apiAt <= reconnectAt || strings.Contains(advice, "—") {
			t.Fatal("recovery advice must lead with sync and reconnect, with API billing last")
		}
	}
}

func TestClaudeOnboardPreflightsNodeBeforeMutation(t *testing.T) {
	for _, name := range []string{"missing", "old"} {
		t.Run(name, func(t *testing.T) { assertClaudeNodePreflightBeforeMutation(t, name == "old") })
	}
}

func assertClaudeNodePreflightBeforeMutation(t *testing.T, oldNode bool) {
	t.Helper()
	home := testenv.SetTempHome(t)
	t.Setenv("PATH", "")
	if oldNode {
		skipNoShebangOnWindows(t, "old Node version stub")
		bin := t.TempDir()
		writeExecutableForTest(t, filepath.Join(bin, "node"), "#!/bin/sh\nprintf 'v18.20.8\\n'\n")
		t.Setenv("PATH", bin)
	}
	t.Setenv("PRELOOP_CONFIRM", "")
	agent := AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}
	original := `{"model":"claude-haiku-4-5"}`
	mustWriteRecoveryFile(t, agent.ConfigPath, original)
	mustWriteRecoveryFile(t, filepath.Join(home, ".claude", ".credentials.json"), claudeKeychainBlob(claudeSyncLocalAccess, claudeSyncLocalRefresh, claudeSyncLocalExpires))
	previousTerminal := stdinIsTerminal
	stdinIsTerminal = func() bool { return true }
	t.Cleanup(func() { stdinIsTerminal = previousTerminal })
	mutations := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			mutations++
			t.Error("mutation before Node preflight")
		}
		_ = json.NewEncoder(w).Encode([]interface{}{})
	}))
	defer server.Close()
	t.Setenv("PRELOOP_URL", server.URL)
	var out bytes.Buffer
	var enrollErr error
	printed := captureStdout(t, func() error {
		enrollErr = executeManagedEnrollment(agent, managedEnrollmentOptions{
			Client:         api.NewClientWithToken(server.URL, "synthetic-session"),
			PreferredModel: "anthropic/claude-haiku-4-5", Input: strings.NewReader("\ny\n"), Output: &out, Approvals: true,
		})
		return nil
	})
	if enrollErr == nil || !strings.Contains(enrollErr.Error(), "node --version") || !strings.Contains(enrollErr.Error(), `preloop agents onboard "Claude Code"`) {
		t.Fatalf("missing Node repair: %v", enrollErr)
	}
	if runtime.GOOS == "darwin" && !strings.Contains(enrollErr.Error(), `export PATH="$(brew --prefix node@22)/bin:$PATH"`) {
		t.Fatal("missing macOS PATH repair")
	}
	if mutations != 0 || strings.Contains(printed+out.String(), "✓ Onboarded") {
		t.Fatal("incomplete onboarding reported success or mutated remote state")
	}
	after, err := os.ReadFile(agent.ConfigPath)
	if err != nil || string(after) != original {
		t.Fatal("preflight changed local settings")
	}
	if !strings.Contains(printed+out.String(), `"API billing"`) {
		t.Fatal("subscription billing label must be explained before applying settings")
	}
	if _, err := loadLocalEnrollmentState(agent); err == nil {
		t.Fatal("preflight created enrollment state")
	}
}

func TestClaudeControlPluginMissingNodeAndFailedSetup(t *testing.T) {
	testenv.SetTempHome(t)
	previous := lookupAgentControlNode
	lookupAgentControlNode = func(string) (string, error) { return "", errors.New("not found") }
	t.Cleanup(func() { lookupAgentControlNode = previous })
	agent := AgentConfig{Name: "Claude Code"}
	var out bytes.Buffer
	result := installAgentControlRuntimePlugin(agent, &out)
	if result["control_plugin_install_status"] != "runtime_node_unavailable" || result["control_plugin_verified"] != false {
		t.Fatal("missing Node was not detected before install")
	}
	if !strings.Contains(out.String(), "node --version") {
		t.Fatal("missing actionable repair")
	}
	for _, state := range []map[string]interface{}{result, {"control_plugin_install_status": "plugin_install_failed"}, {"control_plugin_verified": true, "control_channel_configured": false}} {
		if managedRuntimeControlReadinessError(agent, state) == nil {
			t.Fatal("failed Agent Control setup must stop onboarding before success")
		}
	}
	if managedRuntimeControlReadinessError(agent, map[string]interface{}{"control_plugin_verified": true, "control_channel_configured": true}) != nil {
		t.Fatal("working Agent Control was rejected")
	}
}

func TestClaudeApprovalOutputMatchesInstalledTimeout(t *testing.T) {
	home := testenv.SetTempHome(t)
	agent := AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}
	for _, configured := range []int{86400, 120} {
		if err := writePermissionHookCredential(agent, permissionSourceClaudeCode, "http://127.0.0.1:1", "synthetic", configured, nil, ""); err != nil {
			t.Fatal(err)
		}
		var out bytes.Buffer
		if err := installApprovalHooks(agent, "http://127.0.0.1:1", "synthetic", &out); err != nil {
			t.Fatal(err)
		}
		doc := readJSONDoc(t, agent.ConfigPath)
		hooks, _ := asObjectMap(doc["hooks"])
		entries, _ := hooks["PreToolUse"].([]interface{})
		entry, _ := asObjectMap(entries[0])
		commands, _ := entry["hooks"].([]interface{})
		command, _ := asObjectMap(commands[0])
		wantWait, wantHost := 570, 600
		if configured == 120 {
			wantWait, wantHost = 120, 150
		}
		if int(command["timeout"].(float64)) != wantHost {
			t.Fatal("unexpected installed timeout")
		}
		want := fmt.Sprintf("Approval wait budget: %ds (host hook timeout: %ds; configured budget: %ds)", wantWait, wantHost, configured)
		if !strings.Contains(out.String(), want) {
			t.Fatal("printed budget does not match the effective wait and installed timeout")
		}
	}
}

func TestClaudeEnrollmentRetainsCredentialSource(t *testing.T) {
	f := newClaudeSyncFixture(t)
	mustWriteRecoveryFile(t, f.agent.ConfigPath, `{"model":"claude-haiku-4-5"}`)
	credential, _ := resolveClaudeCredentialFileOAuthCredential()
	if credential == nil || credential.Source != claudeOAuthSourceFile {
		t.Fatal("file discovery lost its source")
	}
	upstream, err := parseClaudeManagedGatewayUpstream(f.agent)
	if err != nil || upstream == nil || upstream.CredentialSource != claudeOAuthSourceFile {
		t.Fatal("onboarding lost the selected credential source")
	}
	state := f.state()
	state.ClaudeOAuthCredentialSource = claudeOAuthSourceFile
	state.BackupPath = filepath.Join(f.home, "original.json")
	mustWriteRecoveryFile(t, state.BackupPath, `{}`)
	if err := saveLocalEnrollmentState(state); err != nil {
		t.Fatal(err)
	}
	backup, err := createLocalEnrollmentBackup(f.agent, true, []byte(`{}`), managedMCPEnrollmentPlan{})
	if err != nil || backup.ClaudeOAuthCredentialSource != claudeOAuthSourceFile {
		t.Fatal("re-onboarding lost the selected store")
	}
}

func TestClaudeNodePreflightChecksVersion(t *testing.T) {
	previousLookup, previousVersion := lookupAgentControlNode, readAgentControlNodeVersion
	t.Cleanup(func() { lookupAgentControlNode, readAgentControlNodeVersion = previousLookup, previousVersion })
	lookupAgentControlNode = func(string) (string, error) { return "/synthetic/node", nil }
	for _, tc := range []struct {
		version           string
		broken, supported bool
	}{
		{version: "v16.20.2"}, {version: "v18.20.8"}, {version: "v20.19.0", supported: true}, {version: "v22.16.0", supported: true},
		{version: "invalid"}, {version: "v22.16.0", broken: true},
	} {
		t.Run(tc.version+fmt.Sprint(tc.broken), func(t *testing.T) {
			readAgentControlNodeVersion = func(string) (string, error) {
				if tc.broken {
					return tc.version, errors.New("synthetic execution failure")
				}
				return tc.version, nil
			}
			err := preflightClaudeAgentControlNode(AgentConfig{Name: "Claude Code"})
			if (err == nil) != tc.supported {
				t.Fatal("Node prerequisite does not match the supported runtime minimum")
			}
		})
	}
}
