package cmd

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestClaudeSubscriptionCustodyDisclosesGatewayEffects(t *testing.T) {
	for _, source := range []string{"claude-code", "account-ai-model"} {
		c := gatewayCredentialCustody(&managedGatewayUpstream{SourceAgent: source, CredentialType: anthropicClaudeCodeOAuthCredentialType, CredentialPayload: map[string]interface{}{"access_token": "synthetic"}})
		var output bytes.Buffer
		printCredentialCustody(&output, []credentialCustody{c})
		for _, want := range []string{"API Usage Billing", "claude.ai", "ANTHROPIC_API_KEY", "Team", "Enterprise", "remote managed settings", "organization policy", "connectors", "subscription usage still pays"} {
			if !strings.Contains(output.String(), want) {
				t.Errorf("missing disclosure %q for %s", want, source)
			}
		}
	}
}

func TestClaudeApprovalPromptExplainsWaitWithoutChangingDefault(t *testing.T) {
	testenv.SetTempHome(t)
	var output bytes.Buffer
	yes, err := promptForApprovalsOptIn(strings.NewReader("\n"), &output, AgentConfig{Name: "Claude Code"})
	if err != nil || !yes {
		t.Fatalf("default changed: yes=%t err=%v", yes, err)
	}
	for _, want := range []string{"Enforce", "Skill", "ToolSearch", "Agent", "Approvals", "570", "600", "Off: auto-approve (recorded)", "workflow"} {
		if !strings.Contains(output.String(), want) {
			t.Errorf("missing approval consequence %q", want)
		}
	}
}

func TestClaudeControlOffboardOwnership(t *testing.T) {
	for _, existing := range []bool{false, true} {
		t.Run(map[bool]string{false: "created", true: "preexisting"}[existing], func(t *testing.T) {
			home := testenv.SetTempHome(t)
			t.Setenv("PRELOOP_CONFIG_DIR", filepath.Join(home, ".preloop"))
			path := claudeControlConfigPath()
			original := []byte("{\"custom\": true}\n")
			if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
				t.Fatal(err)
			}
			var originalMode os.FileMode
			if existing {
				if err := os.WriteFile(path, original, 0640); err != nil {
					t.Fatal(err)
				}
			}
			if existing {
				info, err := os.Stat(path)
				if err != nil {
					t.Fatal(err)
				}
				originalMode = info.Mode().Perm()
			}
			// Claude enrollment does not install a sidecar service. An existing
			// service belongs to the operator even if its path looks familiar.
			sidecar := claudeAgentControlSidecarSpec().launchdPath()
			if err := os.MkdirAll(filepath.Dir(sidecar), 0700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(sidecar, []byte("operator-owned-service"), 0600); err != nil {
				t.Fatal(err)
			}
			for _, token := range []string{"synthetic-first", "synthetic-second"} {
				if err := writeClaudePreloopControlFile(map[string]interface{}{"enabled": true, "bearer_token": token}); err != nil {
					t.Fatal(err)
				}
			}
			if err := removeManagedAgentRuntimeArtifacts(AgentConfig{Name: "Claude Code"}); err != nil {
				t.Fatal(err)
			}
			data, err := os.ReadFile(path)
			if existing {
				if err != nil || !bytes.Equal(data, original) {
					t.Fatal("preexisting control file was not restored")
				}
				info, _ := os.Stat(path)
				if info.Mode().Perm() != originalMode {
					t.Fatal("original permissions were not restored")
				}
			} else if !os.IsNotExist(err) {
				t.Fatal("enrollment-created control file remains")
			}
			if data, err := os.ReadFile(sidecar); err != nil || string(data) != "operator-owned-service" {
				t.Fatal("unowned sidecar changed")
			}
			if err := removeManagedAgentRuntimeArtifacts(AgentConfig{Name: "Claude Code"}); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestClaudeOffboardPreservesUnownedControl(t *testing.T) {
	home := testenv.SetTempHome(t)
	t.Setenv("PRELOOP_CONFIG_DIR", filepath.Join(home, ".preloop"))
	path := claudeControlConfigPath()
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	original := []byte("{\"control\":{\"enabled\":true}}")
	if err := os.WriteFile(path, original, 0600); err != nil {
		t.Fatal(err)
	}
	if err := removeManagedAgentRuntimeArtifacts(AgentConfig{Name: "Claude Code"}); err != nil {
		t.Fatal(err)
	}
	if data, err := os.ReadFile(path); err != nil || !bytes.Equal(data, original) {
		t.Fatal("unowned control file changed")
	}
}

func TestClaudeControlRestoresPermissionsWhenBytesUnchanged(t *testing.T) {
	home := testenv.SetTempHome(t)
	t.Setenv("PRELOOP_CONFIG_DIR", filepath.Join(home, ".preloop"))
	path := claudeControlConfigPath()
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	original := []byte("{\n  \"control\": {\n    \"enabled\": true\n  }\n}")
	if err := os.WriteFile(path, original, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, 0640); err != nil {
		t.Fatal(err)
	}
	before, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := writeClaudePreloopControlFile(map[string]interface{}{"enabled": true}); err != nil {
		t.Fatal(err)
	}
	if err := removeManagedAgentRuntimeArtifacts(AgentConfig{Name: "Claude Code"}); err != nil {
		t.Fatal(err)
	}
	after, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if after.Mode().Perm() != before.Mode().Perm() {
		t.Fatal("offboard did not restore original permissions for unchanged bytes")
	}
}

func TestClaudeControlPreservesChangesAfterEnrollment(t *testing.T) {
	for _, symlink := range []bool{false, true} {
		t.Run(map[bool]string{false: "edited", true: "symlink"}[symlink], func(t *testing.T) {
			home := testenv.SetTempHome(t)
			t.Setenv("PRELOOP_CONFIG_DIR", filepath.Join(home, ".preloop"))
			if err := writeClaudePreloopControlFile(map[string]interface{}{"enabled": true}); err != nil {
				t.Fatal(err)
			}
			path := claudeControlConfigPath()
			original, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			if symlink {
				target := filepath.Join(home, "other-control.json")
				if err := os.WriteFile(target, original, 0600); err != nil {
					t.Fatal(err)
				}
				if err := os.Remove(path); err != nil {
					t.Fatal(err)
				}
				if err := os.Symlink(target, path); err != nil {
					t.Fatal(err)
				}
			} else {
				original = []byte("operator replaced this file")
				if err := os.WriteFile(path, original, 0600); err != nil {
					t.Fatal(err)
				}
			}
			if err := writeClaudePreloopControlFile(map[string]interface{}{"enabled": false}); err == nil {
				t.Fatal("re-onboard overwrote an unowned replacement")
			}
			if err := removeManagedAgentRuntimeArtifacts(AgentConfig{Name: "Claude Code"}); err == nil {
				t.Fatal("offboard must report the preserved replacement")
			}
			if data, err := os.ReadFile(path); err != nil || !bytes.Equal(data, original) {
				t.Fatal("operator replacement changed")
			}
		})
	}
}
